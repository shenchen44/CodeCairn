from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ProvenanceKind = Literal["captured", "derived", "verified", "inferred", "unknown"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProvenanceKind
    source: str
    source_event_ids: list[str] = Field(default_factory=list)
    model: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utc_now)


class RepositorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str
    name: str
    branch: str


class GitSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_ref: str
    base_sha: str
    head_sha: str
    workspace_tree_sha: str
    git_snapshot_id: str = ""
    content_tree_hash: str = ""
    patch_fingerprint: str = ""
    revision_id: str = ""
    captured_at: datetime = Field(default_factory=utc_now)
    file_hashes: dict[str, str] = Field(default_factory=dict)
    is_dirty: bool = False


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    category: Literal["requirement", "acceptance_criterion", "constraint"] = (
        "requirement"
    )
    original_text: str
    revision: int = 1
    deleted: bool = False
    provenance: Provenance


class RequirementRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_id: str
    revision: int = Field(ge=1)
    text: str
    original_text: str
    category: Literal["requirement", "acceptance_criterion", "constraint"]
    deleted: bool = False
    actor: str
    revised_at: datetime = Field(default_factory=utc_now)
    source_event_id: str | None = None


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    path: str
    line: int | None = None
    symbol: str | None = None
    statement: str
    content_sha256: str | None = None
    content_excerpt: str = ""
    stale: bool = False
    provenance: Provenance


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["proposed", "confirmed", "rejected"] = "proposed"
    provenance: Provenance


class PatchHunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    file_change_id: str
    path: str
    old_path: str | None = None
    header: str
    old_start: int = 0
    old_count: int = 0
    new_start: int = 0
    new_count: int = 0
    added_lines: int = 0
    deleted_lines: int = 0
    diff: str
    summary: str
    requirement_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    reviewed: bool = False
    provenance: Provenance


class Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    from_id: str
    to_id: str
    relation: str
    explanation: str
    confirmed: bool = False
    provenance: Provenance


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    change_type: Literal["added", "modified", "deleted", "renamed", "binary"]
    path: str
    old_path: str | None = None
    old_content_sha256: str | None = None
    new_content_sha256: str | None = None
    binary: bool = False
    summary: str
    requirement_ids: list[str] = Field(default_factory=list)
    reviewed: bool = False
    provenance: Provenance


class VerificationEnvironmentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner: str = ""
    platform: str = ""
    language: str = ""
    image: str = ""
    toolchain: str = ""
    supported: bool = True
    unsupported_reason: str | None = None
    user: str = ""
    preflight_status: Literal[
        "passed", "failed", "not_run"
    ] = "not_run"


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    command: str
    command_argv: list[str] = Field(default_factory=list)
    result_status: Literal["passed", "failed", "not_run", "aborted"]
    effective_status: Literal[
        "passed", "failed", "not_run", "aborted", "stale"
    ]
    requirement_ids: list[str] = Field(default_factory=list)
    hunk_ids: list[str] = Field(default_factory=list)
    file_change_ids: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    output_summary: str = ""
    commit_sha: str
    workspace_tree_sha: str
    content_tree_hash: str = ""
    patch_fingerprint: str = ""
    prepare_dependencies: bool = False
    install_command: str | None = None
    install_command_argv: list[str] = Field(default_factory=list)
    install_status: Literal["passed", "failed", "not_run"] = "not_run"
    install_output_summary: str = ""
    install_network_enabled: bool = False
    test_network_enabled: bool = False
    environment_status: Literal["supported", "unsupported"] = "supported"
    environment: VerificationEnvironmentRecord = Field(
        default_factory=VerificationEnvironmentRecord
    )
    provenance: Provenance

    @property
    def status(self) -> str:
        """Compatibility view; effective_status is the canonical field."""
        return self.effective_status


class ResidualRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    code: str
    severity: Literal["high", "medium", "low"]
    statement: str
    rationale: str
    related_ids: list[str] = Field(default_factory=list)
    status: Literal["open", "accepted", "resolved"] = "open"
    provenance: Provenance


class CoverageAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    verification_id: str
    target_type: Literal["requirement", "hunk", "file_change"]
    target_id: str
    status: Literal["proposed", "confirmed", "rejected"] = "proposed"
    explanation: str
    provenance: Provenance
    created_at: datetime = Field(default_factory=utc_now)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    target_type: Literal[
        "claim",
        "mapping",
        "coverage_assertion",
        "hunk_review",
        "file_review",
        "risk",
    ]
    target_id: str
    decision: Literal["confirmed", "rejected", "revoked"]
    explanation: str = ""
    reviewer: str
    decided_at: datetime = Field(default_factory=utc_now)
    source_event_id: str


class GitSnapshotRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str
    git_snapshot_id: str
    content_tree_hash: str
    patch_fingerprint: str
    head_sha: str
    branch: str
    dirty: bool
    transition: Literal[
        "initial", "git_state_transition", "commit_transition", "content_change"
    ]
    captured_at: datetime = Field(default_factory=utc_now)


class Publication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Literal["github"] = "github"
    target: Literal["description", "comment", "check"]
    repository: str
    pr_number: int = Field(ge=1)
    remote_id: str
    url: str = ""
    published_at: datetime = Field(default_factory=utc_now)
    local_head_sha: str
    remote_head_sha: str
    patch_fingerprint: str
    review_family_id: str
    change_id: str
    revision_id: str = ""
    content_hash: str
    provenance: Provenance


class ReviewMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    role: Literal["user", "assistant", "system"]
    content: str
    agent_run_id: str | None = None
    references: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ReviewThread(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    target_type: Literal["change", "file", "hunk", "evidence", "decision"]
    target_id: str = ""
    status: Literal["open", "resolved"] = "open"
    messages: list[ReviewMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    thread_id: str
    prompt: str
    target_type: Literal["change", "file", "hunk"]
    target_id: str = ""
    source_revision_id: str
    result_revision_id: str = ""
    agent_run_id: str = ""
    status: Literal[
        "queued", "running", "completed", "failed", "cancelled"
    ] = "queued"
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class AgentRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    thread_id: str
    mode: Literal["ask", "change"]
    prompt: str
    target_type: Literal[
        "change", "file", "hunk", "evidence", "decision"
    ] = "change"
    target_id: str = ""
    status: Literal[
        "queued", "starting", "running", "completed", "failed", "cancelled"
    ] = "queued"
    provider: str = "pi"
    session_id: str = ""
    process_id: int | None = None
    answer: str = ""
    event_count: int = 0
    error: str = ""
    source_revision_id: str
    result_revision_id: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DeliveryStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal[
        "preflight",
        "branch",
        "stage",
        "commit",
        "push",
        "pull_request",
        "evidence",
    ]
    status: Literal["pending", "running", "completed", "failed", "skipped"]
    detail: str = ""
    updated_at: datetime = Field(default_factory=utc_now)


class DeliveryRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal[
        "queued",
        "preflight",
        "committing",
        "pushing",
        "creating_pr",
        "publishing_evidence",
        "completed",
        "failed",
    ] = "queued"
    repository: str = ""
    base_branch: str
    branch: str = ""
    selected_paths: list[str] = Field(default_factory=list)
    commit_message: str
    commit_sha: str = ""
    pr_number: int | None = None
    pr_url: str = ""
    steps: list[DeliveryStep] = Field(default_factory=list)
    error: str = ""
    retryable: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class CIManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1", "2"] = "2"
    repository: str
    workflow_path: str = ""
    workflow_ref: str = ""
    event_name: str = ""
    pr_number: int | None = Field(default=None, ge=1)
    head_sha: str
    base_sha: str
    patch_fingerprint: str
    requirement_contract_hash: str
    command_argv: list[str]
    requirement_ids: list[str] = Field(default_factory=list)
    hunk_ids: list[str] = Field(default_factory=list)
    file_change_ids: list[str] = Field(default_factory=list)


class CISnapshotMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    head_sha: str
    base_sha: str
    content_tree_hash: str
    patch_fingerprint: str
    tracked_tree_hash: str
    measured_at: datetime = Field(default_factory=utc_now)


class CIVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1", "2"] = "2"
    result_kind: Literal["observation", "legacy_result"] = "observation"
    run_id: str
    run_attempt: int = Field(default=1, ge=1)
    provider: str
    repository: str
    workflow_path: str = ""
    workflow_ref: str = ""
    event_name: str = ""
    pr_number: int | None = Field(default=None, ge=1)
    head_sha: str
    base_sha: str
    patch_fingerprint: str
    requirement_contract_hash: str
    command_argv: list[str]
    result: Literal["passed", "failed", "aborted", "not_run"]
    exit_code: int | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    output_hash: str
    requirement_ids: list[str] = Field(default_factory=list)
    hunk_ids: list[str] = Field(default_factory=list)
    file_change_ids: list[str] = Field(default_factory=list)
    artifact_id: str = ""
    artifact_digest: str = ""
    artifact_size: int | None = Field(default=None, ge=0)
    pre_snapshot: CISnapshotMeasurement | None = None
    post_snapshot: CISnapshotMeasurement | None = None
    signer: str = ""
    signature: str = ""
    trusted: bool = False
    trust_reason: str = "unsigned"
    trust_source: str = "observation"
    policy_hash: str = ""
    attestation_id: str = ""
    attestation_issuer: str = ""
    provenance: Provenance


class CIAttestation(BaseModel):
    """Independent proof over an immutable CI observation and run identity."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    attestation_id: str
    observation_digest: str
    repository: str
    workflow_path: str
    workflow_ref: str
    run_id: str
    run_attempt: int = Field(ge=1)
    event_name: str
    pr_number: int = Field(ge=1)
    head_sha: str
    base_sha: str
    artifact_id: str
    artifact_digest: str
    result_schema_version: str
    patch_fingerprint: str
    requirement_contract_hash: str
    command_argv: list[str]
    output_hash: str
    requirement_ids: list[str] = Field(default_factory=list)
    hunk_ids: list[str] = Field(default_factory=list)
    file_change_ids: list[str] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime
    issuer: str
    issued_at: datetime
    expires_at: datetime
    signature: str = ""
    provenance: Provenance


class CIArtifactDownload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    name: str
    size_in_bytes: int = Field(ge=0)
    expired: bool = False
    declared_digest: str = ""
    measured_digest: str = ""
    content: bytes = b""
    error: str = ""


class LedgerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    sequence: int = Field(ge=1)
    event_type: str
    actor_type: Literal["reviewer", "sandbox", "system", "adapter", "model"]
    actor_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    previous_event_hash: str = ""
    event_hash: str


class DecisionEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    statement: str
    line: int | None = Field(default=None, ge=1)
    symbol: str | None = None
    source_event_id: str | None = None


class DecisionRecord(BaseModel):
    """A structured, reviewable explanation created before a mutation."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    summary: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=10000)
    alternatives: list[str] = Field(default_factory=list)
    affected_paths: list[str] = Field(default_factory=list)
    evidence: list[DecisionEvidenceReference] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    verification_plan: list[str] = Field(default_factory=list)
    status: Literal["draft", "accepted", "superseded"] = "accepted"
    created_at: datetime = Field(default_factory=utc_now)


class CaptureEvent(BaseModel):
    """Host-neutral event envelope used by Pi, Claude Code and future adapters."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1", "2", "3"] = "1"
    event_id: str
    session_id: str
    host: Literal[
        "pi", "claude_code", "codex", "cursor", "manual", "unknown"
    ]
    event_type: str
    sequence: int = Field(default=0, ge=0)
    timestamp: datetime = Field(default_factory=utc_now)
    ingestion_timestamp: datetime = Field(default_factory=utc_now)
    cwd: str
    repository: str
    repository_id: str
    git_snapshot_id: str = ""
    parent_event_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str
    previous_event_hash: str = ""
    event_hash: str = ""
    integrity_status: Literal["unverified", "valid"] = "unverified"
    provenance: Provenance


class GateCoverage(BaseModel):
    requirement_hunk: float = Field(ge=0.0, le=1.0)
    hunk: float = Field(default=0.0, ge=0.0, le=1.0)
    file_change: float = Field(default=0.0, ge=0.0, le=1.0)
    claim_evidence: float = Field(ge=0.0, le=1.0)
    verification: float = Field(ge=0.0, le=1.0)


class EvidenceGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "warning", "blocked"]
    policy: str = "local_review"
    reasons: list[str] = Field(default_factory=list)
    coverage: GateCoverage


class Assurance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["unrated", "low", "medium", "high"]
    reasons: list[str] = Field(default_factory=list)


class ChangeProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    change_id: str
    revision_id: str = ""
    review_family_id: str
    review_series_id: str
    parent_change_id: str | None = None
    revision_number: int = Field(default=1, ge=1)
    requirement_contract_hash: str
    requirement_contract_revision: int = Field(default=1, ge=1)
    title: str
    repository: RepositorySnapshot
    git_snapshot: GitSnapshot
    requirements: list[Requirement]
    requirement_revisions: list[RequirementRevision] = Field(
        default_factory=list
    )
    git_snapshot_revisions: list[GitSnapshotRevision] = Field(
        default_factory=list
    )
    claims: list[Claim]
    evidence: list[Evidence]
    file_changes: list[FileChange] = Field(default_factory=list)
    patch_hunks: list[PatchHunk]
    mappings: list[Mapping] = Field(default_factory=list)
    impact_relations: list[Mapping] = Field(default_factory=list)
    verifications: list[Verification] = Field(default_factory=list)
    coverage_assertions: list[CoverageAssertion] = Field(default_factory=list)
    review_decisions: list[ReviewDecision] = Field(default_factory=list)
    capture_events: list[CaptureEvent] = Field(default_factory=list)
    decision_records: list[DecisionRecord] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    review_threads: list[ReviewThread] = Field(default_factory=list)
    change_requests: list[ChangeRequest] = Field(default_factory=list)
    agent_runs: list[AgentRun] = Field(default_factory=list)
    delivery_runs: list[DeliveryRun] = Field(default_factory=list)
    ci_verifications: list[CIVerificationResult] = Field(default_factory=list)
    ci_attestations: list[CIAttestation] = Field(default_factory=list)
    risks: list[ResidualRisk] = Field(default_factory=list)
    assurance: Assurance
    gate: EvidenceGate
    audit_events: list[LedgerEvent] = Field(default_factory=list)
    ledger_integrity: bool = True
    last_event_hash: str = ""
    storage_migrations: list[str] = Field(default_factory=list)
