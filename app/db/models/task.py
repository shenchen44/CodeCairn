import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class TaskStatus(str, enum.Enum):
    received = "received"
    triaged = "triaged"
    sandbox_ready = "sandbox_ready"
    patching = "patching"
    testing = "testing"
    retrying = "retrying"
    ready_for_pr = "ready_for_pr"
    pr_opened = "pr_opened"
    failed = "failed"
    done = "done"


class TaskResultStatus(str, enum.Enum):
    success = "success"
    failed = "failed"
    skipped = "skipped"


class TaskArtifactType(str, enum.Enum):
    raw_webhook = "raw_webhook"
    issue_snapshot = "issue_snapshot"
    integration_request = "integration_request"
    resolution_link = "resolution_link"
    repo_tree = "repo_tree"
    prompt = "prompt"
    model_response = "model_response"
    diff = "diff"
    install_log = "install_log"
    test_log = "test_log"
    pr_body = "pr_body"
    agent_phase = "agent_phase"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.received, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    branch_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_reason: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    install_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    patch_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    test_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_call_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    repository = relationship("Repository", back_populates="tasks")
    issue = relationship("Issue", back_populates="tasks")
    attempts = relationship("TaskAttempt", back_populates="task", cascade="all, delete-orphan")
    artifacts = relationship("TaskArtifact", back_populates="task", cascade="all, delete-orphan")


class TaskAttempt(Base):
    __tablename__ = "task_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt_index: Mapped[int] = mapped_column(Integer)
    model_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    patch_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    files_changed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diff_line_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    test_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    test_stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    result_status: Mapped[TaskResultStatus] = mapped_column(Enum(TaskResultStatus), default=TaskResultStatus.failed)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    task = relationship("Task", back_populates="attempts")


class TaskArtifact(Base):
    __tablename__ = "task_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    artifact_type: Mapped[TaskArtifactType] = mapped_column(Enum(TaskArtifactType))
    content: Mapped[str | dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    task = relationship("Task", back_populates="artifacts")
