from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.swe_alignment.data import load_instances  # noqa: E402


def _completed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("instance_id"):
            completed.add(str(row["instance_id"]))
    return completed


def _append_failure(path: Path, instance, *, status: str, duration_ms: int, error: str, model_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "model_name_or_path": model_name,
        "model_patch": "",
        "patch_apply_ok": False,
        "status": status,
        "files_changed_count": 0,
        "diff_line_count": 0,
        "model_call_count": 0,
        "tool_call_count": 0,
        "duration_ms": duration_ms,
        "error": error,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Hard-timeout batch runner for SWE-bench Lite predictions")
    parser.add_argument("--instances", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--rollouts", required=True)
    parser.add_argument("--target-attempts", type=int, default=100)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--per-instance-timeout", type=int, default=150)
    parser.add_argument("--model-name", default="micro-swe-agent")
    parser.add_argument("--cache-dir", default=".cache/swebench/repos")
    parser.add_argument("--workspace-dir", default=".workspaces/swebench_lite")
    parser.add_argument("--variant", choices=["legacy", "retrieval", "memory", "full"], default=None)
    parser.add_argument("--memory-seed", default=None)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()

    instances = load_instances(args.instances)
    rollouts = Path(args.rollouts)
    predictions = Path(args.predictions)
    completed = _completed_ids(rollouts)

    attempted_before = len(completed)
    ran_this_batch = 0
    generated_before = sum(1 for line in predictions.read_text(encoding="utf-8").splitlines()) if predictions.exists() else 0

    for index, instance in enumerate(instances[args.start :], start=args.start):
        completed = _completed_ids(rollouts)
        if len(completed) >= args.target_attempts:
            break
        if instance.instance_id in completed:
            continue

        cmd = [
            sys.executable,
            "scripts/run_swebench_lite_predictions.py",
            "--instances",
            args.instances,
            "--predictions",
            args.predictions,
            "--rollouts",
            args.rollouts,
            "--start",
            str(index),
            "--limit",
            "1",
            "--cache-dir",
            args.cache_dir,
            "--workspace-dir",
            args.workspace_dir,
            "--model-name",
            args.model_name,
            "--per-instance-timeout",
            str(max(1, args.per_instance_timeout - 30)),
            "--max-attempts",
            str(max(1, args.max_attempts)),
        ]
        if args.variant:
            cmd.extend(["--variant", args.variant])
        if args.memory_seed:
            cmd.extend(["--memory-seed", args.memory_seed])
        started = time.perf_counter()
        try:
            completed_process = subprocess.run(
                cmd,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=args.per_instance_timeout,
                check=False,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            ran_this_batch += 1
            status = "ok" if completed_process.returncode == 0 else f"exit_{completed_process.returncode}"
            print(json.dumps({"index": index, "instance_id": instance.instance_id, "status": status, "elapsed_ms": elapsed_ms}))
            if completed_process.stdout.strip():
                print(completed_process.stdout.strip().splitlines()[-1])
            if completed_process.returncode != 0 and completed_process.stderr.strip():
                print(completed_process.stderr.strip().splitlines()[-1])
            if completed_process.returncode != 0 and instance.instance_id not in _completed_ids(rollouts):
                stderr_tail = completed_process.stderr.strip().splitlines()[-1] if completed_process.stderr.strip() else ""
                stdout_tail = completed_process.stdout.strip().splitlines()[-1] if completed_process.stdout.strip() else ""
                _append_failure(
                    rollouts,
                    instance,
                    status=f"exit_{completed_process.returncode}",
                    duration_ms=elapsed_ms,
                    error=stderr_tail or stdout_tail or f"subprocess_exit_{completed_process.returncode}",
                    model_name=args.model_name,
                )
        except subprocess.TimeoutExpired:
            ran_this_batch += 1
            # The child may have produced a rollout just before the parent hit
            # its timeout while waiting on stdout/stderr. Avoid adding a second
            # timeout record for the same instance in that race.
            if instance.instance_id not in _completed_ids(rollouts):
                _append_failure(
                    rollouts,
                    instance,
                    status="timeout",
                    duration_ms=args.per_instance_timeout * 1000,
                    error="subprocess_per_instance_timeout",
                    model_name=args.model_name,
                )
            print(json.dumps({"index": index, "instance_id": instance.instance_id, "status": "timeout", "elapsed_ms": args.per_instance_timeout * 1000}))

    completed = _completed_ids(rollouts)
    generated_after = sum(1 for line in predictions.read_text(encoding="utf-8").splitlines()) if predictions.exists() else 0
    print(
        json.dumps(
            {
                "attempted_before": attempted_before,
                "attempted_after": len(completed),
                "ran_this_batch": ran_this_batch,
                "generated_before": generated_before,
                "generated_after": generated_after,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
