from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any


def _numeric_value(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if key == "swe_reward" and isinstance(value, dict):
        value = value.get("reward")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def load_run(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        instance_id = str(row.get("instance_id") or "")
        if instance_id:
            rows[instance_id] = row
    return rows


def summarize(rows: dict[str, dict[str, Any]]) -> dict:
    values = list(rows.values())
    success = sum(row.get("status") == "success" for row in values)
    patch_apply = sum(
        bool(row.get("patch_apply_ok"))
        and bool(str(row.get("patch") or row.get("diff") or "").strip())
        for row in values
    )
    no_patch = sum(
        not str(row.get("patch") or row.get("diff") or "").strip()
        for row in values
    )

    def average(key: str) -> float:
        numbers = [_numeric_value(row, key) for row in values]
        return round(mean(numbers), 4) if numbers else 0.0

    return {
        "instances": len(values),
        "success": success,
        "success_rate": round(success / len(values), 4) if values else 0.0,
        "patch_apply_rate": (
            round(patch_apply / len(values), 4) if values else 0.0
        ),
        "no_patch_rate": (
            round(no_patch / len(values), 4) if values else 0.0
        ),
        "average_reward": average("swe_reward"),
        "average_model_calls": average("model_call_count"),
        "average_tool_calls": average("tool_call_count"),
        "average_input_tokens": average("total_input_tokens"),
        "average_output_tokens": average("total_output_tokens"),
        "average_duration_ms": average("duration_ms"),
    }


def paired_comparison(
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> dict:
    common = sorted(set(baseline) & set(candidate))
    wins = 0
    losses = 0
    ties = 0
    reward_deltas = []
    for instance_id in common:
        baseline_row = baseline[instance_id]
        candidate_row = candidate[instance_id]
        baseline_success = baseline_row.get("status") == "success"
        candidate_success = candidate_row.get("status") == "success"
        if candidate_success and not baseline_success:
            wins += 1
        elif baseline_success and not candidate_success:
            losses += 1
        else:
            ties += 1
        reward_deltas.append(
            _numeric_value(candidate_row, "swe_reward")
            - _numeric_value(baseline_row, "swe_reward")
        )
    return {
        "paired_instances": len(common),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "average_reward_delta": (
            round(mean(reward_deltas), 4) if reward_deltas else 0.0
        ),
    }


def compare_runs(runs: dict[str, dict[str, dict[str, Any]]]) -> dict:
    variants = list(runs)
    if not variants:
        return {"baseline": None, "summaries": {}, "paired_vs_baseline": {}}
    baseline_name = variants[0]
    return {
        "baseline": baseline_name,
        "summaries": {
            name: summarize(rows) for name, rows in runs.items()
        },
        "paired_vs_baseline": {
            name: paired_comparison(runs[baseline_name], rows)
            for name, rows in runs.items()
            if name != baseline_name
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare matched SWE rollout variants"
    )
    parser.add_argument(
        "--run",
        action="append",
        required=True,
        help="Variant and JSONL path as name=path; first run is the baseline",
    )
    parser.add_argument("--output")
    args = parser.parse_args()

    runs = {}
    for value in args.run:
        if "=" not in value:
            parser.error("--run must use name=path")
        name, path = value.split("=", 1)
        runs[name] = load_run(Path(path))
    comparison = compare_runs(runs)
    rendered = json.dumps(comparison, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
