from __future__ import annotations

import argparse
import json
import os
import signal
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.openai.policy import (  # noqa: E402
    POLICY_PRESETS,
    RuntimePolicy,
    get_runtime_policy,
)
from app.services.openai.staged_runtime import StagedAgentRuntime  # noqa: E402
from app.services.openai.tools import AgentToolbox, ToolExecutionError  # noqa: E402
from app.services.orchestration import attach_verification  # noqa: E402
from app.services.retrieval.hybrid import tokenize  # noqa: E402
from app.services.sandbox.git_ops import diff  # noqa: E402
from app.services.sandbox.limits import parse_diff_stats  # noqa: E402
from app.services.sandbox.repo_config import load_repo_config  # noqa: E402
from experiments.swe_alignment.data import load_instances  # noqa: E402
from experiments.swe_alignment.reward import score_patch_evaluation  # noqa: E402
from experiments.swe_alignment.schema import SWEInstance  # noqa: E402
from app.services.adapters import swe_bench_task  # noqa: E402


class LocalRunner:
    def run_tests(self, repo_path: Path, test_command: str):
        command_tokens = shlex.split(test_command)
        if command_tokens[:2] == ["python", "-m"]:
            command = [sys.executable, "-m", *command_tokens[2:]]
        elif command_tokens and command_tokens[0] == "pytest":
            command = [sys.executable, "-m", "pytest", *command_tokens[1:]]
        else:
            command = command_tokens
        process = subprocess.run(
            command,
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return type("Result", (), {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})()


class InstanceTimeoutError(TimeoutError):
    pass


def _timeout_handler(signum, frame):  # noqa: ANN001
    raise InstanceTimeoutError("per_instance_timeout_reached")


class BenchmarkMemoryBank:
    """Small persistent-within-run memory bank for controlled ablations."""

    def __init__(self) -> None:
        self._entries: dict[str, list[dict[str, Any]]] = {}

    def recall(self, repo: str, query: str, limit: int = 6) -> list[dict]:
        query_tokens = set(tokenize(query))
        scored = []
        for entry in self._entries.get(repo, []):
            memory_tokens = set(
                tokenize(json.dumps(entry["content"], ensure_ascii=False))
            )
            overlap = len(query_tokens & memory_tokens)
            if overlap:
                scored.append((overlap / max(len(query_tokens), 1), entry))
        scored.sort(key=lambda item: -item[0])
        return [
            {**entry, "retrieval_score": round(score, 6)}
            for score, entry in scored[:limit]
        ]

    def remember(self, repo: str, row: dict[str, Any]) -> None:
        if row.get("status") != "success":
            return
        summary = row.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        localization = row.get("localization")
        if not isinstance(localization, dict):
            localization = {}
        self._entries.setdefault(repo, []).append(
            {
                "scope": "repository",
                "kind": "solution",
                "content": {
                    "instance_id": row.get("instance_id"),
                    "root_cause": summary.get("root_cause"),
                    "files": localization.get(
                        "candidate_files",
                        [],
                    ),
                },
                "evidence": localization.get("evidence", []),
                "confidence": localization.get(
                    "confidence",
                    0.7,
                ),
            }
        )


def _run_git(repo_path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo_path, check=True, capture_output=True, text=True)


def _ensure_git_repo(repo_path: Path) -> None:
    if (repo_path / ".git").exists():
        return
    _run_git(repo_path, "init")
    _run_git(repo_path, "config", "user.email", "swe-alignment@example.com")
    _run_git(repo_path, "config", "user.name", "swe-alignment")
    _run_git(repo_path, "add", ".")
    _run_git(repo_path, "commit", "-m", "init")


def _prepare_repo(instance: SWEInstance, run_root: Path) -> Path:
    repo_path = instance.meta.get("repo_path")
    repo_fixture = instance.meta.get("repo_fixture")
    target = run_root / instance.instance_id.replace("/", "__")
    if target.exists():
        shutil.rmtree(target)

    if repo_path:
        shutil.copytree(Path(repo_path).expanduser(), target, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache"))
    elif repo_fixture:
        fixture = ROOT / "app" / "tests" / "fixtures" / str(repo_fixture)
        shutil.copytree(fixture, target)
    else:
        raise ValueError(f"Instance {instance.instance_id} requires repo_path or repo_fixture")

    _ensure_git_repo(target)
    return target


def run_instance(
    instance: SWEInstance,
    output_dir: Path,
    *,
    policy: RuntimePolicy | None = None,
    memory_context: list[dict] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    policy = policy or get_runtime_policy("full")
    case_root = output_dir / "workspaces"
    case_root.mkdir(parents=True, exist_ok=True)
    repo_path = _prepare_repo(instance, case_root)
    repo_config = load_repo_config(repo_path)
    if instance.test_command:
        repo_config.test_command = instance.test_command

    issue_context = {
        "title": instance.problem_statement.strip().splitlines()[0][:120],
        "body": instance.problem_statement,
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "fail_to_pass": list(instance.fail_to_pass),
        "pass_to_pass": list(instance.pass_to_pass),
    }
    if policy.enable_memory:
        issue_context["memory_context"] = memory_context or []

    toolbox = AgentToolbox(
        repo_path=repo_path,
        repo_config=repo_config,
        issue_context=issue_context,
        task_context=swe_bench_task(
            {
                **issue_context,
                "problem_statement": instance.problem_statement,
            }
        ),
        sandbox_runner=LocalRunner(),
        runtime_policy=policy,
    )

    started = time.perf_counter()
    patch_apply_ok = False
    error = ""
    result = None
    retry_context: str | None = None
    attempts: list[dict[str, Any]] = []
    total_model_calls = 0
    total_tool_calls = 0
    total_input_tokens = 0
    total_output_tokens = 0
    test_result: dict[str, Any] = {
        "exit_code": -1,
        "stdout": "",
        "stderr": "",
        "failure_type": "agent_error",
    }

    for attempt_index in range(1, max_attempts + 1):
        attempt_started = time.perf_counter()
        attempt_result = None
        try:
            attempt_result = StagedAgentRuntime(policy=policy).run(
                toolbox,
                retry_context=retry_context,
            )
            result = attempt_result
        except InstanceTimeoutError:
            raise
        except Exception as exc:
            attempt_result = getattr(exc, "run_result", None)
            if attempt_result is not None:
                result = attempt_result
            else:
                total_model_calls += int(
                    getattr(exc, "model_call_count", 0)
                )
                total_tool_calls += int(
                    getattr(exc, "tool_call_count", 0)
                )
                total_input_tokens += int(
                    getattr(exc, "total_input_tokens", 0)
                )
                total_output_tokens += int(
                    getattr(exc, "total_output_tokens", 0)
                )
            error = str(exc)

        if attempt_result is not None:
            total_model_calls += attempt_result.model_call_count
            total_tool_calls += attempt_result.tool_call_count
            total_input_tokens += attempt_result.total_input_tokens
            total_output_tokens += attempt_result.total_output_tokens

        if error:
            attempts.append(
                {
                    "attempt": attempt_index,
                    "status": "agent_error",
                    "error": error,
                    "duration_ms": int(
                        (time.perf_counter() - attempt_started) * 1000
                    ),
                }
            )
            retry_context = json.dumps(
                {
                    "attempt": attempt_index,
                    "failure_type": "agent_error",
                    "error": error,
                    "current_diff": diff(repo_path)[-6000:],
                    "instruction": (
                        "Correct the failure and make a new repository edit. "
                        "Do not return success without a non-empty diff."
                    ),
                },
                ensure_ascii=False,
            )
            error = ""
            continue

        test_result = toolbox.run_tests()
        attempt_result.evidence_ledger = attach_verification(
            attempt_result.evidence_ledger,
            command=repo_config.test_command,
            exit_code=test_result["exit_code"],
            stdout=test_result.get("stdout", ""),
            stderr=test_result.get("stderr", ""),
        )
        current_diff = diff(repo_path)
        patch_apply_ok = bool(current_diff.strip())
        tests_pass = test_result["exit_code"] == 0
        attempts.append(
            {
                "attempt": attempt_index,
                "status": "success" if tests_pass and patch_apply_ok else "tests_failed",
                "test_exit_code": test_result["exit_code"],
                "failure_type": test_result.get("failure_type"),
                "duration_ms": int(
                    (time.perf_counter() - attempt_started) * 1000
                ),
            }
        )
        if tests_pass and patch_apply_ok:
            break

        retry_context = json.dumps(
            {
                "attempt": attempt_index,
                "failure_type": test_result.get("failure_type", "test_failure"),
                "test_exit_code": test_result["exit_code"],
                "stdout_tail": test_result.get("stdout", "")[-3000:],
                "stderr_tail": test_result.get("stderr", "")[-2000:],
                "current_diff": current_diff[-6000:],
                "instruction": (
                    "Inspect the failed assertions, revise the existing patch, "
                    "and run focused tests before returning."
                ),
            },
            ensure_ascii=False,
        )

    diff_text = diff(repo_path)
    if not diff_text.strip():
        patch_apply_ok = False
    stats = parse_diff_stats(diff_text)
    duration_ms = int((time.perf_counter() - started) * 1000)

    record: dict[str, Any] = {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "status": "success" if test_result["exit_code"] == 0 and patch_apply_ok else "failed",
        "patch_apply_ok": patch_apply_ok,
        "tests_pass": test_result["exit_code"] == 0,
        "test_exit_code": test_result["exit_code"],
        "stdout": test_result.get("stdout", ""),
        "stderr": test_result.get("stderr", ""),
        "patch": diff_text,
        "diff": diff_text,
        "files_changed_count": stats.files_changed_count,
        "diff_line_count": stats.diff_line_count,
        "model_call_count": total_model_calls,
        "tool_call_count": total_tool_calls,
        "duration_ms": duration_ms,
        "error": attempts[-1].get("error", "") if attempts else error,
        "runtime_variant": policy.name,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "route_decision": result.route_decision if result else None,
        "localization": result.localization if result else None,
        "plan": result.plan if result else None,
        "review": result.review if result else None,
        "agent_graph": result.agent_graph if result else None,
        "evidence_ledger": result.evidence_ledger if result else None,
        "runtime_events": result.runtime_events if result else None,
        "tournament": result.tournament if result else None,
        "recovery": result.recovery if result else None,
        "mutation_pressure_applied": (
            result.mutation_pressure_applied if result else False
        ),
        "summary": result.summary if result else None,
        "memory_items_recalled": len(memory_context or []),
        "attempt_count": len(attempts),
        "attempts": attempts,
    }
    record["swe_reward"] = score_patch_evaluation(record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CodeCairn on local SWE-style instances")
    parser.add_argument("--instances", required=True, help="SWE-bench-like JSON or JSONL with repo_path/repo_fixture")
    parser.add_argument("--output", required=True, help="JSONL output path")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--variant",
        choices=sorted(POLICY_PRESETS),
        default="full",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--per-instance-timeout", type=int, default=600)
    args = parser.parse_args()

    instances = load_instances(args.instances)
    if args.limit is not None:
        instances = instances[: args.limit]

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    run_root = output.parent / "swe_alignment_runs"
    policy = get_runtime_policy(args.variant)
    memory_bank = BenchmarkMemoryBank()
    rows = []
    for instance in instances:
        memory_context = memory_bank.recall(
            instance.repo,
            instance.problem_statement,
        )
        previous_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(max(1, args.per_instance_timeout))
        try:
            row = run_instance(
                instance,
                run_root / policy.name,
                policy=policy,
                memory_context=memory_context,
                max_attempts=max(1, args.max_attempts),
            )
        except InstanceTimeoutError as exc:
            row = {
                "instance_id": instance.instance_id,
                "repo": instance.repo,
                "base_commit": instance.base_commit,
                "status": "timeout",
                "patch_apply_ok": False,
                "tests_pass": False,
                "test_exit_code": -1,
                "patch": "",
                "diff": "",
                "files_changed_count": 0,
                "diff_line_count": 0,
                "model_call_count": 0,
                "tool_call_count": 0,
                "duration_ms": args.per_instance_timeout * 1000,
                "error": str(exc),
                "runtime_variant": policy.name,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "memory_items_recalled": len(memory_context),
                "attempt_count": 0,
                "attempts": [],
            }
            row["swe_reward"] = score_patch_evaluation(row)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
        rows.append(row)
        if policy.enable_memory:
            memory_bank.remember(instance.repo, row)
        with output.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps({k: row[k] for k in ["instance_id", "status", "test_exit_code", "swe_reward"]}, ensure_ascii=False))

    success = sum(1 for row in rows if row["status"] == "success")
    print(
        json.dumps(
            {
                "variant": policy.name,
                "total": len(rows),
                "success": success,
                "success_rate": success / len(rows) if rows else 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
