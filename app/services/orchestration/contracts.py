from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentGraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    agent: str
    depends_on: list[str] = Field(default_factory=list)
    activation_reason: str
    tool_profile: Literal["read_only", "workspace", "review"]
    max_turns: int = Field(ge=1)
    token_budget: int = Field(ge=1)
    status: Literal["planned", "running", "completed", "failed", "skipped"] = (
        "planned"
    )


class AgentGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1"
    graph_id: str
    strategy: Literal[
        "single_agent",
        "evidence_first",
        "deep_review",
        "task_only",
    ]
    risk_signals: list[str] = Field(default_factory=list)
    nodes: list[AgentGraphNode] = Field(min_length=1)
    total_token_budget: int = Field(ge=1)


class RequirementRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str = Field(min_length=1)
    source: Literal[
        "task_objective",
        "task_contract",
        "issue_title",
        "issue_body",
        "integration_request",
    ]


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    path: str
    line: int | None = Field(default=None, ge=1)
    symbol: str | None = None
    reason: str
    content_sha256: str | None = None


class ClaimRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    statement: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class PatchHunkRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    path: str
    header: str
    added_lines: int = Field(ge=0)
    deleted_lines: int = Field(ge=0)
    requirement_ids: list[str] = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)


class VerificationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    command: str
    status: Literal["passed", "failed", "not_run"]
    requirement_ids: list[str] = Field(min_length=1)
    exit_code: int | None = None
    output_tail: str = ""


class EvidenceGate(BaseModel):
    passed: bool
    stage: Literal["patch", "verification"]
    reasons: list[str] = Field(default_factory=list)
    requirement_coverage: float = Field(ge=0.0, le=1.0)
    patch_claim_coverage: float = Field(ge=0.0, le=1.0)
    verification_coverage: float = Field(ge=0.0, le=1.0)


class EvidenceLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1"
    requirements: list[RequirementRecord] = Field(min_length=1)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(min_length=1)
    patch_hunks: list[PatchHunkRecord] = Field(min_length=1)
    verifications: list[VerificationRecord] = Field(default_factory=list)
    require_grounded_evidence: bool = True
    gate: EvidenceGate | None = None


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    event_type: Literal[
        "runtime_start",
        "runtime_end",
        "phase_start",
        "phase_end",
        "gate_passed",
        "gate_failed",
    ]
    phase: str | None = None
    payload: dict = Field(default_factory=dict)
    elapsed_ms: int = Field(ge=0)


class PatchCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    strategy: str
    status: Literal["valid", "failed"]
    score: float
    patch: str = ""
    tests_passed: bool = False
    evidence_passed: bool = False
    files_changed: int = Field(ge=0)
    diff_lines: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    error: str = ""


class PatchTournamentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1"
    triggered: bool = True
    trigger_reason: str
    selected_candidate_id: str
    candidates: list[PatchCandidate] = Field(min_length=2)
