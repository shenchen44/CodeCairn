from __future__ import annotations

from typing import Any

from app.services.agent_runtime import (
    CodingTask,
    DeliveryTarget,
    TaskIntent,
    TaskSource,
)


def github_issue_task(context: dict[str, Any]) -> CodingTask:
    title = str(context.get("title") or "").strip()
    body = str(context.get("body") or "").strip()
    repository_value = context.get("repository")
    repository = (
        dict(repository_value)
        if isinstance(repository_value, dict)
        else {"name": str(repository_value)}
        if repository_value
        else {}
    )
    return CodingTask(
        id=(
            str(context["issue_number"])
            if context.get("issue_number") is not None
            else None
        ),
        objective=title or body.splitlines()[0],
        description=body,
        intent=TaskIntent.change,
        source=TaskSource.github_issue,
        delivery=DeliveryTarget.pull_request,
        prior_context=list(context.get("memory_context") or []),
        repository=repository,
        metadata={
            key: value
            for key, value in context.items()
            if key not in {"title", "body", "memory_context", "repository"}
        },
    )


def swe_bench_task(context: dict[str, Any]) -> CodingTask:
    statement = str(
        context.get("problem_statement")
        or context.get("body")
        or ""
    ).strip()
    objective = str(context.get("title") or "").strip()
    if not objective:
        objective = statement.splitlines()[0].strip()
    return CodingTask(
        id=str(context.get("instance_id") or "") or None,
        objective=objective,
        description=statement,
        intent=TaskIntent.change,
        source=TaskSource.swe_bench,
        delivery=DeliveryTarget.patch,
        repository={"repo": context.get("repo")},
        metadata={
            key: value
            for key, value in context.items()
            if key
            not in {
                "title",
                "body",
                "problem_statement",
                "repo",
            }
        },
    )


def interactive_task(
    objective: str,
    *,
    description: str = "",
    intent: TaskIntent = TaskIntent.change,
    requirements: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> CodingTask:
    delivery = (
        DeliveryTarget.workspace
        if intent == TaskIntent.change
        else DeliveryTarget.report
    )
    return CodingTask(
        objective=objective,
        description=description,
        intent=intent,
        source=TaskSource.interactive,
        requirements=list(requirements or []),
        acceptance_criteria=list(acceptance_criteria or []),
        delivery=delivery,
    )
