from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.orchestration import attach_verification  # noqa: E402
from experiments.swe_alignment.data import load_instances, write_jsonl  # noqa: E402
from experiments.swe_alignment.prompts import (  # noqa: E402
    build_empty_patch_completion,
    build_patch_completion,
    build_patch_completion_from_patch,
    build_swe_agent_prompt,
)


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    input_path = Path(path)
    if not input_path.exists():
        return []
    return [json.loads(line) for line in input_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _result_bucket(row: dict[str, Any], report: dict[str, Any]) -> str:
    instance_id = row["instance_id"]
    if instance_id in set(report.get("resolved_ids", [])):
        return "resolved"
    if instance_id in set(report.get("error_ids", [])):
        return "official_error"
    if row.get("model_patch") and not row.get("patch_apply_ok", True):
        return "local_patch_apply_failed"
    if row.get("model_patch"):
        return "unresolved"
    return "no_patch"


def _brief_error(row: dict[str, Any]) -> str:
    error = str(row.get("error") or "")
    if not error:
        return ""
    return error.replace("\n", " ")[:240]


def _generation_bucket(row: dict[str, Any]) -> str:
    if row.get("model_patch"):
        return "generated"
    error = str(row.get("error") or "")
    if row.get("status") == "timeout":
        return "timeout"
    if "no_repository_diff" in error:
        return "no_repository_diff"
    if "corrupt patch" in error:
        return "corrupt_patch"
    if "localization_gate_failed" in error:
        return "localization_gate_rejected"
    return "other_generation_failure"


def _make_summary(instances: list, rollouts: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any]:
    rollout_by_id = {row["instance_id"]: row for row in rollouts}
    buckets = Counter(_result_bucket(row, report) for row in rollouts)
    generation_buckets = Counter(_generation_bucket(row) for row in rollouts)
    total_duration_ms = sum(int(row.get("duration_ms") or 0) for row in rollouts)
    total_model_calls = sum(int(row.get("model_call_count") or 0) for row in rollouts)
    total_tool_calls = sum(int(row.get("tool_call_count") or 0) for row in rollouts)
    generated = [row for row in rollouts if row.get("model_patch")]
    valid_workspace_diff = [row for row in rollouts if row.get("workspace_diff")]
    completed = int(report.get("completed_instances", 0))
    resolved = int(report.get("resolved_instances", 0))
    return {
        "dataset": {
            "instances": len(instances),
            "rollouts": len(rollouts),
            "official_submitted": report.get("submitted_instances", 0),
            "official_completed": report.get("completed_instances", 0),
            "official_resolved": report.get("resolved_instances", 0),
            "official_unresolved": report.get("unresolved_instances", 0),
            "official_errors": report.get("error_instances", 0),
            "official_resolved_rate": round(resolved / completed, 4) if completed else 0,
            "end_to_end_resolved_rate": round(resolved / len(instances), 4) if instances else 0,
        },
        "generation": {
            "generated_patch_count": len(generated),
            "workspace_diff_count": len(valid_workspace_diff),
            "no_patch_count": buckets.get("no_patch", 0),
            "bucket_counts": dict(sorted(buckets.items())),
            "failure_bucket_counts": {
                key: value
                for key, value in sorted(generation_buckets.items())
                if key != "generated"
            },
            "total_duration_ms": total_duration_ms,
            "avg_duration_ms": round(total_duration_ms / len(rollouts), 2) if rollouts else 0,
            "generation_rate": round(len(generated) / len(rollouts), 4) if rollouts else 0,
            "avg_model_calls": round(total_model_calls / len(rollouts), 2) if rollouts else 0,
            "avg_tool_calls": round(total_tool_calls / len(rollouts), 2) if rollouts else 0,
        },
        "ids": {
            "resolved": report.get("resolved_ids", []),
            "official_error": report.get("error_ids", []),
            "no_patch": [row["instance_id"] for row in rollouts if not row.get("model_patch")],
            "local_patch_apply_failed": [
                row["instance_id"] for row in rollouts if row.get("model_patch") and not row.get("patch_apply_ok", True)
            ],
            "missing_rollout": [instance.instance_id for instance in instances if instance.instance_id not in rollout_by_id],
        },
        "failure_samples": [
            {
                "instance_id": row["instance_id"],
                "bucket": _result_bucket(row, report),
                "model_calls": row.get("model_call_count", 0),
                "tool_calls": row.get("tool_call_count", 0),
                "duration_ms": row.get("duration_ms", 0),
                "error": _brief_error(row),
            }
            for row in rollouts
            if _result_bucket(row, report) != "resolved"
        ],
    }


def _make_markdown(summary: dict[str, Any]) -> str:
    dataset = summary["dataset"]
    generation = summary["generation"]
    ids = summary["ids"]
    lines = [
        "# SWE-bench Lite Dev Alignment Preparation",
        "",
        "## Official Harness Result",
        "",
        f"- Total dev instances: {dataset['instances']}",
        f"- Submitted predictions: {dataset['official_submitted']}",
        f"- Completed by harness: {dataset['official_completed']}",
        f"- Resolved: {dataset['official_resolved']}",
        f"- Unresolved: {dataset['official_unresolved']}",
        f"- Official errors: {dataset['official_errors']}",
        f"- Resolved rate on completed predictions: {dataset['official_resolved_rate']:.1%}",
        f"- End-to-end resolved rate on the dataset: {dataset['end_to_end_resolved_rate']:.1%}",
        "",
        "## Rollout Generation",
        "",
        f"- Generated patch rows: {generation['generated_patch_count']}",
        f"- Valid workspace diff rows: {generation['workspace_diff_count']}",
        f"- No-patch rows: {generation['no_patch_count']}",
        f"- Patch generation rate: {generation['generation_rate']:.1%}",
        f"- Average rollout duration: {generation['avg_duration_ms']} ms",
        f"- Average model calls: {generation['avg_model_calls']}",
        f"- Average tool calls: {generation['avg_tool_calls']}",
        f"- Bucket counts: `{json.dumps(generation['bucket_counts'], ensure_ascii=False)}`",
        f"- Generation failure buckets: `{json.dumps(generation['failure_bucket_counts'], ensure_ascii=False)}`",
        "",
        "## Resolved IDs",
        "",
        *[f"- {item}" for item in ids["resolved"]],
        "",
    ]
    lines.extend(["## Main Optimization Targets", ""])
    if generation["no_patch_count"]:
        lines.append("- Reduce no-patch failures with stronger final-answer pressure.")
    if ids["local_patch_apply_failed"]:
        lines.append("- Repair invalid unified diffs before prediction export.")
    if dataset["official_unresolved"]:
        lines.append(
            "- Improve semantic correctness with targeted test discovery and test-guided reflection."
        )
    lines.extend(
        [
            "- Use resolved model patches as SFT examples and failed patches as preference negatives.",
            "",
        ]
    )
    return "\n".join(lines)


def build_alignment_artifacts(args: argparse.Namespace) -> dict[str, Any]:
    instances = load_instances(args.instances)
    instance_by_id = {instance.instance_id: instance for instance in instances}
    rollouts = _read_jsonl(args.rollouts)
    report = _read_json(args.report)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = _make_summary(instances, rollouts, report)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "summary.md").write_text(_make_markdown(summary), encoding="utf-8")

    gold_sft = [
        {
            "prompt": build_swe_agent_prompt(instance),
            "completion": build_patch_completion(instance),
            "meta": {
                "instance_id": instance.instance_id,
                "repo": instance.repo,
                "source": "official_gold_patch",
                "split": args.split_name,
            },
        }
        for instance in instances
        if instance.gold_patch
    ]

    resolved_ids = set(report.get("resolved_ids", []))
    evaluated_rollouts = []
    for row in rollouts:
        evaluated = dict(row)
        bucket = _result_bucket(row, report)
        is_resolved = row["instance_id"] in resolved_ids
        evaluated["official_bucket"] = bucket
        evaluated["official_resolved"] = is_resolved
        if row.get("evidence_ledger"):
            evaluated["evidence_ledger"] = attach_verification(
                row["evidence_ledger"],
                command="official_swebench_harness",
                exit_code=0 if is_resolved else 1,
                stderr="" if is_resolved else f"official_bucket:{bucket}",
            )
        evaluated_rollouts.append(evaluated)
    write_jsonl(output_dir / "evaluated_rollouts.jsonl", evaluated_rollouts)

    model_sft = []
    dpo_pairs = []
    for row in rollouts:
        instance = instance_by_id.get(row["instance_id"])
        if instance is None:
            continue
        prompt = build_swe_agent_prompt(instance)
        model_patch = str(row.get("model_patch") or "")
        if row["instance_id"] in resolved_ids and model_patch:
            model_sft.append(
                {
                    "prompt": prompt,
                    "completion": build_patch_completion_from_patch(instance, model_patch, source="resolved_model_patch"),
                    "meta": {
                        "instance_id": instance.instance_id,
                        "repo": instance.repo,
                        "source": "resolved_model_patch",
                        "split": args.split_name,
                    },
                }
            )
            dpo_pairs.append(
                {
                    "prompt": prompt,
                    "chosen": build_patch_completion_from_patch(instance, model_patch, source="resolved_model_patch"),
                    "rejected": build_empty_patch_completion(instance),
                    "meta": {
                        "instance_id": instance.instance_id,
                        "repo": instance.repo,
                        "preference": "resolved_model_patch_over_empty_patch",
                        "split": args.split_name,
                    },
                }
            )
        elif instance.gold_patch:
            rejected = (
                build_patch_completion_from_patch(instance, model_patch, source="failed_model_patch")
                if model_patch
                else build_empty_patch_completion(instance)
            )
            dpo_pairs.append(
                {
                    "prompt": prompt,
                    "chosen": build_patch_completion(instance),
                    "rejected": rejected,
                    "meta": {
                        "instance_id": instance.instance_id,
                        "repo": instance.repo,
                        "preference": "gold_patch_over_failed_model_output",
                        "failure_bucket": _result_bucket(row, report),
                        "split": args.split_name,
                    },
                }
            )

    write_jsonl(output_dir / "sft_gold.jsonl", gold_sft)
    write_jsonl(output_dir / "sft_resolved_model.jsonl", model_sft)
    write_jsonl(output_dir / "dpo_gold_vs_model.jsonl", dpo_pairs)

    return {
        "output_dir": str(output_dir),
        "summary": str(output_dir / "summary.json"),
        "summary_md": str(output_dir / "summary.md"),
        "evaluated_rollouts": str(output_dir / "evaluated_rollouts.jsonl"),
        "sft_gold_records": len(gold_sft),
        "sft_resolved_model_records": len(model_sft),
        "dpo_pairs": len(dpo_pairs),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare SFT/DPO artifacts from SWE-bench rollout and official report")
    parser.add_argument("--instances", required=True)
    parser.add_argument("--rollouts", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--split-name", default="swebench_lite_dev")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = build_alignment_artifacts(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
