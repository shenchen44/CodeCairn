import time
from collections.abc import Callable

from app.services.orchestration.contracts import RuntimeEvent


class RuntimeEventRecorder:
    """Small event recorder that can later be replaced by a streaming sink."""

    def __init__(
        self,
        listener: Callable[[RuntimeEvent], None] | None = None,
    ) -> None:
        self._started = time.perf_counter()
        self._events: list[RuntimeEvent] = []
        self._listener = listener

    def emit(
        self,
        event_type: str,
        *,
        phase: str | None = None,
        payload: dict | None = None,
    ) -> None:
        event = RuntimeEvent(
            sequence=len(self._events) + 1,
            event_type=event_type,
            phase=phase,
            payload=payload or {},
            elapsed_ms=int(
                (time.perf_counter() - self._started) * 1000
            ),
        )
        self._events.append(event)
        if self._listener is not None:
            self._listener(event)

    def dump(self) -> list[dict]:
        return [
            event.model_dump(mode="json")
            for event in self._events
        ]
