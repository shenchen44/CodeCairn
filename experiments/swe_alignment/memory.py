from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from app.services.retrieval.hybrid import tokenize


def verified_memory_from_rollout(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("official_resolved") is not True:
        return None
    localization = row.get("localization")
    if not isinstance(localization, dict):
        localization = {}
    return {
        "source_instance_id": str(row.get("instance_id") or ""),
        "repo": str(row.get("repo") or ""),
        "kind": "verified_solution",
        "content": {
            "issue_summary": localization.get("issue_summary"),
            "root_cause": localization.get("root_cause_hypothesis"),
            "behavioral_contracts": localization.get(
                "behavioral_contracts",
                [],
            ),
            "candidate_files": localization.get("candidate_files", []),
            "suspected_symbols": localization.get("suspected_symbols", []),
        },
        "confidence": 1.0,
        "verification": "official_swebench_harness",
    }


def build_verified_memory_seed(
    rollouts: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    memories: dict[str, dict[str, Any]] = {}
    for row in rollouts:
        memory = verified_memory_from_rollout(row)
        if memory is None or not memory["source_instance_id"] or not memory["repo"]:
            continue
        memories[memory["source_instance_id"]] = memory
    return [memories[key] for key in sorted(memories)]


def load_memory_seed(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"memory_seed_not_found:{source}")
    return [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def recall_verified_memories(
    memories: Iterable[dict[str, Any]],
    *,
    repo: str,
    instance_id: str,
    query: str,
    limit: int = 6,
) -> list[dict[str, Any]]:
    query_tokens = set(tokenize(query))
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for memory in memories:
        source_id = str(memory.get("source_instance_id") or "")
        if memory.get("repo") != repo or source_id == instance_id:
            continue
        content = memory.get("content")
        serialized = json.dumps(
            content if isinstance(content, dict) else {},
            ensure_ascii=False,
            sort_keys=True,
        )
        memory_tokens = set(tokenize(serialized))
        overlap = len(query_tokens & memory_tokens)
        if overlap == 0:
            continue
        coverage = overlap / max(len(query_tokens), 1)
        confidence = float(memory.get("confidence", 0.5))
        score = coverage * 0.7 + confidence * 0.3
        scored.append((score, source_id, memory))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        {**memory, "retrieval_score": round(score, 6)}
        for score, _, memory in scored[: max(1, min(limit, 20))]
    ]
