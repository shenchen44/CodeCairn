from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class SessionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    parent_id: str | None = None
    kind: str
    payload: dict
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class AgentSession:
    """Append-only session tree for interactive, API, and automation clients."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        entries: list[SessionEntry] | None = None,
        head_id: str | None = None,
        path: Path | None = None,
    ) -> None:
        self.session_id = session_id or uuid4().hex
        self.entries = list(entries or [])
        self.head_id = (
            head_id
            if head_id is not None
            else self.entries[-1].id
            if self.entries
            else None
        )
        self.path = path

    def append(self, kind: str, payload: dict) -> SessionEntry:
        entry = SessionEntry(
            parent_id=self.head_id,
            kind=kind,
            payload=payload,
        )
        self.entries.append(entry)
        self.head_id = entry.id
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(entry.model_dump_json() + "\n")
        return entry

    def append_message(self, message: dict) -> SessionEntry:
        return self.append("message", message)

    def lineage(self, head_id: str | None = None) -> list[SessionEntry]:
        by_id = {entry.id: entry for entry in self.entries}
        current = head_id if head_id is not None else self.head_id
        lineage: list[SessionEntry] = []
        while current is not None:
            entry = by_id[current]
            lineage.append(entry)
            current = entry.parent_id
        return list(reversed(lineage))

    def message_context(self) -> list[dict]:
        return [
            dict(entry.payload)
            for entry in self.lineage()
            if entry.kind == "message"
        ]

    def fork(self, head_id: str | None = None) -> AgentSession:
        selected = head_id if head_id is not None else self.head_id
        if selected is not None and selected not in {
            entry.id for entry in self.entries
        }:
            raise ValueError(f"session_entry_not_found:{selected}")
        return AgentSession(
            entries=self.entries,
            head_id=selected,
        )

    @classmethod
    def load(cls, path: Path) -> AgentSession:
        entries = [
            SessionEntry.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return cls(entries=entries, path=path)
