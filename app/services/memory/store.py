from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models.memory import AgentMemory, MemoryKind, MemoryScope
from app.services.retrieval.hybrid import tokenize


def _fingerprint(kind: MemoryKind, content: dict) -> str:
    normalized = json.dumps(
        {"kind": kind.value, "content": content},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def snapshot_evidence(
    repo_path: Path,
    evidence: list | dict | None,
) -> list | dict:
    if not isinstance(evidence, list):
        return evidence or []
    root = repo_path.resolve()
    snapshots: list = []
    for item in evidence:
        if not isinstance(item, dict):
            snapshots.append(item)
            continue
        enriched = dict(item)
        relative_path = item.get("path")
        if relative_path:
            candidate = (root / str(relative_path)).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                enriched["content_sha256"] = None
            else:
                enriched["content_sha256"] = _file_sha256(candidate)
        snapshots.append(enriched)
    return snapshots


def _evidence_is_stale(repo_path: Path, evidence: list | dict) -> bool:
    if not isinstance(evidence, list):
        return False
    root = repo_path.resolve()
    for item in evidence:
        if not isinstance(item, dict) or not item.get("content_sha256"):
            continue
        candidate = (root / str(item.get("path", ""))).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return True
        if _file_sha256(candidate) != item["content_sha256"]:
            return True
    return False


def remember(
    db: Session,
    *,
    repository_id: int,
    scope: MemoryScope,
    kind: MemoryKind,
    content: dict,
    evidence: list | dict | None = None,
    confidence: float = 0.5,
    task_id: str | None = None,
    source_commit: str | None = None,
) -> AgentMemory:
    if scope == MemoryScope.task and task_id is None:
        raise ValueError("task_scope_requires_task_id")
    fingerprint = _fingerprint(kind, content)
    existing = db.scalar(
        select(AgentMemory).where(
            AgentMemory.repository_id == repository_id,
            AgentMemory.task_id == task_id,
            AgentMemory.scope == scope,
            AgentMemory.kind == kind,
            AgentMemory.fingerprint == fingerprint,
            AgentMemory.invalidated_at.is_(None),
        )
    )
    if existing is not None:
        existing.confidence = max(existing.confidence, confidence)
        existing.evidence = evidence or existing.evidence
        existing.source_commit = source_commit or existing.source_commit
        db.add(existing)
        return existing

    memory = AgentMemory(
        repository_id=repository_id,
        task_id=task_id,
        scope=scope,
        kind=kind,
        content=content,
        evidence=evidence or [],
        confidence=max(0.0, min(confidence, 1.0)),
        fingerprint=fingerprint,
        source_commit=source_commit,
    )
    db.add(memory)
    db.flush()
    return memory


def _memory_score(memory: AgentMemory, query_tokens: set[str]) -> float:
    serialized = json.dumps(memory.content, ensure_ascii=False, sort_keys=True)
    memory_tokens = set(tokenize(serialized))
    overlap = len(query_tokens & memory_tokens)
    if overlap == 0:
        return 0.0
    coverage = overlap / max(len(query_tokens), 1)
    return coverage * 0.7 + memory.confidence * 0.3


def recall(
    db: Session,
    *,
    repository_id: int,
    query: str,
    task_id: str | None = None,
    limit: int = 6,
    repo_path: Path | None = None,
) -> list[dict]:
    scope_filter = AgentMemory.scope == MemoryScope.repository
    if task_id is not None:
        scope_filter = or_(
            scope_filter,
            (
                (AgentMemory.scope == MemoryScope.task)
                & (AgentMemory.task_id == task_id)
            ),
        )
    memories = list(
        db.scalars(
            select(AgentMemory)
            .where(
                AgentMemory.repository_id == repository_id,
                AgentMemory.invalidated_at.is_(None),
                scope_filter,
            )
            .order_by(AgentMemory.created_at.desc())
            .limit(100)
        )
    )
    if repo_path is not None:
        valid_memories: list[AgentMemory] = []
        for memory in memories:
            if (
                memory.scope == MemoryScope.repository
                and _evidence_is_stale(repo_path, memory.evidence)
            ):
                memory.invalidated_at = datetime.now(timezone.utc)
                db.add(memory)
                continue
            valid_memories.append(memory)
        memories = valid_memories
    query_tokens = set(tokenize(query))
    ranked = sorted(
        (
            (_memory_score(memory, query_tokens), memory)
            for memory in memories
        ),
        key=lambda item: (-item[0], item[1].created_at),
    )
    return [
        {
            "id": memory.id,
            "scope": memory.scope.value,
            "kind": memory.kind.value,
            "content": memory.content,
            "evidence": memory.evidence,
            "confidence": memory.confidence,
            "source_commit": memory.source_commit,
            "retrieval_score": round(score, 6),
        }
        for score, memory in ranked[: max(1, min(limit, 20))]
        if score > 0
    ]


def invalidate_memory(db: Session, memory_id: str) -> bool:
    memory = db.get(AgentMemory, memory_id)
    if memory is None or memory.invalidated_at is not None:
        return False
    memory.invalidated_at = datetime.now(timezone.utc)
    db.add(memory)
    return True
