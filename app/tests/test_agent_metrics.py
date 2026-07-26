from app.db.models.task import (
    TaskArtifact,
    TaskArtifactType,
    TaskAttempt,
    TaskResultStatus,
    TaskStatus,
)
from app.services.task_runner.orchestrator import create_task_from_webhook


def test_dashboard_metrics_aggregate_agent_phases_and_cost(
    client,
    db_session,
    sample_issue_payload,
) -> None:
    task = create_task_from_webhook(db_session, sample_issue_payload)
    task.status = TaskStatus.done
    task.attempt_count = 2
    task.total_duration_ms = 1200
    task.model_call_count = 5
    task.tool_call_count = 7
    db_session.add(task)
    db_session.add(
        TaskAttempt(
            task_id=task.id,
            attempt_index=1,
            patch_text="",
            diff_line_count=0,
            result_status=TaskResultStatus.failed,
        )
    )
    db_session.add(
        TaskAttempt(
            task_id=task.id,
            attempt_index=2,
            patch_text="",
            diff_line_count=3,
            result_status=TaskResultStatus.success,
        )
    )
    phase_results = {
        "supervisor": {"mode": "deep_review", "variant": "full"},
        "localization": {"status": "ready", "confidence": 0.9},
        "planning": {"risk_level": "low"},
        "review": {"verdict": "approved"},
        "agent_graph": {"strategy": "deep_review", "nodes": []},
        "evidence_ledger": {
            "gate": {
                "passed": True,
                "requirement_coverage": 1.0,
            }
        },
    }
    for phase, result in phase_results.items():
        db_session.add(
            TaskArtifact(
                task_id=task.id,
                artifact_type=TaskArtifactType.agent_phase,
                content={"attempt": 2, "phase": phase, "result": result},
            )
        )
    db_session.add(
        TaskArtifact(
            task_id=task.id,
            artifact_type=TaskArtifactType.model_response,
            content={"total_input_tokens": 1000, "total_output_tokens": 250},
        )
    )
    db_session.commit()

    response = client.get("/dashboard/metrics")

    assert response.status_code == 200
    metrics = response.json()
    assert metrics["tasks"]["resolved_rate"] == 1.0
    assert metrics["tasks"]["retry_rate"] == 1.0
    assert metrics["attempts"]["no_patch_rate"] == 0.5
    assert metrics["routing"]["deep_review_rate"] == 1.0
    assert metrics["gates"]["localization_pass_rate"] == 1.0
    assert metrics["gates"]["review_approval_rate"] == 1.0
    assert metrics["gates"]["evidence_pass_rate"] == 1.0
    assert metrics["gates"]["average_requirement_coverage"] == 1.0
    assert metrics["orchestration"]["strategies"]["deep_review"] == 1
    assert metrics["cost"]["input_tokens"] == 1000
    assert metrics["latency"]["average_task_duration_ms"] == 1200.0
    assert metrics["variants"]["full"]["resolved_rate"] == 1.0
