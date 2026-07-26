from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.agent_runtime.task import CodingTask


@dataclass(slots=True)
class ExtensionEvent:
    name: str
    task: CodingTask
    turn: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtensionResult:
    block: bool = False
    reason: str = ""
    payload: dict[str, Any] | None = None


class AgentExtension(Protocol):
    def on_event(
        self,
        event: ExtensionEvent,
    ) -> ExtensionResult | None: ...


class ExtensionManager:
    def __init__(
        self,
        extensions: list[AgentExtension] | None = None,
    ) -> None:
        self._extensions = list(extensions or [])

    def register(self, extension: AgentExtension) -> None:
        self._extensions.append(extension)

    def emit(self, event: ExtensionEvent) -> ExtensionResult:
        payload = dict(event.payload)
        for extension in self._extensions:
            response = extension.on_event(
                ExtensionEvent(
                    name=event.name,
                    task=event.task,
                    turn=event.turn,
                    payload=payload,
                )
            )
            if response is None:
                continue
            if response.payload is not None:
                payload = dict(response.payload)
            if response.block:
                return ExtensionResult(
                    block=True,
                    reason=response.reason,
                    payload=payload,
                )
        return ExtensionResult(payload=payload)
