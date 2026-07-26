from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskIntent(str, Enum):
    change = "change"
    review = "review"
    investigate = "investigate"
    explain = "explain"


class TaskSource(str, Enum):
    interactive = "interactive"
    api = "api"
    github_issue = "github_issue"
    swe_bench = "swe_bench"
    automation = "automation"


class DeliveryTarget(str, Enum):
    workspace = "workspace"
    patch = "patch"
    pull_request = "pull_request"
    report = "report"


class TaskConstraints(BaseModel):
    model_config = ConfigDict(extra="allow")

    allowed_paths: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)
    max_changed_files: int | None = Field(default=None, ge=1)
    max_diff_lines: int | None = Field(default=None, ge=1)
    time_budget_seconds: int | None = Field(default=None, ge=1)


class CodingTask(BaseModel):
    """Source-independent contract consumed by the coding runtime."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    objective: str = Field(min_length=1)
    description: str = ""
    intent: TaskIntent = TaskIntent.change
    source: TaskSource = TaskSource.interactive
    requirements: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    delivery: DeliveryTarget = DeliveryTarget.workspace
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    repository: dict[str, Any] = Field(default_factory=dict)
    prior_context: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def requires_workspace_change(self) -> bool:
        return self.intent == TaskIntent.change

    @property
    def allows_mutation(self) -> bool:
        return self.intent == TaskIntent.change

    def prompt_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def _infer_source(payload: dict[str, Any]) -> TaskSource:
    explicit = payload.get("source")
    if explicit:
        try:
            return TaskSource(str(explicit))
        except ValueError:
            pass
    if payload.get("instance_id") or payload.get("evaluation_note"):
        return TaskSource.swe_bench
    if payload.get("issue_number") or payload.get("github_issue_number"):
        return TaskSource.github_issue
    return TaskSource.api


def _infer_intent(payload: dict[str, Any]) -> TaskIntent:
    explicit = payload.get("intent")
    if explicit:
        try:
            return TaskIntent(str(explicit))
        except ValueError:
            pass
    mode = str(payload.get("mode") or "").lower()
    if mode in {"review", "investigate", "explain"}:
        return TaskIntent(mode)
    return TaskIntent.change


def normalize_task(value: CodingTask | dict[str, Any]) -> CodingTask:
    if isinstance(value, CodingTask):
        return value

    if "objective" in value:
        payload = dict(value)
        payload.setdefault("source", _infer_source(payload))
        payload.setdefault("intent", _infer_intent(payload))
        return CodingTask.model_validate(payload)

    title = str(value.get("title") or "").strip()
    body = str(
        value.get("body")
        or value.get("problem_statement")
        or ""
    ).strip()
    objective = title or body.splitlines()[0].strip()
    if not objective:
        objective = "Work on the requested repository task"

    requirements = value.get("requirements")
    if not isinstance(requirements, list):
        requirements = []
    acceptance = value.get("acceptance_criteria")
    if not isinstance(acceptance, list):
        acceptance = []

    reserved = {
        "title",
        "body",
        "problem_statement",
        "requirements",
        "acceptance_criteria",
        "memory_context",
    }
    metadata = {
        key: item
        for key, item in value.items()
        if key not in reserved
    }
    return CodingTask(
        id=(
            str(value.get("instance_id"))
            if value.get("instance_id")
            else None
        ),
        objective=objective,
        description=body,
        intent=_infer_intent(value),
        source=_infer_source(value),
        requirements=[str(item) for item in requirements],
        acceptance_criteria=[str(item) for item in acceptance],
        delivery=(
            DeliveryTarget.patch
            if _infer_source(value) == TaskSource.swe_bench
            else DeliveryTarget.pull_request
            if _infer_source(value) == TaskSource.github_issue
            else DeliveryTarget.workspace
        ),
        prior_context=list(value.get("memory_context") or []),
        metadata=metadata,
    )
