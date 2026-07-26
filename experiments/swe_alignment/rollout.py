from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Iterable

from experiments.swe_alignment.reward import score_patch_evaluation
from experiments.swe_alignment.schema import PatchEvaluation


def summarize_rollouts(evaluations: Iterable[PatchEvaluation | dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evaluations:
        evaluation = PatchEvaluation.from_mapping(item) if isinstance(item, dict) else item
        score = score_patch_evaluation(evaluation)
        grouped[evaluation.instance_id].append(
            {
                "patch": evaluation.patch,
                "reward": score["reward"],
                "task_success": score["task_success"],
                "score": score,
            }
        )

    records = []
    for instance_id, samples in grouped.items():
        rewards = [sample["reward"] for sample in samples]
        best = max(samples, key=lambda sample: sample["reward"])
        mean_reward = statistics.fmean(rewards) if rewards else 0.0
        records.append(
            {
                "instance_id": instance_id,
                "num_samples": len(samples),
                "sample_success_rate": round(sum(float(sample["task_success"]) for sample in samples) / len(samples), 4),
                "best_of_n_success": bool(best["task_success"]),
                "mean_reward": round(mean_reward, 4),
                "best_reward": best["reward"],
                "reward_variance": round(statistics.pvariance(rewards), 4) if len(rewards) > 1 else 0.0,
            }
        )

    total_samples = sum(record["num_samples"] for record in records)
    total_groups = len(records)
    return {
        "num_instances": total_groups,
        "total_samples": total_samples,
        "sample_success_rate": round(
            sum(record["sample_success_rate"] * record["num_samples"] for record in records) / total_samples,
            4,
        ) if total_samples else 0.0,
        "best_of_n_success_rate": round(
            sum(float(record["best_of_n_success"]) for record in records) / total_groups,
            4,
        ) if total_groups else 0.0,
        "avg_best_reward": round(statistics.fmean(record["best_reward"] for record in records), 4) if records else 0.0,
        "records": records,
    }
