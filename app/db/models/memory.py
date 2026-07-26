import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MemoryScope(str, enum.Enum):
    task = "task"
    repository = "repository"


class MemoryKind(str, enum.Enum):
    localization = "localization"
    failure = "failure"
    solution = "solution"
    review = "review"


class AgentMemory(Base):
    __tablename__ = "agent_memories"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"),
        index=True,
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    scope: Mapped[MemoryScope] = mapped_column(Enum(MemoryScope), index=True)
    kind: Mapped[MemoryKind] = mapped_column(Enum(MemoryKind), index=True)
    content: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[list | dict] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    source_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
