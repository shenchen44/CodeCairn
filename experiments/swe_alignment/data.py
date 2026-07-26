from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from experiments.swe_alignment.prompts import build_patch_completion, build_swe_agent_prompt
from experiments.swe_alignment.reward import score_patch_evaluation
from experiments.swe_alignment.schema import PatchEvaluation, SWEInstance


def _read_json_or_jsonl(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    if isinstance(payload, dict) and isinstance(payload.get("instances"), list):
        return payload["instances"]
    if isinstance(payload, dict):
        return [payload]
    raise ValueError(f"Unsupported JSON payload in {path}")


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_instances(path: str | Path) -> list[SWEInstance]:
    return [SWEInstance.from_mapping(row) for row in _read_json_or_jsonl(Path(path))]


def load_patch_evaluations(path: str | Path) -> list[PatchEvaluation]:
    return [PatchEvaluation.from_mapping(row) for row in _read_json_or_jsonl(Path(path))]


def build_sft_records(instances: Iterable[SWEInstance]) -> list[dict[str, Any]]:
    records = []
    for instance in instances:
        if not instance.gold_patch:
            continue
        records.append(
            {
                "prompt": build_swe_agent_prompt(instance),
                "completion": build_patch_completion(instance),
                "meta": {
                    "instance_id": instance.instance_id,
                    "repo": instance.repo,
                    "base_commit": instance.base_commit,
                    "test_command": instance.test_command,
                    "source": "swe_alignment_gold_patch",
                },
            }
        )
    return records


def build_dpo_pairs(evaluations: Iterable[PatchEvaluation]) -> list[dict[str, Any]]:
    grouped: dict[str, list[PatchEvaluation]] = defaultdict(list)
    for evaluation in evaluations:
        grouped[evaluation.instance_id].append(evaluation)

    pairs = []
    for instance_id, items in grouped.items():
        if len(items) < 2:
            continue
        scored = [(item, score_patch_evaluation(item)["reward"]) for item in items]
        chosen, chosen_reward = max(scored, key=lambda item: item[1])
        rejected, rejected_reward = min(scored, key=lambda item: item[1])
        if chosen_reward <= rejected_reward or not chosen.patch or not rejected.patch:
            continue
        pairs.append(
            {
                "prompt": f"Fix SWE task {instance_id}. Return the minimal safe patch.",
                "chosen": chosen.patch,
                "rejected": rejected.patch,
                "meta": {
                    "instance_id": instance_id,
                    "chosen_reward": chosen_reward,
                    "rejected_reward": rejected_reward,
                    "source": "swe_alignment_patch_reward",
                },
            }
        )
    return pairs
