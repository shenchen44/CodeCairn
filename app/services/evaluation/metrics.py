from collections import Counter
from statistics import mean

from app.db.models.task import Task, TaskArtifactType, TaskStatus


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _phase_results(task: Task, phase: str) -> list[dict]:
    return [
        artifact.content.get("result", {})
        for artifact in task.artifacts
        if artifact.artifact_type == TaskArtifactType.agent_phase
        and isinstance(artifact.content, dict)
        and artifact.content.get("phase") == phase
        and isinstance(artifact.content.get("result"), dict)
    ]


def _task_variant(task: Task) -> str:
    decisions = _phase_results(task, "supervisor")
    if not decisions:
        return "unversioned"
    return str(decisions[-1].get("variant") or "unversioned")


def _variant_metrics(tasks: list[Task]) -> dict:
    grouped: dict[str, list[Task]] = {}
    for task in tasks:
        grouped.setdefault(_task_variant(task), []).append(task)
    result = {}
    for variant, variant_tasks in sorted(grouped.items()):
        completed = [
            task
            for task in variant_tasks
            if task.status in {TaskStatus.done, TaskStatus.failed}
        ]
        resolved = [
            task for task in completed if task.status == TaskStatus.done
        ]
        model_artifacts = [
            artifact
            for task in variant_tasks
            for artifact in task.artifacts
            if artifact.artifact_type == TaskArtifactType.model_response
            and isinstance(artifact.content, dict)
        ]
        result[variant] = {
            "tasks": len(variant_tasks),
            "completed": len(completed),
            "resolved": len(resolved),
            "resolved_rate": _ratio(len(resolved), len(completed)),
            "input_tokens": sum(
                int(item.content.get("total_input_tokens") or 0)
                for item in model_artifacts
            ),
            "output_tokens": sum(
                int(item.content.get("total_output_tokens") or 0)
                for item in model_artifacts
            ),
            "model_calls": sum(
                task.model_call_count for task in variant_tasks
            ),
            "tool_calls": sum(
                task.tool_call_count for task in variant_tasks
            ),
        }
    return result


def build_agent_metrics(tasks: list[Task]) -> dict:
    completed = [
        task
        for task in tasks
        if task.status in {TaskStatus.done, TaskStatus.failed}
    ]
    resolved = [task for task in completed if task.status == TaskStatus.done]
    retried = [task for task in completed if task.attempt_count > 1]

    attempts = [attempt for task in tasks for attempt in task.attempts]
    no_patch_attempts = [
        attempt
        for attempt in attempts
        if not (attempt.patch_text or "").strip()
        and (attempt.diff_line_count or 0) == 0
    ]

    phase_counts: Counter[str] = Counter()
    localization_results: list[dict] = []
    supervisor_results: list[dict] = []
    review_results: list[dict] = []
    graph_results: list[dict] = []
    evidence_ledgers: list[dict] = []
    tournament_results: list[dict] = []
    recovery_results: list[dict] = []
    for task in tasks:
        for artifact in task.artifacts:
            if (
                artifact.artifact_type != TaskArtifactType.agent_phase
                or not isinstance(artifact.content, dict)
            ):
                continue
            phase = str(artifact.content.get("phase", "unknown"))
            phase_counts[phase] += 1
        localization_results.extend(_phase_results(task, "localization"))
        supervisor_results.extend(_phase_results(task, "supervisor"))
        review_results.extend(_phase_results(task, "review"))
        graph_results.extend(_phase_results(task, "agent_graph"))
        evidence_ledgers.extend(
            _phase_results(task, "evidence_ledger")
        )
        tournament_results.extend(
            _phase_results(task, "patch_tournament")
        )
        recovery_results.extend(
            _phase_results(task, "patch_recovery")
        )

    localization_passed = sum(
        result.get("gate", {}).get("passed") is not False
        for result in localization_results
    )
    deep_routes = sum(
        result.get("mode") == "deep_review"
        for result in supervisor_results
    )
    approved_reviews = sum(
        result.get("verdict") == "approved"
        and result.get("gate", {}).get("passed") is not False
        for result in review_results
    )
    evidence_gates_passed = sum(
        ledger.get("gate", {}).get("passed") is True
        for ledger in evidence_ledgers
    )
    requirement_coverages = [
        float(ledger.get("gate", {}).get("requirement_coverage") or 0)
        for ledger in evidence_ledgers
    ]
    graph_strategies = Counter(
        str(graph.get("strategy", "unknown"))
        for graph in graph_results
    )

    model_artifacts = [
        artifact
        for task in tasks
        for artifact in task.artifacts
        if artifact.artifact_type == TaskArtifactType.model_response
        and isinstance(artifact.content, dict)
    ]
    total_input_tokens = sum(
        int(artifact.content.get("total_input_tokens") or 0)
        for artifact in model_artifacts
    )
    total_output_tokens = sum(
        int(artifact.content.get("total_output_tokens") or 0)
        for artifact in model_artifacts
    )
    durations = [
        task.total_duration_ms
        for task in completed
        if task.total_duration_ms is not None
    ]
    failure_buckets = Counter(
        str((task.failure_reason or {}).get("reason", "unknown"))
        for task in completed
        if task.status == TaskStatus.failed
    )

    return {
        "tasks": {
            "total": len(tasks),
            "completed": len(completed),
            "resolved": len(resolved),
            "resolved_rate": _ratio(len(resolved), len(completed)),
            "retry_rate": _ratio(len(retried), len(completed)),
        },
        "attempts": {
            "total": len(attempts),
            "no_patch": len(no_patch_attempts),
            "no_patch_rate": _ratio(len(no_patch_attempts), len(attempts)),
        },
        "routing": {
            "decisions": len(supervisor_results),
            "deep_review": deep_routes,
            "deep_review_rate": _ratio(deep_routes, len(supervisor_results)),
        },
        "gates": {
            "localization_total": len(localization_results),
            "localization_passed": localization_passed,
            "localization_pass_rate": _ratio(
                localization_passed,
                len(localization_results),
            ),
            "review_total": len(review_results),
            "review_approved": approved_reviews,
            "review_approval_rate": _ratio(
                approved_reviews,
                len(review_results),
            ),
            "evidence_total": len(evidence_ledgers),
            "evidence_passed": evidence_gates_passed,
            "evidence_pass_rate": _ratio(
                evidence_gates_passed,
                len(evidence_ledgers),
            ),
            "average_requirement_coverage": (
                round(mean(requirement_coverages), 4)
                if requirement_coverages
                else 0.0
            ),
        },
        "orchestration": {
            "graphs": len(graph_results),
            "strategies": dict(sorted(graph_strategies.items())),
            "tournaments": len(tournament_results),
            "tournament_candidates": sum(
                len(item.get("candidates", []))
                for item in tournament_results
            ),
            "recoveries": len(recovery_results),
            "recovery_succeeded": sum(
                item.get("status") == "recovered"
                for item in recovery_results
            ),
        },
        "cost": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "model_calls": sum(task.model_call_count for task in tasks),
            "tool_calls": sum(task.tool_call_count for task in tasks),
        },
        "latency": {
            "average_task_duration_ms": (
                round(mean(durations), 2) if durations else 0.0
            ),
        },
        "phase_counts": dict(sorted(phase_counts.items())),
        "failure_buckets": dict(sorted(failure_buckets.items())),
        "variants": _variant_metrics(tasks),
    }
