from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from codecairn.review.models import LedgerEvent, Provenance


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def event_hash(event: LedgerEvent) -> str:
    payload = event.model_dump(mode="json", exclude={"event_hash"})
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def new_ledger_event(
    *,
    sequence: int,
    event_type: str,
    actor_type: str,
    actor_id: str,
    payload: dict[str, Any],
    previous_event_hash: str,
    event_id: str | None = None,
    timestamp: datetime | None = None,
) -> LedgerEvent:
    kind = (
        "verified"
        if actor_type in {"reviewer", "sandbox"}
        else "inferred"
        if actor_type == "model"
        else "derived"
        if actor_type == "system"
        else "captured"
    )
    event = LedgerEvent(
        event_id=event_id or f"ledger_{uuid.uuid4().hex}",
        sequence=sequence,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        timestamp=timestamp or datetime.now(timezone.utc),
        payload=payload,
        provenance=Provenance(kind=kind, source=actor_id),
        previous_event_hash=previous_event_hash,
        event_hash="",
    )
    event.event_hash = event_hash(event)
    return event


def verify_ledger(events: list[LedgerEvent]) -> tuple[bool, str]:
    previous = ""
    for expected_sequence, event in enumerate(events, start=1):
        if (
            event.sequence != expected_sequence
            or event.previous_event_hash != previous
            or event.event_hash != event_hash(event)
        ):
            return False, previous
        previous = event.event_hash
    return True, previous


def migrate_legacy_events(
    events: list[dict[str, Any] | LedgerEvent],
    *,
    default_timestamp: datetime,
) -> list[LedgerEvent]:
    migrated: list[LedgerEvent] = []
    previous = ""
    for sequence, raw in enumerate(events, start=1):
        if isinstance(raw, LedgerEvent):
            migrated.append(raw)
            previous = raw.event_hash
            continue
        action = str(raw.get("action") or raw.get("event_type") or "legacy_event")
        provenance = raw.get("provenance") or {}
        source = str(provenance.get("source") or "legacy_reviewer")
        actor_type = (
            "sandbox"
            if "verification" in action
            else "system"
            if "stale" in action or "revision" in action
            else "reviewer"
        )
        timestamp_value = provenance.get("created_at")
        try:
            timestamp = (
                datetime.fromisoformat(timestamp_value)
                if timestamp_value
                else default_timestamp
            )
        except (TypeError, ValueError):
            timestamp = default_timestamp
        event = new_ledger_event(
            sequence=sequence,
            event_type=action,
            actor_type=actor_type,
            actor_id=source,
            payload=dict(raw.get("details") or raw.get("payload") or {}),
            previous_event_hash=previous,
            event_id=f"legacy_{sequence}_{hashlib.sha256(_canonical_json(raw).encode()).hexdigest()[:16]}",
            timestamp=timestamp,
        )
        migrated.append(event)
        previous = event.event_hash
    return migrated
