"""SWE-style alignment utilities for coding-agent experiments."""

from experiments.swe_alignment.data import (
    build_dpo_pairs,
    build_sft_records,
    load_instances,
    load_patch_evaluations,
    write_jsonl,
)
from experiments.swe_alignment.reward import score_patch_evaluation
from experiments.swe_alignment.rollout import summarize_rollouts
from experiments.swe_alignment.schema import PatchEvaluation, SWEInstance

__all__ = [
    "PatchEvaluation",
    "SWEInstance",
    "build_dpo_pairs",
    "build_sft_records",
    "load_instances",
    "load_patch_evaluations",
    "score_patch_evaluation",
    "summarize_rollouts",
    "write_jsonl",
]
