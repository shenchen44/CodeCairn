from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ToolCapability(str, Enum):
    context = "context"
    read = "read"
    search = "search"
    mutate = "mutate"
    execute = "execute"
    version_control = "version_control"


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    schema: dict[str, Any]
    handler: Callable[..., dict]
    capability: ToolCapability
    source: str = "extension"


class ToolRegistry:
    """Runtime tool registry inspired by Pi's active-tool model."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._active: set[str] | None = None

    def register(self, tool: RegisteredTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def set_active(self, names: list[str] | None) -> None:
        self._active = None if names is None else set(names)

    def is_active(self, name: str) -> bool:
        return self._active is None or name in self._active

    def schemas(self) -> list[dict[str, Any]]:
        return [
            tool.schema
            for tool in self._tools.values()
            if self.is_active(tool.name)
        ]

    def names(self) -> list[str]:
        return [
            name for name in self._tools if self.is_active(name)
        ]
