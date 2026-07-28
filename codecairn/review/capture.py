from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from codecairn.review.models import CaptureEvent
from codecairn.review.models import Provenance


class CaptureStorageError(RuntimeError):
    pass


_SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|secret|password|cookie)"
)
_SENSITIVE_VALUE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_-]{12,}|"
    r"github_pat_[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16})"
)
_ALLOWED_HOSTS = {
    "pi",
    "claude_code",
    "codex",
    "cursor",
    "manual",
    "unknown",
}


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def redact_payload(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > 12:
        return "[TRUNCATED]"
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): redact_payload(
                item_value, key=str(item_key), depth=depth + 1
            )
            for item_key, item_value in list(value.items())[:200]
        }
    if isinstance(value, (list, tuple)):
        return [
            redact_payload(item, depth=depth + 1) for item in value[:500]
        ]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub("[REDACTED]", value)[:50000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:50000]


def default_capture_root() -> Path:
    return Path.home() / ".codecairn" / "captures"


def capture_path(repo: Path, root: Path | None = None) -> Path:
    repository_id = _canonical_hash(str(repo.resolve()))[:20]
    directory = (root or default_capture_root()) / repository_id
    current = directory / "events.jsonl"
    legacy = directory / "events-v3.jsonl"
    if not current.exists() and legacy.exists():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        legacy.replace(current)
        legacy_lock = legacy.with_suffix(legacy.suffix + ".lock")
        if legacy_lock.exists():
            legacy_lock.replace(current.with_suffix(current.suffix + ".lock"))
    return current


def _safe_paths(repo: Path, values: Iterable[Any]) -> list[str]:
    root = repo.resolve()
    result: list[str] = []
    for raw in values:
        candidate = Path(str(raw))
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (root / candidate).resolve()
        )
        try:
            result.append(resolved.relative_to(root).as_posix())
        except ValueError as exc:
            raise ValueError(
                f"capture_path_outside_repository:{raw}"
            ) from exc
    return sorted(set(result))


def _event_hash(event: CaptureEvent) -> str:
    content = event.model_dump(mode="json")
    content["event_hash"] = ""
    content["integrity_status"] = "unverified"
    return _canonical_hash(content)


def verify_capture_chain(
    events: list[CaptureEvent],
) -> tuple[bool, str]:
    previous_hash = ""
    ids: dict[str, str] = {}
    for sequence, event in enumerate(events, 1):
        if event.event_id in ids:
            return False, (
                "capture_id_collision"
                if ids[event.event_id] != event.payload_hash
                else "capture_duplicate_event_id"
            )
        ids[event.event_id] = event.payload_hash
        if (
            event.sequence != sequence
            or event.previous_event_hash != previous_hash
            or event.event_hash != _event_hash(event)
        ):
            return False, f"capture_integrity_failed:sequence_{sequence}"
        previous_hash = event.event_hash
    return True, previous_hash


def event_from_payload(
    payload: dict[str, Any],
    *,
    repo: Path,
    host: str | None = None,
) -> CaptureEvent:
    repo = repo.resolve()
    redacted = redact_payload(payload)
    if not isinstance(redacted, dict):
        raise ValueError("capture_payload_must_be_object")
    selected_host = str(host or redacted.get("host") or "unknown")
    if selected_host not in _ALLOWED_HOSTS:
        selected_host = "unknown"
    session_id = str(redacted.get("session_id") or "unknown-session")
    event_type = str(redacted.get("event_type") or "unknown")
    affected_paths = redacted.get("affected_paths") or []
    if not isinstance(affected_paths, list):
        raise ValueError("capture_affected_paths_must_be_list")
    redacted["affected_paths"] = _safe_paths(repo, affected_paths)
    timestamp_raw = redacted.get("timestamp")
    try:
        timestamp = (
            datetime.fromisoformat(str(timestamp_raw).replace("Z", "+00:00"))
            if timestamp_raw
            else datetime.now(timezone.utc)
        )
    except ValueError:
        timestamp = datetime.now(timezone.utc)
    semantic = {
        "session_id": session_id,
        "host": selected_host,
        "event_type": event_type,
        "cwd": str(repo),
        "parent_event_id": redacted.get("parent_event_id"),
        "payload": redacted,
    }
    payload_hash = _canonical_hash(semantic)
    event_id = str(
        redacted.get("event_id")
        or "capture_" + hashlib.sha256(
            (
                f"{session_id}:{event_type}:"
                f"{redacted.get('parent_event_id') or ''}:{payload_hash}"
            ).encode()
        ).hexdigest()[:24]
    )
    repository_id = _canonical_hash(str(repo))[:20]
    return CaptureEvent(
        event_id=event_id,
        session_id=session_id,
        host=selected_host,
        event_type=event_type,
        timestamp=timestamp,
        cwd=str(repo),
        repository=str(repo),
        repository_id=repository_id,
        git_snapshot_id=str(redacted.get("git_snapshot_id") or ""),
        parent_event_id=(
            str(redacted["parent_event_id"])
            if redacted.get("parent_event_id")
            else None
        ),
        payload=redacted,
        payload_hash=payload_hash,
        provenance=Provenance(
            kind="captured",
            source=selected_host,
            source_event_ids=[event_id],
            model=(
                str(redacted["model"]) if redacted.get("model") else None
            ),
        ),
    )


class CaptureStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    def load(self) -> list[CaptureEvent]:
        if not self.path.exists():
            return []
        events: list[CaptureEvent] = []
        previous_hash = ""
        ids: dict[str, str] = {}
        lines = self.path.read_bytes().splitlines(keepends=True)
        for index, line in enumerate(lines):
            if not line.endswith(b"\n") and index == len(lines) - 1:
                break
            try:
                event = CaptureEvent.model_validate_json(line)
            except (ValidationError, ValueError) as exc:
                raise CaptureStorageError(
                    f"capture_jsonl_corrupt:line_{index + 1}"
                ) from exc
            if event.event_id in ids:
                reason = (
                    "capture_id_collision"
                    if ids[event.event_id] != event.payload_hash
                    else "capture_duplicate_event_id"
                )
                raise CaptureStorageError(
                    f"{reason}:line_{index + 1}"
                )
            if (
                event.sequence != index + 1
                or event.previous_event_hash != previous_hash
                or event.event_hash != _event_hash(event)
            ):
                raise CaptureStorageError(
                    f"capture_integrity_failed:line_{index + 1}"
                )
            event.integrity_status = "valid"
            ids[event.event_id] = event.payload_hash
            events.append(event)
            previous_hash = event.event_hash
        return events

    def append(self, event: CaptureEvent) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        descriptor = os.open(
            self.lock_path, os.O_CREAT | os.O_RDWR, 0o600
        )
        os.close(descriptor)
        with self.lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    raw = self.path.read_bytes()
                    if raw and not raw.endswith(b"\n"):
                        end = raw.rfind(b"\n")
                        with self.path.open("r+b") as handle:
                            handle.truncate(end + 1)
                existing = self.load()
                duplicate = next(
                    (
                        item
                        for item in existing
                        if item.event_id == event.event_id
                    ),
                    None,
                )
                if duplicate:
                    if duplicate.payload_hash != event.payload_hash:
                        raise CaptureStorageError(
                            "capture_id_collision"
                        )
                    return False
                stored = event.model_copy(deep=True)
                stored.sequence = len(existing) + 1
                stored.previous_event_hash = (
                    existing[-1].event_hash if existing else ""
                )
                stored.integrity_status = "unverified"
                stored.event_hash = _event_hash(stored)
                stored.integrity_status = "valid"
                output = os.open(
                    self.path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                try:
                    os.write(
                        output,
                        (stored.model_dump_json() + "\n").encode(),
                    )
                    os.fsync(output)
                finally:
                    os.close(output)
                return True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def sessions(self) -> dict[str, list[CaptureEvent]]:
        result: dict[str, list[CaptureEvent]] = {}
        for event in self.load():
            result.setdefault(event.session_id, []).append(event)
        return result
