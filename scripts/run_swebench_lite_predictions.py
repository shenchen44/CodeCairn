from __future__ import annotations

import argparse
import json
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.openai.agent_loop import AgentLoop, extract_json_object  # noqa: E402
from app.services.openai.policy import POLICY_PRESETS, get_runtime_policy  # noqa: E402
from app.services.openai.staged_runtime import StagedAgentRuntime  # noqa: E402
from app.services.openai.tools import AgentToolbox, ToolExecutionError  # noqa: E402
from app.services.sandbox.git_ops import diff  # noqa: E402
from app.services.sandbox.git_ops import run_git  # noqa: E402
from app.services.sandbox.limits import parse_diff_stats  # noqa: E402
from app.services.sandbox.repo_config import RepoConfig  # noqa: E402
from experiments.swe_alignment.data import load_instances  # noqa: E402
from experiments.swe_alignment.memory import (  # noqa: E402
    load_memory_seed,
    recall_verified_memories,
)
from experiments.swe_alignment.schema import SWEInstance  # noqa: E402
from app.services.adapters import swe_bench_task  # noqa: E402


class InstanceTimeoutError(TimeoutError):
    pass


def _timeout_handler(signum, frame):  # noqa: ANN001
    raise InstanceTimeoutError("per_instance_timeout_reached")


class DeferredTestRunner:
    """Prediction mode runner.

    SWE-bench official scoring happens later in Docker via the harness. During
    patch generation, tests are treated as deferred so the agent can still
    complete a patch even when the local machine lacks each repo's environment.
    """

    def run_tests(self, repo_path: Path, test_command: str):
        return type(
            "Result",
            (),
            {
                "exit_code": 0,
                "stdout": "SWE-bench prediction mode: tests deferred to official harness.",
                "stderr": "",
            },
        )()


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)


def _repo_cache_path(cache_dir: Path, repo: str) -> Path:
    return cache_dir / repo.replace("/", "__")


def _ensure_repo_cache(repo: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = _repo_cache_path(cache_dir, repo)
    if cached.exists():
        _run(["git", "fetch", "--all", "--tags", "--prune"], cwd=cached, check=False)
        return cached
    clone_url = f"https://github.com/{repo}.git"
    _run(["git", "clone", clone_url, str(cached)])
    return cached


def _prepare_workspace(instance: SWEInstance, cache_dir: Path, workspace_dir: Path) -> Path:
    if not instance.repo:
        raise ValueError(f"missing_repo_for_instance:{instance.instance_id}")
    cached = _ensure_repo_cache(instance.repo, cache_dir)
    target = workspace_dir / instance.instance_id.replace("/", "__")
    if target.exists():
        shutil.rmtree(target)
    _run(["git", "clone", "--shared", str(cached), str(target)])
    _run(["git", "checkout", instance.base_commit], cwd=target)
    _run(["git", "reset", "--hard"], cwd=target)
    _run(["git", "clean", "-fdx"], cwd=target)
    return target


def _repo_config_for_swebench() -> RepoConfig:
    return RepoConfig(
        language="python",
        test_command="pytest -q",
        install_command="python -m pip install -r requirements.txt",
        allowed_paths=[""],
        blocked_paths=[".git/", ".github/workflows/", "infra/", "deploy/"],
        max_changed_files=25,
        max_diff_lines=2500,
    )


def _issue_context(instance: SWEInstance) -> dict[str, Any]:
    return {
        "title": instance.problem_statement.strip().splitlines()[0][:160] or instance.instance_id,
        "body": instance.problem_statement,
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "hints_text": instance.meta.get("hints_text", ""),
        "fail_to_pass": list(instance.fail_to_pass),
        "pass_to_pass": list(instance.pass_to_pass),
        "evaluation_note": "Generate a patch only. Official SWE-bench harness will apply tests later.",
    }


def _reset_workspace(repo_path: Path) -> None:
    run_git(repo_path, "reset", "--hard")
    run_git(repo_path, "clean", "-fdx")


def _patch_failure_context(instance: SWEInstance, patch_text: str, error: str) -> str:
    return (
        "The previous attempt produced an invalid patch for this SWE-bench task. "
        "Do not return full file contents or prose. Produce a minimal valid unified diff only inside patch_text.\n\n"
        f"instance_id: {instance.instance_id}\n"
        f"git_apply_error: {error[:1200]}\n\n"
        f"invalid_patch_text:\n{patch_text[:6000]}"
    )


def _no_patch_retry_context(instance: SWEInstance) -> str:
    return (
        "The previous attempt did not produce any patch. Reflect on the issue, identify the most likely file and "
        "function from the repository evidence, then return a strict final JSON with a minimal unified diff in "
        "patch_text. Avoid ending with an empty patch unless the issue is impossible.\n\n"
        f"instance_id: {instance.instance_id}\n"
        f"problem_statement:\n{instance.problem_statement[:4000]}"
    )


def _localization_gate_context(instance: SWEInstance) -> str:
    return (
        "Before generating any patch, perform structured localization only. "
        "Use repository tools to identify the smallest likely edit target, but do not modify files. "
        "Return strict JSON with the normal final-answer keys. Keep patch_text empty. Put this exact object in summary:\n"
        "{\n"
        '  "suspect_files": ["relative/path.py"],\n'
        '  "target_symbols": ["ClassOrFunction"],\n'
        '  "root_cause": "one concise sentence",\n'
        '  "edit_plan": ["specific edit step"],\n'
        '  "patch_strategy": "minimal strategy",\n'
        '  "confidence": 0.0\n'
        "}\n\n"
        f"instance_id: {instance.instance_id}\n"
        f"problem_statement:\n{instance.problem_statement[:4000]}"
    )


def _normalize_plan(summary: dict[str, Any], pr_body_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    source = summary if isinstance(summary, dict) else {}
    body = pr_body_summary if isinstance(pr_body_summary, dict) else {}
    if not source.get("suspect_files") and isinstance(body.get("suspect_files"), list):
        source = {**body, **source}
    return {
        "suspect_files": [str(item) for item in source.get("suspect_files", []) if str(item).strip()][:8],
        "target_symbols": [str(item) for item in source.get("target_symbols", []) if str(item).strip()][:8],
        "root_cause": str(source.get("root_cause") or ""),
        "edit_plan": [str(item) for item in source.get("edit_plan", []) if str(item).strip()][:8],
        "patch_strategy": str(source.get("patch_strategy") or ""),
        "confidence": source.get("confidence", 0),
    }


def _plan_is_actionable(plan: dict[str, Any]) -> bool:
    return bool(plan.get("suspect_files")) and bool(plan.get("edit_plan") or plan.get("target_symbols"))


def _patch_generation_context(instance: SWEInstance, plan: dict[str, Any]) -> str:
    return (
        "Use the structured localization plan below as a hard edit gate. "
        "First inspect the listed suspect files/symbols if needed, then produce the smallest valid unified diff in patch_text. "
        "Do not continue broad repository search unless the plan is clearly contradicted by file contents.\n\n"
        f"instance_id: {instance.instance_id}\n"
        f"structured_localization_plan:\n{json.dumps(plan, ensure_ascii=False, indent=2)}"
    )


def _file_excerpt_for_plan(repo_path: Path, plan: dict[str, Any], *, max_files: int = 3, max_chars_per_file: int = 12000) -> str:
    sections = []
    target_symbols = [str(item) for item in plan.get("target_symbols", [])]
    for rel_path in list(plan.get("suspect_files", []))[:max_files]:
        path = (repo_path / str(rel_path)).resolve()
        try:
            path.relative_to(repo_path.resolve())
        except ValueError:
            continue
        if not path.exists() or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        hit_line = None
        for symbol in target_symbols:
            needle = symbol.split(".")[-1]
            for index, line in enumerate(lines):
                if needle and needle in line:
                    hit_line = index
                    break
            if hit_line is not None:
                break

        if hit_line is None:
            start = 0
            end = min(len(lines), 220)
        else:
            start = max(0, hit_line - 90)
            end = min(len(lines), hit_line + 140)
        excerpt = "\n".join(f"{i + 1:4d} | {lines[i]}" for i in range(start, end))
        if len(excerpt) > max_chars_per_file:
            excerpt = excerpt[:max_chars_per_file]
        sections.append(f"### {rel_path} lines {start + 1}-{end}\n{excerpt}")
    return "\n\n".join(sections)


def _force_patch_from_plan(
    agent: AgentLoop,
    instance: SWEInstance,
    repo_path: Path,
    plan: dict[str, Any],
) -> tuple[Any, str]:
    file_context = _file_excerpt_for_plan(repo_path, plan)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a precise SWE-bench patch generator. Return only strict JSON. "
                "Do not call tools. Do not use markdown. patch_text must be a valid unified diff with diff --git headers."
            ),
        },
        {
            "role": "user",
            "content": (
                "Generate the smallest patch for this issue using the structured plan and file excerpts. "
                "If uncertain, still make the minimal edit most directly implied by the plan. "
                "Return JSON with keys summary, patch_text, pr_title, pr_body_summary.\n\n"
                f"instance_id: {instance.instance_id}\n"
                f"problem_statement:\n{instance.problem_statement[:3000]}\n\n"
                f"structured_localization_plan:\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
                f"file_excerpts:\n{file_context}"
            ),
        },
    ]
    response = agent.client.create_completion(messages=messages, tools=[])
    payload = response.choices[0].message.content or ""
    try:
        parsed = extract_json_object(payload)
    except Exception:
        return None, ""
    return parsed, str(parsed.get("patch_text") or "")


def _run_localization_gate(
    agent: AgentLoop,
    instance: SWEInstance,
    toolbox: AgentToolbox,
    repo_path: Path,
) -> tuple[dict[str, Any], int, int, dict[str, Any]]:
    result = agent.run(toolbox, retry_context=_localization_gate_context(instance))
    _reset_workspace(repo_path)
    plan = _normalize_plan(result.summary, result.pr_body_summary)
    metadata = {
        "actionable": _plan_is_actionable(plan),
        "raw_summary": result.summary,
        "raw_patch_chars": len(result.patch_text or ""),
    }
    return plan, result.model_call_count, result.tool_call_count, metadata


def _apply_candidate_patch(toolbox: AgentToolbox, patch_text: str) -> tuple[bool, str]:
    if not patch_text.strip():
        return False, "empty_patch_text"
    try:
        toolbox.apply_patch(patch_text)
    except Exception as exc:
        return False, str(exc)
    return True, ""


def _repair_invalid_patch(
    agent: AgentLoop,
    instance: SWEInstance,
    toolbox: AgentToolbox,
    repo_path: Path,
    patch_text: str,
    error: str,
    attempts: list[dict[str, Any]],
) -> tuple[Any, str, str, bool]:
    _reset_workspace(repo_path)
    repair = agent.run(toolbox, retry_context=_patch_failure_context(instance, patch_text, error))
    repair_patch = repair.patch_text or ""
    attempts.append({"kind": "patch_repair", "patch_chars": len(repair_patch)})
    applied, repair_error = _apply_candidate_patch(toolbox, repair_patch)
    if applied:
        return repair, repair_patch, "", True
    attempts[-1]["apply_error"] = repair_error[:500]
    _reset_workspace(repo_path)
    return repair, repair_patch or patch_text, repair_error or error, False


def _run_agent_with_retry(
    instance: SWEInstance,
    toolbox: AgentToolbox,
    repo_path: Path,
    *,
    use_localization_gate: bool = True,
) -> tuple[Any | None, str, str, bool, int, int, list[dict[str, Any]]]:
    agent = AgentLoop()
    attempts: list[dict[str, Any]] = []
    total_model_calls = 0
    total_tool_calls = 0
    plan: dict[str, Any] = {}

    if use_localization_gate:
        plan, plan_model_calls, plan_tool_calls, plan_metadata = _run_localization_gate(agent, instance, toolbox, repo_path)
        total_model_calls += plan_model_calls
        total_tool_calls += plan_tool_calls
        attempts.append({"kind": "localization_gate", "plan": plan, **plan_metadata})

    result = agent.run(toolbox, retry_context=_patch_generation_context(instance, plan) if plan else None)
    total_model_calls += result.model_call_count
    total_tool_calls += result.tool_call_count
    attempted_patch = result.patch_text or ""
    attempts.append({"kind": "initial", "patch_chars": len(attempted_patch)})

    if attempted_patch:
        applied, error = _apply_candidate_patch(toolbox, attempted_patch)
        if applied:
            return result, attempted_patch, "", True, total_model_calls, total_tool_calls, attempts

        attempts[-1]["apply_error"] = error[:500]
        repair, repair_patch, repair_error, applied = _repair_invalid_patch(
            agent,
            instance,
            toolbox,
            repo_path,
            attempted_patch,
            error,
            attempts,
        )
        total_model_calls += repair.model_call_count
        total_tool_calls += repair.tool_call_count
        if applied:
            return repair, repair_patch, "", True, total_model_calls, total_tool_calls, attempts
        return repair, repair_patch or attempted_patch, repair_error or error, False, total_model_calls, total_tool_calls, attempts

    retry = agent.run(toolbox, retry_context=_no_patch_retry_context(instance))
    total_model_calls += retry.model_call_count
    total_tool_calls += retry.tool_call_count
    retry_patch = retry.patch_text or ""
    attempts.append({"kind": "no_patch_retry", "patch_chars": len(retry_patch)})
    applied, retry_error = _apply_candidate_patch(toolbox, retry_patch)
    if applied:
        return retry, retry_patch, "", True, total_model_calls, total_tool_calls, attempts
    if retry_patch:
        attempts[-1]["apply_error"] = retry_error[:500]
        repair, repair_patch, repair_error, applied = _repair_invalid_patch(
            agent,
            instance,
            toolbox,
            repo_path,
            retry_patch,
            retry_error,
            attempts,
        )
        total_model_calls += repair.model_call_count
        total_tool_calls += repair.tool_call_count
        if applied:
            return repair, repair_patch, "", True, total_model_calls, total_tool_calls, attempts
        return repair, repair_patch or retry_patch, repair_error or retry_error, False, total_model_calls, total_tool_calls, attempts
    if plan and _plan_is_actionable(plan):
        forced_result, forced_patch = _force_patch_from_plan(agent, instance, repo_path, plan)
        total_model_calls += 1
        attempts.append({"kind": "plan_forced_patch", "patch_chars": len(forced_patch)})
        applied, forced_error = _apply_candidate_patch(toolbox, forced_patch)
        if applied:
            return retry, forced_patch, "", True, total_model_calls, total_tool_calls, attempts
        if forced_patch:
            attempts[-1]["apply_error"] = forced_error[:500]
            repair, repair_patch, repair_error, applied = _repair_invalid_patch(
                agent,
                instance,
                toolbox,
                repo_path,
                forced_patch,
                forced_error,
                attempts,
            )
            total_model_calls += repair.model_call_count
            total_tool_calls += repair.tool_call_count
            if applied:
                return repair, repair_patch, "", True, total_model_calls, total_tool_calls, attempts
            return repair, repair_patch or forced_patch, repair_error or forced_error, False, total_model_calls, total_tool_calls, attempts
    _reset_workspace(repo_path)
    return retry, retry_patch or attempted_patch, retry_error, False, total_model_calls, total_tool_calls, attempts


def run_instance(
    instance: SWEInstance,
    *,
    cache_dir: Path,
    workspace_dir: Path,
    model_name: str,
    use_localization_gate: bool = True,
    variant: str | None = None,
    max_attempts: int = 3,
    memory_context: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    repo_path = _prepare_workspace(instance, cache_dir, workspace_dir)
    policy = get_runtime_policy(variant) if variant else None
    legacy_context = _issue_context(instance)
    recalled_memory = (
        list(memory_context or [])
        if policy is not None and policy.enable_memory
        else []
    )
    if recalled_memory:
        legacy_context["memory_context"] = recalled_memory
    toolbox = AgentToolbox(
        repo_path=repo_path,
        repo_config=_repo_config_for_swebench(),
        issue_context=legacy_context,
        task_context=swe_bench_task(
            {
                **legacy_context,
                "problem_statement": instance.problem_statement,
            }
        ),
        sandbox_runner=DeferredTestRunner(),
        runtime_policy=policy,
    )

    patch_apply_ok = True
    error = ""
    result = None
    attempted_patch_text = ""
    model_call_count = 0
    tool_call_count = 0
    repair_attempts: list[dict[str, Any]] = []
    total_input_tokens = 0
    total_output_tokens = 0
    if policy is not None:
        patch_apply_ok = False
        retry_context: str | None = None
        for attempt_index in range(1, max(1, max_attempts) + 1):
            attempt_started = time.perf_counter()
            attempt_result = None
            try:
                attempt_result = StagedAgentRuntime(policy=policy).run(
                    toolbox,
                    retry_context=retry_context,
                )
                result = attempt_result
                attempted_patch_text = attempt_result.patch_text
                model_call_count += attempt_result.model_call_count
                tool_call_count += attempt_result.tool_call_count
                total_input_tokens += attempt_result.total_input_tokens
                total_output_tokens += attempt_result.total_output_tokens
                patch_apply_ok = bool(diff(repo_path).strip())
                repair_attempts.append(
                    {
                        "kind": "staged_runtime",
                        "attempt": attempt_index,
                        "status": "generated" if patch_apply_ok else "no_patch",
                        "duration_ms": int(
                            (time.perf_counter() - attempt_started) * 1000
                        ),
                    }
                )
                if patch_apply_ok:
                    error = ""
                    break
            except Exception as exc:
                attempt_result = getattr(exc, "run_result", None)
                if attempt_result is not None:
                    result = attempt_result
                else:
                    model_call_count += int(
                        getattr(exc, "model_call_count", 0)
                    )
                    tool_call_count += int(
                        getattr(exc, "tool_call_count", 0)
                    )
                    total_input_tokens += int(
                        getattr(exc, "total_input_tokens", 0)
                    )
                    total_output_tokens += int(
                        getattr(exc, "total_output_tokens", 0)
                    )
                error = str(exc)
                repair_attempts.append(
                    {
                        "kind": "staged_runtime",
                        "attempt": attempt_index,
                        "status": "agent_error",
                        "error": error,
                        "phase": getattr(exc, "phase", None),
                        "localization": getattr(exc, "localization", None),
                        "phase_result": getattr(exc, "phase_result", None),
                        "gate_reasons": getattr(exc, "reasons", None),
                        "raw_response": getattr(exc, "raw_response", "")[
                            :8000
                        ],
                        "duration_ms": int(
                            (time.perf_counter() - attempt_started) * 1000
                        ),
                    }
                )
            if attempt_result is not None:
                model_call_count += attempt_result.model_call_count
                tool_call_count += attempt_result.tool_call_count
                total_input_tokens += attempt_result.total_input_tokens
                total_output_tokens += attempt_result.total_output_tokens
            retry_context = json.dumps(
                {
                    "attempt": attempt_index,
                    "failure_type": "prediction_generation_error",
                    "error": error,
                    "current_diff": diff(repo_path)[-6000:],
                    "instruction": (
                        "Reflect on the failure and make a new concrete repository "
                        "edit that produces a valid non-empty patch."
                    ),
                },
                ensure_ascii=False,
            )
    else:
        try:
            result, attempted_patch_text, error, patch_apply_ok, model_call_count, tool_call_count, repair_attempts = (
                _run_agent_with_retry(instance, toolbox, repo_path, use_localization_gate=use_localization_gate)
            )
        except ToolExecutionError as exc:
            patch_apply_ok = False
            error = str(exc)
        except Exception as exc:
            patch_apply_ok = False
            error = str(exc)

    workspace_diff = diff(repo_path)
    model_patch = workspace_diff
    stats = parse_diff_stats(model_patch)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "model_name_or_path": model_name,
        "model_patch": model_patch,
        "workspace_diff": workspace_diff,
        "attempted_patch_text": attempted_patch_text,
        "patch_apply_ok": patch_apply_ok,
        "status": "generated" if model_patch and patch_apply_ok else "failed",
        "files_changed_count": stats.files_changed_count,
        "diff_line_count": stats.diff_line_count,
        "model_call_count": model_call_count or (result.model_call_count if result else 0),
        "tool_call_count": tool_call_count or (result.tool_call_count if result else 0),
        "repair_attempts": repair_attempts,
        "localization_gate_enabled": use_localization_gate,
        "runtime_variant": policy.name if policy else "legacy_prediction_runner",
        "memory_items_recalled": len(recalled_memory),
        "memory_source_ids": [
            str(item.get("source_instance_id") or "")
            for item in recalled_memory
        ],
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "route_decision": result.route_decision if policy and result else None,
        "localization": result.localization if policy and result else None,
        "plan": result.plan if policy and result else None,
        "review": result.review if policy and result else None,
        "agent_graph": result.agent_graph if policy and result else None,
        "evidence_ledger": (
            result.evidence_ledger if policy and result else None
        ),
        "runtime_events": (
            result.runtime_events if policy and result else None
        ),
        "tournament": result.tournament if policy and result else None,
        "recovery": result.recovery if policy and result else None,
        "mutation_pressure_applied": (
            result.mutation_pressure_applied
            if policy and result
            else False
        ),
        "duration_ms": duration_ms,
        "error": error,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SWE-bench Lite prediction patches with micro-swe-agent")
    parser.add_argument("--instances", required=True, help="SWE-bench Lite JSONL fetched by scripts/fetch_swebench_lite.py")
    parser.add_argument("--predictions", required=True, help="Official SWE-bench predictions JSONL output")
    parser.add_argument("--rollouts", required=True, help="Detailed rollout JSONL output")
    parser.add_argument("--cache-dir", default=".cache/swebench/repos")
    parser.add_argument("--workspace-dir", default=".workspaces/swebench_lite")
    parser.add_argument("--model-name", default="micro-swe-agent")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--resume", action="store_true", help="Skip instances already present in the rollout file")
    parser.add_argument("--skip-localization-gate", action="store_true", help="Disable structured localization before patch generation")
    parser.add_argument("--variant", choices=sorted(POLICY_PRESETS), default=None)
    parser.add_argument(
        "--memory-seed",
        default=None,
        help="Verified repository memories produced from an earlier split",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--stop-after-seconds", type=int, default=None, help="Stop cleanly after this many seconds")
    parser.add_argument("--per-instance-timeout", type=int, default=600, help="Hard timeout in seconds for one instance")
    args = parser.parse_args()

    instances = load_instances(args.instances)
    memory_seed = load_memory_seed(args.memory_seed)
    instances = instances[args.start :]
    if args.limit is not None:
        instances = instances[: args.limit]

    predictions_path = Path(args.predictions)
    rollouts_path = Path(args.rollouts)
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    rollouts_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir)
    workspace_dir = Path(args.workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    completed_ids: set[str] = set()
    if args.resume and rollouts_path.exists():
        for line in rollouts_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("instance_id"):
                completed_ids.add(str(row["instance_id"]))
        instances = [instance for instance in instances if instance.instance_id not in completed_ids]

    started = time.perf_counter()
    success = 0
    with predictions_path.open("a", encoding="utf-8") as pred_f, rollouts_path.open("a", encoding="utf-8") as rollout_f:
        for instance in instances:
            if args.stop_after_seconds is not None and time.perf_counter() - started >= args.stop_after_seconds:
                print(json.dumps({"stopped": "time_budget_reached", "completed_before_run": len(completed_ids)}, indent=2))
                break
            try:
                previous_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(args.per_instance_timeout)
                row = run_instance(
                    instance,
                    cache_dir=cache_dir,
                    workspace_dir=workspace_dir,
                    model_name=args.model_name,
                    use_localization_gate=not args.skip_localization_gate,
                    variant=args.variant,
                    max_attempts=args.max_attempts,
                    memory_context=recall_verified_memories(
                        memory_seed,
                        repo=instance.repo,
                        instance_id=instance.instance_id,
                        query=instance.problem_statement,
                    ),
                )
            except InstanceTimeoutError as exc:
                row = {
                    "instance_id": instance.instance_id,
                    "repo": instance.repo,
                    "base_commit": instance.base_commit,
                    "model_name_or_path": args.model_name,
                    "model_patch": "",
                    "patch_apply_ok": False,
                    "status": "timeout",
                    "files_changed_count": 0,
                    "diff_line_count": 0,
                    "model_call_count": 0,
                    "tool_call_count": 0,
                    "memory_items_recalled": 0,
                    "memory_source_ids": [],
                    "duration_ms": args.per_instance_timeout * 1000,
                    "error": str(exc),
                }
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous_handler)
            rollout_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            rollout_f.flush()
            if row["model_patch"]:
                pred_f.write(
                    json.dumps(
                        {
                            "instance_id": row["instance_id"],
                            "model_name_or_path": row["model_name_or_path"],
                            "model_patch": row["model_patch"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                pred_f.flush()
            success += int(row["status"] == "generated")
            print(json.dumps({key: row[key] for key in ["instance_id", "status", "files_changed_count", "diff_line_count", "duration_ms", "error"]}, ensure_ascii=False))

    print(
        json.dumps(
            {
                "completed_before_run": len(completed_ids),
                "attempted_this_run": len(instances),
                "generated_this_run": success,
                "generation_rate_this_run": success / len(instances) if instances else 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
