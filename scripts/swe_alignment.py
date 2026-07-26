from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.swe_alignment.data import (  # noqa: E402
    build_dpo_pairs,
    build_sft_records,
    load_instances,
    load_patch_evaluations,
    write_jsonl,
)
from experiments.swe_alignment.reward import score_patch_evaluation  # noqa: E402
from experiments.swe_alignment.rollout import summarize_rollouts  # noqa: E402


def cmd_make_sft(args: argparse.Namespace) -> None:
    instances = load_instances(args.instances)
    records = build_sft_records(instances)
    write_jsonl(args.output, records)
    print(json.dumps({"instances": len(instances), "sft_records": len(records), "output": args.output}, indent=2))


def cmd_score(args: argparse.Namespace) -> None:
    evaluations = load_patch_evaluations(args.results)
    rows = []
    for evaluation in evaluations:
        row = dict(evaluation.meta)
        row["swe_reward"] = score_patch_evaluation(evaluation)
        rows.append(row)
    write_jsonl(args.output, rows)
    print(json.dumps({"evaluations": len(evaluations), "output": args.output}, indent=2))


def cmd_make_dpo(args: argparse.Namespace) -> None:
    evaluations = load_patch_evaluations(args.results)
    pairs = build_dpo_pairs(evaluations)
    write_jsonl(args.output, pairs)
    print(json.dumps({"evaluations": len(evaluations), "dpo_pairs": len(pairs), "output": args.output}, indent=2))


def cmd_rollout_summary(args: argparse.Namespace) -> None:
    evaluations = load_patch_evaluations(args.results)
    summary = summarize_rollouts(evaluations)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    compact = {key: value for key, value in summary.items() if key != "records"}
    print(json.dumps(compact, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SWE-style alignment utilities for CodeCairn")
    subparsers = parser.add_subparsers(dest="command", required=True)

    make_sft = subparsers.add_parser("make-sft", help="Generate completion SFT records from SWE-bench-like instances")
    make_sft.add_argument("--instances", required=True)
    make_sft.add_argument("--output", required=True)
    make_sft.set_defaults(func=cmd_make_sft)

    score = subparsers.add_parser("score", help="Attach shaped SWE rewards to patch evaluation results")
    score.add_argument("--results", required=True)
    score.add_argument("--output", required=True)
    score.set_defaults(func=cmd_score)

    make_dpo = subparsers.add_parser("make-dpo", help="Generate DPO pairs from multiple patch candidates per task")
    make_dpo.add_argument("--results", required=True)
    make_dpo.add_argument("--output", required=True)
    make_dpo.set_defaults(func=cmd_make_dpo)

    rollout = subparsers.add_parser("rollout-summary", help="Summarize best-of-N / GRPO-style grouped rollouts")
    rollout.add_argument("--results", required=True)
    rollout.add_argument("--output", required=True)
    rollout.set_defaults(func=cmd_rollout_summary)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
