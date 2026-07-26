from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class LocalizationStatus(str, Enum):
    ready = "ready"
    insufficient = "insufficient"


class CodeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    symbol: str | None = None
    reason: str = Field(min_length=1)


class BehavioralHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis: str = Field(min_length=1)
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    falsification_test: str = ""


class LocalizationResult(BaseModel):
    """Structured hand-off from the read-only localization phase."""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1"
    status: LocalizationStatus
    issue_summary: str = Field(min_length=1)
    candidate_files: list[str] = Field(default_factory=list)
    suspected_symbols: list[str] = Field(default_factory=list)
    evidence: list[CodeEvidence] = Field(default_factory=list)
    root_cause_hypothesis: str = ""
    behavioral_contracts: list[str] = Field(default_factory=list)
    alternative_hypotheses: list[BehavioralHypothesis] = Field(
        default_factory=list
    )
    confidence: float = Field(ge=0.0, le=1.0)
    missing_information: list[str] = Field(default_factory=list)


class LocalizationGateDecision(BaseModel):
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class ExecutionMode(str, Enum):
    standard = "standard"
    deep_review = "deep_review"


class SupervisorDecision(BaseModel):
    contract_version: str = "1"
    variant: str = "full"
    mode: ExecutionMode
    complexity_score: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    required_agents: list[str] = Field(default_factory=list)


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1)
    description: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class PatchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1"
    objective: str = Field(min_length=1)
    steps: list[PlanStep] = Field(min_length=1)
    test_strategy: list[str] = Field(min_length=1)
    risk_level: RiskLevel
    rollback_strategy: str = Field(min_length=1)


class ReviewVerdict(str, Enum):
    approved = "approved"
    needs_revision = "needs_revision"


class ReviewSeverity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: ReviewSeverity
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1"
    verdict: ReviewVerdict
    summary: str = Field(min_length=1)
    findings: list[ReviewFinding] = Field(default_factory=list)
    behavior_contracts_covered: bool = True
    hypotheses_considered: list[str] = Field(default_factory=list)
    test_gaps: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class ExactEditOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    rationale: str = Field(min_length=1)


class PatchRecoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1"
    selected_hypothesis: str = Field(min_length=1)
    rejected_hypotheses: list[str] = Field(default_factory=list)
    behavior_contracts: list[str] = Field(default_factory=list)
    operations: list[ExactEditOperation] = Field(
        min_length=1,
        max_length=5,
    )
    test_expectation: str = Field(min_length=1)


def evaluate_localization_gate(
    result: LocalizationResult,
    *,
    minimum_confidence: float = 0.55,
) -> LocalizationGateDecision:
    """Require grounded code evidence before the patching phase can mutate files."""

    reasons: list[str] = []
    if result.status != LocalizationStatus.ready:
        reasons.append("localization_status_not_ready")
    if not result.candidate_files:
        reasons.append("candidate_files_missing")
    if not result.evidence:
        reasons.append("code_evidence_missing")
    if not result.root_cause_hypothesis.strip():
        reasons.append("root_cause_hypothesis_missing")
    if result.confidence < minimum_confidence:
        reasons.append("localization_confidence_below_threshold")

    candidate_files = set(result.candidate_files)
    evidence_files = {item.path for item in result.evidence}
    if candidate_files and evidence_files and not (
        candidate_files & evidence_files
    ):
        reasons.append("candidate_files_not_grounded_by_evidence")

    return LocalizationGateDecision(passed=not reasons, reasons=reasons)


def evaluate_plan_gate(
    plan: PatchPlan,
    localization: LocalizationResult,
) -> LocalizationGateDecision:
    candidate_files = set(localization.candidate_files)
    planned_files = {
        path
        for step in plan.steps
        for path in step.files
    }
    reasons: list[str] = []
    if planned_files - candidate_files:
        reasons.append("plan_references_unlocalized_files")
    if [step.order for step in plan.steps] != list(
        range(1, len(plan.steps) + 1)
    ):
        reasons.append("plan_step_order_invalid")
    return LocalizationGateDecision(passed=not reasons, reasons=reasons)


def evaluate_review_gate(review: ReviewResult) -> LocalizationGateDecision:
    reasons: list[str] = []
    if review.verdict != ReviewVerdict.approved:
        reasons.append("review_needs_revision")
    if any(
        finding.severity == ReviewSeverity.high
        for finding in review.findings
    ):
        reasons.append("review_contains_high_severity_finding")
    if not review.behavior_contracts_covered:
        reasons.append("review_behavior_contracts_not_covered")
    return LocalizationGateDecision(passed=not reasons, reasons=reasons)
