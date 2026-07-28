from __future__ import annotations

import json
import os
import tempfile
import hashlib
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from codecairn.review.models import ChangeProof
from codecairn.review.ledger import (
    migrate_legacy_events,
    new_ledger_event,
    verify_ledger,
)


STORAGE_SCHEMA_VERSION = "2"


class ReviewStorageError(RuntimeError):
    pass


class StoredReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_schema_version: str
    proof: ChangeProof


class ReviewRevisionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_series_id: str
    review_family_id: str = ""
    change_id: str
    revision_id: str = ""
    parent_change_id: str | None
    revision_number: int
    requirement_contract_hash: str
    requirement_contract_revision: int = 1
    base_sha: str
    head_sha: str
    workspace_tree_sha: str
    git_snapshot_id: str = ""
    content_tree_hash: str = ""
    patch_fingerprint: str = ""
    snapshot_revision_id: str = ""
    created_at: datetime


class ReviewSeriesIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_schema_version: str
    review_series_id: str
    revisions: list[ReviewRevisionMetadata]


def default_review_root() -> Path:
    return Path.home() / ".codecairn" / "reviews"


def review_path(change_id: str, root: Path | None = None) -> Path:
    return (root or default_review_root()) / f"{change_id}.json"


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_review(proof: ChangeProof, path: Path) -> None:
    payload = StoredReview(
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        proof=proof,
    ).model_dump_json(indent=2)
    _atomic_write(path, payload)


def _migrate_stored_review(raw: dict) -> dict:
    proof = raw.get("proof")
    if not isinstance(proof, dict):
        return raw
    snapshot = proof.get("git_snapshot", {})
    snapshot.setdefault(
        "content_tree_hash", snapshot.get("workspace_tree_sha", "")
    )
    snapshot.setdefault("patch_fingerprint", proof.get("change_id", ""))
    snapshot.setdefault("git_snapshot_id", "")
    snapshot.setdefault("revision_id", "")
    current_captures = proof.pop("unified_capture_events", [])
    if current_captures:
        proof["capture_events"] = current_captures
    else:
        proof["capture_events"] = [
            capture
            for capture in proof.get("capture_events", [])
            if "payload" in capture and "host" in capture
        ]
    for capture in proof["capture_events"]:
        capture["schema_version"] = "1"
    proof["schema_version"] = "1"
    for verification in proof.get("verifications", []):
        verification.setdefault(
            "content_tree_hash", verification.get("workspace_tree_sha", "")
        )
        verification.setdefault(
            "patch_fingerprint", snapshot.get("patch_fingerprint", "")
        )
    if not proof.get("review_family_id"):
        repository = proof.get("repository", {}).get("root", "")
        snapshot = proof.get("git_snapshot", {})
        parts = [
            repository,
            snapshot.get("base_ref", ""),
            snapshot.get("base_sha", ""),
        ]
        digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]
        proof["review_family_id"] = f"family_{digest}"
        proof.setdefault("storage_migrations", []).append(
            "review_family_identity"
        )
    captured = proof.get("git_snapshot", {}).get("captured_at")
    try:
        timestamp = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
    except ValueError:
        timestamp = datetime.now().astimezone()
    raw_events = proof.get("audit_events") or []
    legacy_events = any(
        not isinstance(item, dict) or "event_hash" not in item
        for item in raw_events
    )
    if legacy_events:
        events = migrate_legacy_events(
            raw_events, default_timestamp=timestamp
        )
        proof["audit_events"] = [
            item.model_dump(mode="json") for item in events
        ]
        proof["last_event_hash"] = events[-1].event_hash if events else ""
        proof["ledger_integrity"] = True
        proof.setdefault("storage_migrations", []).append(
            "legacy_audit_events_to_ledger"
        )
    else:
        events = []
    decisions = proof.setdefault("review_decisions", [])
    if not decisions:
        current_events = proof.get("audit_events") or []
        previous = (
            current_events[-1]["event_hash"] if current_events else ""
        )
        sequence = len(current_events) + 1
        migration_event = new_ledger_event(
            sequence=sequence,
            event_type="legacy_review_decisions_migrated",
            actor_type="system",
            actor_id="schema_migration",
            payload={"from_storage_schema": raw.get("storage_schema_version", "1")},
            previous_event_hash=previous,
            timestamp=timestamp,
        )
        derived: list[tuple[str, str, str]] = []
        derived.extend(
            ("claim", item["id"], item.get("status", "proposed"))
            for item in proof.get("claims", [])
            if item.get("status") in {"confirmed", "rejected"}
        )
        derived.extend(
            ("mapping", item["id"], "confirmed")
            for item in proof.get("mappings", [])
            if item.get("confirmed")
        )
        derived.extend(
            ("coverage_assertion", item["id"], item.get("status", "proposed"))
            for item in proof.get("coverage_assertions", [])
            if item.get("status") in {"confirmed", "rejected"}
        )
        if derived:
            proof.setdefault("audit_events", []).append(
                migration_event.model_dump(mode="json")
            )
            proof["last_event_hash"] = migration_event.event_hash
            for target_type, target_id, decision in derived:
                digest = hashlib.sha256(
                    f"{target_type}:{target_id}:{decision}".encode()
                ).hexdigest()[:16]
                decisions.append(
                    {
                        "id": f"legacy_decision_{digest}",
                        "target_type": target_type,
                        "target_id": target_id,
                        "decision": decision,
                        "explanation": "Migrated from legacy mutable review state.",
                        "reviewer": "legacy_reviewer",
                        "decided_at": migration_event.timestamp.isoformat(),
                        "source_event_id": migration_event.event_id,
                    }
                )
            proof.setdefault("storage_migrations", []).append(
                "legacy_mutable_status_to_review_decisions"
            )
    if not proof.get("requirement_revisions"):
        proof["requirement_revisions"] = [
            {
                "requirement_id": item["id"],
                "revision": item.get("revision", 1),
                "text": item["text"],
                "original_text": item.get("original_text", item["text"]),
                "category": item.get("category", "requirement"),
                "deleted": item.get("deleted", False),
                "actor": "schema_migration",
                "revised_at": timestamp.isoformat(),
                "source_event_id": None,
            }
            for item in proof.get("requirements", [])
        ]
    raw["storage_schema_version"] = STORAGE_SCHEMA_VERSION
    return raw


def load_review(path: Path, expected: ChangeProof) -> ChangeProof | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        stored_version = str(raw.get("storage_schema_version", ""))
        if stored_version not in {"1", STORAGE_SCHEMA_VERSION}:
            raise ReviewStorageError(
                f"本地 Review 状态版本 {stored_version} 不受支持；"
                f"当前版本为 {STORAGE_SCHEMA_VERSION}。"
            )
        proof_raw = raw.get("proof") if isinstance(raw, dict) else {}
        raw_events = (
            proof_raw.get("audit_events", [])
            if isinstance(proof_raw, dict)
            else []
        )
        needs_rewrite = (
            stored_version == "1"
            or not isinstance(proof_raw, dict)
            or not proof_raw.get("review_family_id")
            or not proof_raw.get("requirement_revisions")
            or any(
                not isinstance(item, dict) or "event_hash" not in item
                for item in raw_events
            )
        )
        raw = _migrate_stored_review(raw)
        stored = StoredReview.model_validate(raw)
    except ReviewStorageError:
        raise
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ReviewStorageError(
            f"无法读取本地 Review 状态 {path}：文件损坏或格式无效。"
        ) from exc
    if stored.storage_schema_version != STORAGE_SCHEMA_VERSION:
        raise ReviewStorageError(
            f"本地 Review 状态版本 {stored.storage_schema_version} 不受支持；"
            f"当前版本为 {STORAGE_SCHEMA_VERSION}。"
        )
    integrity, last_hash = verify_ledger(stored.proof.audit_events)
    stored.proof.ledger_integrity = integrity
    if integrity:
        stored.proof.last_event_hash = last_hash
    else:
        if "ledger_integrity_failed" not in stored.proof.gate.reasons:
            stored.proof.gate.reasons.append("ledger_integrity_failed")
        stored.proof.gate.status = "warning"
        if stored.proof.assurance.level == "high":
            stored.proof.assurance.level = "medium"
    if stored.proof.schema_version != expected.schema_version:
        raise ReviewStorageError(
            "Change Proof schema 不兼容，不能恢复旧状态。"
        )
    if (
        stored.proof.requirement_contract_hash
        != expected.requirement_contract_hash
    ):
        raise ReviewStorageError(
            "本地 Review 的 Requirement Contract 与当前请求不一致，"
            "已拒绝复用。"
        )
    saved = stored.proof.git_snapshot
    current = expected.git_snapshot
    if (
        stored.proof.change_id != expected.change_id
        or saved.base_sha != current.base_sha
        or (
            saved.patch_fingerprint
            and saved.patch_fingerprint != current.patch_fingerprint
        )
        or (
            saved.content_tree_hash
            and saved.content_tree_hash != current.content_tree_hash
        )
    ):
        raise ReviewStorageError(
            "本地 Review 状态与当前 Git Snapshot 不一致，已拒绝复用。"
        )
    if needs_rewrite:
        save_review(stored.proof, path)
    return stored.proof


def load_review_revision(path: Path) -> ChangeProof:
    """Load a historical revision without claiming it matches current Git."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw = _migrate_stored_review(raw)
        stored = StoredReview.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ReviewStorageError(
            f"无法读取历史 Review revision {path}。"
        ) from exc
    valid, last_hash = verify_ledger(stored.proof.audit_events)
    if not valid:
        raise ReviewStorageError("历史 Review Ledger 完整性校验失败。")
    stored.proof.ledger_integrity = True
    stored.proof.last_event_hash = last_hash
    return stored.proof


def find_review_for_patch(
    repository_root: Path,
    *,
    base_sha: str,
    patch_fingerprint: str,
    root: Path | None = None,
) -> ChangeProof | None:
    base = root or default_review_root()
    if not base.exists():
        return None
    matches: list[tuple[float, ChangeProof]] = []
    for path in base.glob("change_*.json"):
        try:
            proof = load_review_revision(path)
        except ReviewStorageError:
            continue
        if (
            Path(proof.repository.root).resolve() == repository_root.resolve()
            and proof.git_snapshot.base_sha == base_sha
            and proof.git_snapshot.patch_fingerprint == patch_fingerprint
        ):
            matches.append((path.stat().st_mtime, proof))
    return max(matches, key=lambda item: item[0])[1] if matches else None


def series_index_path(series_id: str, root: Path | None = None) -> Path:
    return (root or default_review_root()) / f"series_{series_id}.json"


def load_review_revisions(
    series_id: str, root: Path | None = None
) -> list[ReviewRevisionMetadata]:
    path = series_index_path(series_id, root)
    if not path.exists():
        return []
    try:
        index = ReviewSeriesIndex.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise ReviewStorageError(
            f"无法读取 Review revision index {path}：文件损坏或格式无效。"
        ) from exc
    if (
        index.storage_schema_version not in {"1", STORAGE_SCHEMA_VERSION}
        or index.review_series_id != series_id
    ):
        raise ReviewStorageError("Review revision index 版本或 series 不匹配。")
    return index.revisions


def register_review_revision(
    proof: ChangeProof, root: Path | None = None
) -> None:
    revisions = load_review_revisions(proof.review_series_id, root)
    if any(
        item.revision_id == proof.revision_id
        and item.revision_number == proof.revision_number
        for item in revisions
    ):
        return
    revisions.append(
        ReviewRevisionMetadata(
            review_series_id=proof.review_series_id,
            review_family_id=proof.review_family_id,
            change_id=proof.change_id,
            revision_id=proof.revision_id,
            parent_change_id=proof.parent_change_id,
            revision_number=proof.revision_number,
            requirement_contract_hash=proof.requirement_contract_hash,
            requirement_contract_revision=proof.requirement_contract_revision,
            base_sha=proof.git_snapshot.base_sha,
            head_sha=proof.git_snapshot.head_sha,
            workspace_tree_sha=proof.git_snapshot.workspace_tree_sha,
            git_snapshot_id=proof.git_snapshot.git_snapshot_id,
            content_tree_hash=proof.git_snapshot.content_tree_hash,
            patch_fingerprint=proof.git_snapshot.patch_fingerprint,
            snapshot_revision_id=proof.git_snapshot.revision_id,
            created_at=proof.git_snapshot.captured_at,
        )
    )
    index = ReviewSeriesIndex(
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        review_series_id=proof.review_series_id,
        revisions=revisions,
    )
    _atomic_write(
        series_index_path(proof.review_series_id, root),
        index.model_dump_json(indent=2),
    )


def load_review_family_revisions(
    family_id: str, root: Path | None = None
) -> list[ReviewRevisionMetadata]:
    base = root or default_review_root()
    if not base.exists():
        return []
    revisions: list[ReviewRevisionMetadata] = []
    for path in sorted(base.glob("series_*.json")):
        try:
            index = ReviewSeriesIndex.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise ReviewStorageError(
                f"无法读取 Review revision index {path}：文件损坏或格式无效。"
            ) from exc
        revisions.extend(
            item
            for item in index.revisions
            if item.review_family_id == family_id
        )
    return sorted(revisions, key=lambda item: item.created_at)
