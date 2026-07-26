from app.db.models.memory import MemoryKind, MemoryScope
from app.services.memory import invalidate_memory, recall, remember
from app.services.memory import snapshot_evidence
from app.services.task_runner.orchestrator import create_task_from_webhook
from app.workers.poller import _build_issue_context


def test_memory_deduplicates_and_keeps_highest_confidence(
    db_session,
    sample_issue_payload,
) -> None:
    task = create_task_from_webhook(db_session, sample_issue_payload)
    content = {"root_cause": "format_display_name lacks a None guard"}

    first = remember(
        db_session,
        repository_id=task.repository_id,
        task_id=task.id,
        scope=MemoryScope.task,
        kind=MemoryKind.failure,
        content=content,
        confidence=0.4,
    )
    second = remember(
        db_session,
        repository_id=task.repository_id,
        task_id=task.id,
        scope=MemoryScope.task,
        kind=MemoryKind.failure,
        content=content,
        confidence=0.8,
    )
    db_session.commit()

    assert first.id == second.id
    assert second.confidence == 0.8


def test_recall_combines_task_and_repository_memory(
    db_session,
    sample_issue_payload,
) -> None:
    task = create_task_from_webhook(db_session, sample_issue_payload)
    repository_memory = remember(
        db_session,
        repository_id=task.repository_id,
        scope=MemoryScope.repository,
        kind=MemoryKind.solution,
        content={"solution": "guard None before display formatting"},
        confidence=0.95,
        source_commit="abc123",
    )
    task_memory = remember(
        db_session,
        repository_id=task.repository_id,
        task_id=task.id,
        scope=MemoryScope.task,
        kind=MemoryKind.failure,
        content={"failure": "display assertion still fails for None"},
        confidence=0.7,
    )
    db_session.commit()

    task_results = recall(
        db_session,
        repository_id=task.repository_id,
        task_id=task.id,
        query="display None failure",
    )
    repository_results = recall(
        db_session,
        repository_id=task.repository_id,
        query="display None failure",
    )

    assert {item["id"] for item in task_results} == {
        repository_memory.id,
        task_memory.id,
    }
    assert [item["id"] for item in repository_results] == [
        repository_memory.id
    ]


def test_invalidated_memory_is_not_recalled(
    db_session,
    sample_issue_payload,
) -> None:
    task = create_task_from_webhook(db_session, sample_issue_payload)
    memory = remember(
        db_session,
        repository_id=task.repository_id,
        scope=MemoryScope.repository,
        kind=MemoryKind.solution,
        content={"solution": "fix display None handling"},
        confidence=0.9,
    )
    db_session.commit()

    assert invalidate_memory(db_session, memory.id) is True
    db_session.commit()

    assert recall(
        db_session,
        repository_id=task.repository_id,
        query="display None",
    ) == []


def test_issue_context_includes_relevant_task_memory(
    db_session,
    sample_issue_payload,
) -> None:
    task = create_task_from_webhook(db_session, sample_issue_payload)
    remember(
        db_session,
        repository_id=task.repository_id,
        task_id=task.id,
        scope=MemoryScope.task,
        kind=MemoryKind.failure,
        content={"failure": "display_name None assertion failed"},
        confidence=0.8,
    )
    db_session.commit()

    context = _build_issue_context(task, db_session)

    assert len(context["memory_context"]) == 1
    assert context["memory_context"][0]["kind"] == "failure"


def test_repository_memory_is_invalidated_when_evidence_file_changes(
    db_session,
    sample_issue_payload,
    workspace_tmp_dir,
) -> None:
    task = create_task_from_webhook(db_session, sample_issue_payload)
    evidence_file = workspace_tmp_dir / "display.py"
    evidence_file.write_text("def display():\n    return 'old'\n", encoding="utf-8")
    evidence = snapshot_evidence(
        workspace_tmp_dir,
        [{"path": "display.py", "reason": "contains display behavior"}],
    )
    memory = remember(
        db_session,
        repository_id=task.repository_id,
        scope=MemoryScope.repository,
        kind=MemoryKind.solution,
        content={"solution": "fix display behavior"},
        evidence=evidence,
        confidence=0.9,
    )
    db_session.commit()

    assert recall(
        db_session,
        repository_id=task.repository_id,
        query="display behavior",
        repo_path=workspace_tmp_dir,
    )

    evidence_file.write_text(
        "def display():\n    return 'changed'\n",
        encoding="utf-8",
    )
    assert recall(
        db_session,
        repository_id=task.repository_id,
        query="display behavior",
        repo_path=workspace_tmp_dir,
    ) == []
    assert memory.invalidated_at is not None
