from __future__ import annotations

import html
import os
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from codecairn.verification.config import parse_command
from codecairn.verification.config import load_repo_config
from codecairn.verification.runner import (
    CommandResult,
    SandboxRunner,
    UnsupportedEnvironment,
    WorkspacePermissionDenied,
)
from codecairn.review.analyzer import (
    _id,
    _workspace_tree,
    build_file_comparison,
    build_change_proof,
    canonical_change_identity,
    requirement_contract_hash,
    refresh_evidence_stale,
    sync_hunk_requirement_ids,
)
from codecairn.review.models import (
    ChangeProof,
    CoverageAssertion,
    LedgerEvent,
    Mapping,
    Provenance,
    Requirement,
    RequirementRevision,
    ReviewDecision,
    GitSnapshotRevision,
    Verification,
)
from codecairn.review.ledger import (
    migrate_legacy_events,
    new_ledger_event,
    verify_ledger,
)
from codecairn.review.store import (
    ReviewStorageError,
    load_review,
    load_review_revision,
    load_review_revisions,
    load_review_family_revisions,
    register_review_revision,
    review_path,
    save_review,
)
from codecairn.review.capture import (
    CaptureStorageError,
    CaptureStore,
    capture_path,
    verify_capture_chain,
)
from codecairn.review.decision_compiler import compile_decision_events
from codecairn.review.graph import (
    ExportRenderError,
    build_evidence_graph,
    graph_html,
    graph_png,
    graph_svg,
)


class VerificationRunner(Protocol):
    def run_tests(self, repo_path: Path, test_command: str) -> CommandResult: ...

    def install_dependencies(
        self, repo_path: Path, install_command: str
    ) -> CommandResult: ...


class MappingUpdate(BaseModel):
    requirement_ids: list[str] = Field(default_factory=list)


class ReviewUpdate(BaseModel):
    reviewed: bool = True


class ClaimUpdate(BaseModel):
    status: str


class RiskUpdate(BaseModel):
    status: str


class CoverageAssertionUpdate(BaseModel):
    status: str
    explanation: str | None = None


class RequirementCreate(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    category: str = "requirement"


class RequirementUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    category: str = "requirement"


class VerificationRequest(BaseModel):
    command: str = Field(min_length=1, max_length=1000)
    requirement_ids: list[str] = Field(default_factory=list)
    hunk_ids: list[str] = Field(default_factory=list)
    file_change_ids: list[str] = Field(default_factory=list)
    prepare_dependencies: bool = False


class UnsafeExternalSymlink(RuntimeError):
    pass


def _snapshot_paths(repo: Path) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def build_verification_snapshot(repo: Path, destination: Path) -> None:
    """Copy Git-visible files without following symlinks or reading targets."""
    repo = repo.resolve()
    paths = _snapshot_paths(repo)
    for relative in paths:
        source = repo / relative
        try:
            stat = source.lstat()
        except FileNotFoundError:
            continue
        if source.is_symlink():
            target = os.readlink(source)
            resolved = (source.parent / target).resolve()
            try:
                resolved.relative_to(repo)
            except ValueError as exc:
                raise UnsafeExternalSymlink(
                    f"unsafe_external_symlink:{relative}"
                ) from exc
            target_path = destination / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.symlink_to(target)
            continue
        if not source.is_file():
            continue
        target_path = destination / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target_path, follow_symlinks=False)


SECRET_PATTERNS = [
    re.compile(
        r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"
    ),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|secret[_-]?key)"
        r"(\s*[:=]\s*)[^\s,;]+"
    ),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_-]{12,}|"
        r"github_pat_[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16}|"
        r"xox[baprs]-[A-Za-z0-9-]{10,})\b"
    ),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)"
        r"://[^\s]+"
    ),
]


def redact_sensitive_output(value: str, limit: int = 4000) -> str:
    redacted = value
    for index, pattern in enumerate(SECRET_PATTERNS):
        if index == 0:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        elif index == 1:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted[-limit:]


@dataclass(slots=True)
class ReviewState:
    repo: Path
    proof: ChangeProof
    runner: VerificationRunner = field(default_factory=SandboxRunner)
    storage_path: Path | None = None
    storage_root: Path | None = None
    audit_sequence: int = 0
    current_stale: bool = False

    def __post_init__(self) -> None:
        self.repo = self.repo.resolve()
        if any(
            not isinstance(item, LedgerEvent)
            for item in self.proof.audit_events
        ):
            self.proof.audit_events = migrate_legacy_events(
                self.proof.audit_events,
                default_timestamp=self.proof.git_snapshot.captured_at,
            )
            self.proof.storage_migrations.append("legacy_audit_events_to_ledger")
        self.audit_sequence = max(
            (item.sequence for item in self.proof.audit_events),
            default=0,
        )
        self.refresh()

    def persist(self) -> None:
        if self.storage_path is not None:
            save_review(self.proof, self.storage_path)

    def audit(
        self,
        action: str,
        details: dict,
        *,
        actor_type: str = "reviewer",
        actor_id: str = "local_reviewer",
        event_id: str | None = None,
    ) -> LedgerEvent:
        self.audit_sequence += 1
        event = new_ledger_event(
            sequence=self.audit_sequence,
            event_type=action,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=details,
            previous_event_hash=self.proof.last_event_hash,
            event_id=event_id,
        )
        self.proof.audit_events.append(event)
        self.proof.last_event_hash = event.event_hash
        self.proof.ledger_integrity = True
        return event

    def commit(
        self,
        action: str,
        details: dict,
        *,
        actor_type: str = "reviewer",
        actor_id: str = "local_reviewer",
    ) -> LedgerEvent:
        event = self.audit(
            action,
            details,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        self.refresh()
        self.persist()
        return event

    def decide(
        self,
        *,
        target_type: str,
        target_id: str,
        decision: str,
        explanation: str = "",
        reviewer: str = "local_reviewer",
    ) -> ReviewDecision:
        event = self.audit(
            "review_decision_recorded",
            {
                "target_type": target_type,
                "target_id": target_id,
                "decision": decision,
                "explanation": explanation,
            },
            actor_type="reviewer",
            actor_id=reviewer,
        )
        record = ReviewDecision(
            id=_id("decision", event.event_id),
            target_type=target_type,
            target_id=target_id,
            decision=decision,
            explanation=explanation,
            reviewer=reviewer,
            source_event_id=event.event_id,
        )
        self.proof.review_decisions.append(record)
        return record

    def revise_requirements(
        self,
        requirements: list[Requirement],
        revisions: list[RequirementRevision],
        *,
        action: str,
        details: dict,
    ) -> ChangeProof:
        decision_event = self.commit(action, details)
        for revision in revisions:
            if revision.source_event_id is None:
                revision.source_event_id = decision_event.event_id
        previous = self.proof
        active = [item for item in requirements if not item.deleted]
        fresh = build_change_proof(
            self.repo,
            base_ref=previous.git_snapshot.base_ref,
            requirement_texts=[item.text for item in active],
        )
        generated_ids = [item.id for item in fresh.requirements]
        id_map = {
            generated_id: item.id
            for generated_id, item in zip(generated_ids, active)
        }
        for mapping in fresh.mappings:
            mapping.from_id = id_map.get(mapping.from_id, mapping.from_id)
            mapping.id = _id("map", mapping.from_id, mapping.to_id)
        active_ids = {item.id for item in active}
        fresh.mappings = [
            item for item in fresh.mappings if item.from_id in active_ids
        ]
        for risk in fresh.risks:
            risk.related_ids = [
                id_map.get(item, item) for item in risk.related_ids
            ]
        valid_risk_targets = active_ids | {
            item.id for item in fresh.patch_hunks
        } | {item.id for item in fresh.file_changes} | {
            item.id for item in fresh.claims
        }
        fresh.risks = [
            item
            for item in fresh.risks
            if not (
                item.code == "verification_not_run"
                and item.related_ids
                and item.related_ids[0] not in valid_risk_targets
            )
        ]
        fresh.requirements = [
            item.model_copy(deep=True) for item in requirements
        ]
        fresh.requirement_revisions = [
            item.model_copy(deep=True) for item in revisions
        ]
        fresh.requirement_contract_hash = requirement_contract_hash(
            fresh.requirements
        )
        fresh.requirement_contract_revision = (
            previous.requirement_contract_revision + 1
        )
        fresh.review_family_id = previous.review_family_id
        fresh.review_series_id = _id(
            "series",
            str(self.repo),
            fresh.git_snapshot.base_ref,
            fresh.git_snapshot.base_sha,
            fresh.requirement_contract_hash,
        )
        fresh.change_id = _id(
            "change",
            fresh.git_snapshot.patch_fingerprint,
            fresh.requirement_contract_hash,
            fresh.schema_version,
        )
        fresh.revision_id = _id(
            "review_revision",
            fresh.git_snapshot.git_snapshot_id,
            fresh.requirement_contract_hash,
            str(fresh.requirement_contract_revision),
        )
        fresh.parent_change_id = previous.change_id
        series_revisions = load_review_revisions(
            fresh.review_series_id, self.storage_root
        )
        fresh.revision_number = (
            max(
                (item.revision_number for item in series_revisions),
                default=0,
            )
            + 1
        )
        fresh.title = active[0].text if active else "No active requirements"
        fresh.audit_events = [
            item.model_copy(deep=True) for item in previous.audit_events
        ]
        fresh.last_event_hash = previous.last_event_hash
        fresh.ledger_integrity = previous.ledger_integrity
        fresh.capture_events = [
            item.model_copy(deep=True)
            for item in previous.capture_events
        ]
        fresh.decision_records = [
            item.model_copy(deep=True) for item in previous.decision_records
        ]
        compile_decision_events(
            fresh,
            fresh.capture_events,
            repo=self.repo,
        )
        fresh.storage_migrations = list(previous.storage_migrations)
        self.proof = fresh
        self.audit_sequence = len(fresh.audit_events)
        self.storage_path = review_path(fresh.change_id, self.storage_root)
        self.commit(
            "requirement_contract_revision_created",
            {
                "change_id": fresh.change_id,
                "parent_change_id": fresh.parent_change_id,
                "requirement_contract_hash": fresh.requirement_contract_hash,
                "requirement_contract_revision": (
                    fresh.requirement_contract_revision
                ),
            },
            actor_type="system",
            actor_id="requirement_revision_manager",
        )
        register_review_revision(fresh, self.storage_root)
        return fresh

    @property
    def stale(self) -> bool:
        return _workspace_tree(self.repo) != self.proof.git_snapshot.workspace_tree_sha

    def refresh(self) -> None:
        ledger_valid, last_hash = verify_ledger(self.proof.audit_events)
        capture_valid, _ = verify_capture_chain(self.proof.capture_events)
        capture_binding_records = [
            (
                item.payload.get("capture_event_id"),
                item.payload.get("event_hash"),
            )
            for item in self.proof.audit_events
            if item.event_type == "capture_event_ingested"
        ]
        capture_bindings = set(capture_binding_records)
        capture_pairs = {
            (item.event_id, item.event_hash)
            for item in self.proof.capture_events
        }
        binding_valid = (
            capture_pairs == capture_bindings
            and len(capture_binding_records) == len(capture_pairs)
        )
        self.proof.ledger_integrity = (
            ledger_valid
            and capture_valid
            and binding_valid
        )
        if ledger_valid:
            self.proof.last_event_hash = last_hash
        latest_decisions = {
            (item.target_type, item.target_id): item
            for item in self.proof.review_decisions
        }
        for claim in self.proof.claims:
            decision = latest_decisions.get(("claim", claim.id))
            claim.status = (
                "proposed"
                if decision is None or decision.decision == "revoked"
                else decision.decision
            )
        for mapping in self.proof.mappings:
            decision = latest_decisions.get(("mapping", mapping.id))
            if decision is not None:
                mapping.confirmed = decision.decision == "confirmed"
        for assertion in self.proof.coverage_assertions:
            decision = latest_decisions.get(
                ("coverage_assertion", assertion.id)
            )
            assertion.status = (
                "proposed"
                if decision is None or decision.decision == "revoked"
                else decision.decision
            )
        sync_hunk_requirement_ids(self.proof)
        self.current_stale = refresh_evidence_stale(self.repo, self.proof)
        current_tree = _workspace_tree(self.repo)
        for verification in self.proof.verifications:
            verification.effective_status = (
                verification.result_status
                if (
                    verification.content_tree_hash
                    or verification.workspace_tree_sha
                ) == current_tree
                else "stale"
            )

        requirements = {
            item.id for item in self.proof.requirements if not item.deleted
        }
        hunks = {item.id for item in self.proof.patch_hunks}
        files = {item.id for item in self.proof.file_changes}
        hunk_parent = {
            item.id: item.file_change_id for item in self.proof.patch_hunks
        }
        children: dict[str, set[str]] = {item: set() for item in files}
        for hunk_id, file_id in hunk_parent.items():
            children.setdefault(file_id, set()).add(hunk_id)
        confirmed_hunk_mappings = [
            item
            for item in self.proof.mappings
            if item.relation == "implemented_by"
            and item.confirmed
        ]
        confirmed_file_mappings = [
            item
            for item in self.proof.mappings
            if item.relation == "implemented_by_file"
            and item.confirmed
        ]
        mapped_requirements = {
            item.from_id
            for item in confirmed_hunk_mappings
            if item.from_id in requirements
        }
        mapped_requirements.update(
            item.from_id
            for item in confirmed_file_mappings
            if item.from_id in requirements
            and not children.get(item.to_id)
        )
        directly_mapped_hunks = {
            item.to_id
            for item in confirmed_hunk_mappings
            if item.from_id in requirements and item.to_id in hunks
        }
        directly_mapped_files = {
            item.to_id
            for item in confirmed_file_mappings
            if item.from_id in requirements and item.to_id in files
        }
        mapped_hunks = {
            hunk_id
            for hunk_id in hunks
            if hunk_id in directly_mapped_hunks
        }
        mapped_files = {
            file_id
            for file_id in files
            if (
                not children.get(file_id)
                and file_id in directly_mapped_files
            )
            or (
                children.get(file_id)
                and children[file_id] <= directly_mapped_hunks
            )
        }
        valid_verifications = [
            item
            for item in self.proof.verifications
            if item.effective_status == "passed"
        ]
        valid_verification_ids = {item.id for item in valid_verifications}
        valid_assertions = [
            item
            for item in self.proof.coverage_assertions
            if item.status == "confirmed"
            and item.verification_id in valid_verification_ids
        ]
        rejected_assertions = [
            item
            for item in self.proof.coverage_assertions
            if item.status == "rejected"
        ]
        verified_requirements = {
            item.target_id
            for item in valid_assertions
            if item.target_type == "requirement"
            and item.target_id in requirements
        }
        verified_hunks = {
            item.target_id
            for item in valid_assertions
            if item.target_type == "hunk" and item.target_id in hunks
        }
        directly_verified_files = {
            item.target_id
            for item in valid_assertions
            if item.target_type == "file_change"
            and item.target_id in files
        }
        effective_verified_hunks = verified_hunks
        verified_files = {
            file_id
            for file_id in files
            if (
                not children.get(file_id)
                and file_id in directly_verified_files
            )
            or (
                children.get(file_id)
                and children[file_id] <= effective_verified_hunks
            )
        }
        rejected_uncovered_assertions = [
            item
            for item in rejected_assertions
            if (
                item.target_type == "requirement"
                and item.target_id not in verified_requirements
            )
            or (
                item.target_type == "hunk"
                and item.target_id not in effective_verified_hunks
            )
            or (
                item.target_type == "file_change"
                and item.target_id not in verified_files
            )
        ]
        claims = {item.id: item for item in self.proof.claims}
        evidence = {item.id: item for item in self.proof.evidence}
        hunk_claims = [
            claims[claim_id]
            for hunk in self.proof.patch_hunks
            for claim_id in hunk.claim_ids
            if claim_id in claims
        ]
        rejected = [item for item in hunk_claims if item.status == "rejected"]
        proposed_claims = [
            item for item in hunk_claims if item.status == "proposed"
        ]
        claims_with_current_evidence = [
            item
            for item in hunk_claims
            if item.evidence_ids
            and any(
                evidence[evidence_id].content_sha256
                and not evidence[evidence_id].stale
                for evidence_id in item.evidence_ids
                if evidence_id in evidence
            )
        ]
        directly_reviewed_files = {
            item.id for item in self.proof.file_changes if item.reviewed
        }
        reviewed_hunks = {
            item.id
            for item in self.proof.patch_hunks
            if item.reviewed
        }
        reviewed_files = {
            item.id
            for item in self.proof.file_changes
            if (not children.get(item.id) and item.reviewed)
            or (
                children.get(item.id)
                and children[item.id]
                <= {hunk.id for hunk in self.proof.patch_hunks if hunk.reviewed}
            )
        }

        requirement_mapping_coverage = len(
            mapped_requirements & requirements
        ) / max(len(requirements), 1)
        hunk_mapping_coverage = len(mapped_hunks) / max(len(hunks), 1)
        file_mapping_coverage = len(mapped_files) / max(len(files), 1)
        claim_evidence_coverage = len(
            {item.id for item in claims_with_current_evidence}
        ) / max(len({item.id for item in hunk_claims}), 1)
        verification_coverage = (
            len(verified_requirements & requirements)
            + len(effective_verified_hunks & hunks)
            + len(verified_files & files)
        ) / max(len(requirements) + len(hunks) + len(files), 1)
        self.proof.gate.coverage.requirement_hunk = requirement_mapping_coverage
        self.proof.gate.coverage.hunk = hunk_mapping_coverage
        self.proof.gate.coverage.file_change = file_mapping_coverage
        self.proof.gate.coverage.claim_evidence = claim_evidence_coverage
        self.proof.gate.coverage.verification = verification_coverage

        reasons: list[str] = []
        if not self.proof.ledger_integrity:
            reasons.append("ledger_integrity_failed")
        if not capture_valid:
            reasons.append("capture_integrity_failed")
        elif not binding_valid:
            reasons.append("ledger_capture_hash_binding_failed")
        if not self.proof.file_changes:
            reasons.append("no_changes")
        if self.current_stale:
            reasons.append("change_proof_stale")
        if mapped_requirements != requirements:
            reasons.append("requirements_without_confirmed_change_mapping")
        if hunks and mapped_hunks != hunks:
            reasons.append("patch_hunks_without_confirmed_requirement_mapping")
        if mapped_files != files:
            reasons.append("file_changes_without_confirmed_requirement_mapping")
        if reviewed_hunks != hunks:
            reasons.append("unreviewed_patch_hunks")
        if reviewed_files != files:
            reasons.append("unreviewed_file_changes")
        if verified_requirements != requirements:
            reasons.append("requirements_without_passing_verification")
        if effective_verified_hunks != hunks:
            reasons.append("patch_hunks_without_passing_verification")
        if verified_files != files:
            reasons.append("file_changes_without_passing_verification")
        if rejected:
            reasons.append("rejected_claim_for_patch")
        if proposed_claims:
            reasons.append("unconfirmed_claim_for_patch")
        if rejected_uncovered_assertions:
            reasons.append("rejected_coverage_assertion")
        if len({item.id for item in claims_with_current_evidence}) != len(
            {item.id for item in hunk_claims}
        ):
            reasons.append("claims_without_current_evidence")
        relevant_evidence_ids = {
            evidence_id
            for claim in hunk_claims
            for evidence_id in claim.evidence_ids
        }
        if any(
            item.stale and item.id in relevant_evidence_ids
            for item in self.proof.evidence
        ):
            reasons.append("stale_evidence")
        if any(
            item.severity == "high" and item.status == "open"
            for item in self.proof.risks
        ):
            reasons.append("unhandled_high_risk")

        for risk in self.proof.risks:
            if risk.code != "verification_not_run" or risk.status == "accepted":
                continue
            target = risk.related_ids[0] if risk.related_ids else ""
            covered = (
                target in verified_requirements if target in requirements
                else target in effective_verified_hunks
                if target in hunks
                else target in verified_files
            )
            risk.status = "resolved" if covered else "open"

        high = bool(self.proof.file_changes) and not reasons
        self.proof.gate.reasons = reasons
        self.proof.gate.status = (
            "passed"
            if high
            else "blocked"
            if "no_changes" in reasons or "unhandled_high_risk" in reasons
            else "warning"
        )
        if high:
            self.proof.assurance.level = "high"
            self.proof.assurance.reasons = [
                "Git Snapshot 当前有效。",
                "全部 Requirement-Change 映射已由 Reviewer 确认。",
                "全部 Hunk/FileChange 已审核且有有效的通过验证。",
                "关键 Claim 有当前 Evidence，且无拒绝 Claim 或未处理 High Risk。",
            ]
        elif (
            reviewed_hunks
            or reviewed_files
            or confirmed_hunk_mappings
            or confirmed_file_mappings
            or verified_requirements
            or effective_verified_hunks
            or verified_files
        ):
            self.proof.assurance.level = "medium"
            self.proof.assurance.reasons = reasons
        elif self.proof.file_changes:
            self.proof.assurance.level = "low"
            self.proof.assurance.reasons = reasons
        else:
            self.proof.assurance.level = "unrated"
            self.proof.assurance.reasons = reasons


class ProgressiveReviewState:
    """Thread-safe placeholder while the review proof is built."""

    def __init__(self, repo: Path, *, batch_size: int = 8) -> None:
        self.repo = repo.resolve()
        self._state: ReviewState | None = None
        self._lock = threading.Lock()
        self._phase = "workspace"
        self._message = "正在读取工作区状态…"
        self._total = 0
        self._loaded = 0
        self._files: list[dict[str, object]] = []
        self._file_ids: set[str] = set()
        self._published = 0
        self._batch_size = max(1, batch_size)
        self._error = ""

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._state is not None

    @property
    def proof(self) -> ChangeProof:
        with self._lock:
            state = self._state
        if state is None:
            raise RuntimeError("review_state_not_ready")
        return state.proof

    def __getattr__(self, name: str):
        with self._lock:
            state = self._state
        if state is None:
            raise RuntimeError("review_state_not_ready")
        return getattr(state, name)

    def update(self, payload: dict[str, object]) -> None:
        with self._lock:
            self._phase = str(payload.get("phase") or self._phase)
            self._message = str(payload.get("message") or self._message)
            self._total = int(payload.get("total") or self._total)
            self._loaded = int(payload.get("loaded") or self._loaded)
            file_payload = payload.get("file")
            if isinstance(file_payload, dict):
                file_id = str(file_payload.get("id") or "")
                if file_id and file_id not in self._file_ids:
                    self._file_ids.add(file_id)
                    self._files.append(dict(file_payload))

    def complete(self, state: ReviewState) -> None:
        with self._lock:
            for item in state.proof.file_changes:
                if item.id in self._file_ids:
                    continue
                self._file_ids.add(item.id)
                self._files.append(
                    {
                        "id": item.id,
                        "path": item.path,
                        "old_path": item.old_path,
                        "change_type": item.change_type,
                        "summary": item.summary,
                    }
                )
            self._state = state
            self._phase = "ready"
            self._message = "代码变更已载入。"
            self._total = len(self._files)
            self._loaded = len(self._files)

    def fail(self, error: Exception) -> None:
        with self._lock:
            self._phase = "failed"
            self._message = "无法分析当前代码变更。"
            self._error = str(error)

    def loading_snapshot(self) -> dict[str, object]:
        with self._lock:
            if self._published < len(self._files):
                self._published = min(
                    len(self._files),
                    self._published + self._batch_size,
                )
            presented = self._files[: self._published]
            ready = (
                self._state is not None
                and self._published >= len(self._files)
            )
            presenting = self._state is not None and not ready
            return {
                "status": (
                    "failed"
                    if self._phase == "failed"
                    else "ready"
                    if ready
                    else "loading"
                ),
                "phase": "presenting" if presenting else self._phase,
                "message": (
                    "正在载入文件列表…"
                    if presenting
                    else self._message
                ),
                "total": self._total,
                "loaded": min(self._published, self._loaded),
                "files": [dict(item) for item in presented],
                "error": self._error,
            }


def _inherit_review_revision(
    previous: ChangeProof, fresh: ChangeProof
) -> None:
    """Carry review facts forward only where deterministic target IDs survive."""
    requirement_ids = {item.id for item in fresh.requirements}
    hunk_ids = {item.id for item in fresh.patch_hunks}
    file_ids = {item.id for item in fresh.file_changes}
    current_evidence_ids = {item.id for item in fresh.evidence}
    for evidence in previous.evidence:
        if evidence.id not in current_evidence_ids:
            historical = evidence.model_copy(deep=True)
            historical.stale = True
            fresh.evidence.append(historical)
    current_claim_ids = {item.id for item in fresh.claims}
    fresh.claims.extend(
        item.model_copy(deep=True)
        for item in previous.claims
        if item.id not in current_claim_ids
    )
    claim_ids = {item.id for item in fresh.claims}
    valid_mapping_targets = hunk_ids | file_ids
    prior_mappings = [
        item.model_copy(deep=True)
        for item in previous.mappings
        if item.from_id in requirement_ids and item.to_id in valid_mapping_targets
    ]
    prior_mapping_ids = {item.id for item in prior_mappings}
    fresh.mappings = prior_mappings + [
        item for item in fresh.mappings if item.id not in prior_mapping_ids
    ]
    old_hunks = {item.id: item for item in previous.patch_hunks}
    for item in fresh.patch_hunks:
        if item.id in old_hunks:
            item.reviewed = old_hunks[item.id].reviewed
    old_files = {item.id: item for item in previous.file_changes}
    for item in fresh.file_changes:
        if item.id in old_files:
            item.reviewed = old_files[item.id].reviewed

    fresh.verifications = [
        item.model_copy(deep=True) for item in previous.verifications
    ]
    surviving_targets = requirement_ids | hunk_ids | file_ids
    previous_mapping_targets: dict[str, set[str]] = {}
    for mapping in previous.mappings:
        previous_mapping_targets.setdefault(mapping.from_id, set()).add(
            mapping.to_id
        )
    for verification in fresh.verifications:
        explicit = set(verification.hunk_ids) | set(
            verification.file_change_ids
        )
        requirement_targets = set().union(
            *(
                previous_mapping_targets.get(item, set())
                for item in verification.requirement_ids
            ),
            set(),
        )
        targets = explicit | requirement_targets
        if targets and targets <= surviving_targets:
            verification.workspace_tree_sha = fresh.git_snapshot.content_tree_hash
            verification.content_tree_hash = fresh.git_snapshot.content_tree_hash
            verification.patch_fingerprint = fresh.git_snapshot.patch_fingerprint
    verification_ids = {item.id for item in fresh.verifications}
    fresh.coverage_assertions = [
        item.model_copy(deep=True)
        for item in previous.coverage_assertions
        if item.verification_id in verification_ids
        and item.target_id in surviving_targets
    ]
    assertion_ids = {item.id for item in fresh.coverage_assertions}
    valid_decision_targets = (
        claim_ids
        | prior_mapping_ids
        | assertion_ids
        | hunk_ids
        | file_ids
        | {item.id for item in fresh.risks}
    )
    fresh.review_decisions = [
        item.model_copy(deep=True)
        for item in previous.review_decisions
        if item.target_id in valid_decision_targets
    ]
    old_risks = {item.id: item for item in previous.risks}
    for risk in fresh.risks:
        if risk.id in old_risks:
            risk.status = old_risks[risk.id].status
    fresh.requirement_revisions = [
        item.model_copy(deep=True) for item in previous.requirement_revisions
    ]
    fresh.capture_events = [
        item.model_copy(deep=True)
        for item in previous.capture_events
    ]
    fresh.decision_records = [
        item.model_copy(deep=True) for item in previous.decision_records
    ]
    fresh.publications = [
        item.model_copy(deep=True) for item in previous.publications
    ]
    fresh.ci_verifications = [
        item.model_copy(deep=True) for item in previous.ci_verifications
    ]
    fresh.ci_attestations = [
        item.model_copy(deep=True) for item in previous.ci_attestations
    ]
    fresh.audit_events = [
        item.model_copy(deep=True) for item in previous.audit_events
    ]
    fresh.last_event_hash = previous.last_event_hash
    fresh.ledger_integrity = previous.ledger_integrity
    fresh.git_snapshot_revisions = [
        item.model_copy(deep=True)
        for item in previous.git_snapshot_revisions
    ] + [
        item.model_copy(update={"transition": "content_change"})
        for item in fresh.git_snapshot_revisions
    ]


def load_or_create_review_state(
    repo: Path,
    *,
    base_ref: str | None = None,
    requirement_texts: list[str] | None = None,
    storage_root: Path | None = None,
    runner: VerificationRunner | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> ReviewState:
    fresh = build_change_proof(
        repo,
        base_ref=base_ref,
        requirement_texts=requirement_texts,
        progress_callback=progress_callback,
    )
    if progress_callback is not None:
        progress_callback(
            {
                "phase": "restoring",
                "message": "正在恢复已有评审记录…",
                "total": len(fresh.file_changes),
                "loaded": len(fresh.file_changes),
            }
        )
    path = review_path(fresh.change_id, storage_root)
    restored = load_review(path, fresh)
    if restored is None:
        revisions = load_review_revisions(
            fresh.review_series_id, storage_root
        )
        if revisions:
            parent = max(revisions, key=lambda item: item.revision_number)
            fresh.parent_change_id = parent.change_id
            fresh.revision_number = parent.revision_number + 1
            previous = load_review_revision(
                review_path(parent.change_id, storage_root)
            )
            _inherit_review_revision(previous, fresh)
        restored = fresh
    state = ReviewState(
        repo=repo,
        proof=restored,
        runner=runner or SandboxRunner(),
        storage_path=path,
        storage_root=storage_root,
    )
    if restored is not fresh and (
        restored.git_snapshot.git_snapshot_id
        != fresh.git_snapshot.git_snapshot_id
    ):
        previous_snapshot = restored.git_snapshot
        previous_review_revision = restored.revision_id
        restored.git_snapshot = fresh.git_snapshot
        restored.revision_id = fresh.revision_id
        restored.repository.branch = fresh.repository.branch
        existing_revisions = load_review_revisions(
            restored.review_series_id, storage_root
        )
        restored.revision_number = (
            max(
                (item.revision_number for item in existing_revisions),
                default=restored.revision_number,
            )
            + 1
        )
        same_content = (
            previous_snapshot.patch_fingerprint
            == fresh.git_snapshot.patch_fingerprint
            and previous_snapshot.content_tree_hash
            == fresh.git_snapshot.content_tree_hash
        )
        transition = (
            "commit_transition"
            if same_content
            and previous_snapshot.head_sha != fresh.git_snapshot.head_sha
            else "git_state_transition"
            if same_content
            else "content_change"
        )
        restored.git_snapshot_revisions.append(
            GitSnapshotRevision(
                revision_id=fresh.git_snapshot.revision_id,
                git_snapshot_id=fresh.git_snapshot.git_snapshot_id,
                content_tree_hash=fresh.git_snapshot.content_tree_hash,
                patch_fingerprint=fresh.git_snapshot.patch_fingerprint,
                head_sha=fresh.git_snapshot.head_sha,
                branch=fresh.repository.branch,
                dirty=fresh.git_snapshot.is_dirty,
                transition=transition,
            )
        )
        state.commit(
            transition,
            {
                "previous_head_sha": previous_snapshot.head_sha,
                "head_sha": fresh.git_snapshot.head_sha,
                "patch_fingerprint": fresh.git_snapshot.patch_fingerprint,
                "content_tree_hash": fresh.git_snapshot.content_tree_hash,
                "git_snapshot_id": fresh.git_snapshot.git_snapshot_id,
                "previous_revision_id": previous_review_revision,
                "revision_id": fresh.revision_id,
            },
            actor_type="system",
            actor_id="git_snapshot_manager",
        )
        register_review_revision(restored, storage_root)
    try:
        captured = CaptureStore(capture_path(repo)).load()
    except CaptureStorageError as exc:
        raise ReviewStorageError(str(exc)) from exc
    known_capture_ids = {item.event_id for item in state.proof.capture_events}
    imported = [
        item for item in captured if item.event_id not in known_capture_ids
    ]
    for capture in imported:
        state.proof.capture_events.append(capture)
        state.audit(
            "capture_event_ingested",
            {
                "capture_event_id": capture.event_id,
                "payload_hash": capture.payload_hash,
                "event_hash": capture.event_hash,
                "session_id": capture.session_id,
            },
            actor_type="adapter",
            actor_id=capture.host,
        )
    compile_decision_events(
        state.proof,
        captured,
        repo=repo,
    )
    if restored is fresh:
        requirements_by_id = {
            item.id: item for item in fresh.requirements
        }
        for revision in fresh.requirement_revisions:
            requirement = requirements_by_id[revision.requirement_id]
            event = state.audit(
                "requirement_revision_captured",
                {
                    "requirement_id": requirement.id,
                    "revision": revision.revision,
                    "category": requirement.category,
                },
                actor_type=(
                    "reviewer"
                    if requirement.provenance.kind == "captured"
                    else "system"
                ),
                actor_id=requirement.provenance.source,
            )
            revision.source_event_id = event.event_id
        for claim in fresh.claims:
            if claim.provenance.kind == "inferred":
                state.audit(
                    "claim_inferred",
                    {
                        "claim_id": claim.id,
                        "confidence": claim.provenance.confidence,
                    },
                    actor_type="system",
                    actor_id=claim.provenance.source,
                )
        state.commit(
            "change_proof_revision_created",
            {
                "change_id": fresh.change_id,
                "review_series_id": fresh.review_series_id,
                "parent_change_id": fresh.parent_change_id,
                "revision_number": fresh.revision_number,
            },
            actor_type="system",
            actor_id="review_revision_manager",
        )
        register_review_revision(fresh, storage_root)
    elif imported:
        state.refresh()
        state.persist()
    return state


def proof_markdown(proof: ChangeProof, *, stale: bool = False) -> str:
    verification_lines = [
        f"- `{item.command}` — result **{item.result_status}**, "
        f"effective **{item.effective_status}**"
        f" (requirements: {len(item.requirement_ids)}, "
        f"hunks: {len(item.hunk_ids)}, files: {len(item.file_change_ids)}, "
        f"prepare_dependencies: {item.prepare_dependencies}, "
        f"argv: {item.command_argv}, environment: {item.environment})"
        for item in proof.verifications
    ] or ["- 未运行验证"]
    assertion_lines = [
        f"- `{item.verification_id}` → `{item.target_type}:{item.target_id}` "
        f"— **{item.status}** ({item.provenance.kind})"
        for item in proof.coverage_assertions
    ] or ["- 尚无 Coverage Assertion"]
    decision_lines = [
        f"- `{item.target_type}:{item.target_id}` — **{item.decision}** "
        f"by `{item.reviewer}` (event `{item.source_event_id}`)"
        for item in proof.review_decisions
    ] or ["- 尚无 Reviewer Decision"]
    risk_lines = [
        f"- **{item.severity.upper()}** {item.statement}（{item.status}）"
        for item in proof.risks
    ] or ["- 未记录残余风险"]
    mapping_lines = [
        f"- `{item.from_id}` → `{item.to_id}`"
        f"（{'confirmed' if item.confirmed else 'unconfirmed'}, "
        f"{item.provenance.kind}, confidence={item.provenance.confidence:.2f}）"
        for item in proof.mappings
    ] or ["- 尚无 Requirement-Hunk 映射"]
    return "\n".join(
        [
            "<!-- codecairn:change-proof -->",
            f"## {proof.title}",
            "",
            f"**Assurance:** {proof.assurance.level.upper()}"
            + (" · STALE" if stale else ""),
            f"**Revision:** {proof.revision_number} · "
            f"Requirement Contract r{proof.requirement_contract_revision}",
            f"**Patch fingerprint:** `{proof.git_snapshot.patch_fingerprint}`",
            f"**Git snapshot:** `{proof.git_snapshot.git_snapshot_id}` · "
            f"Review revision `{proof.revision_id}`",
            "",
            "### 未满足条件",
            *[f"- `{reason}`" for reason in proof.gate.reasons],
            "",
            "### 目标",
            *[f"- {item.text}" for item in proof.requirements],
            "",
            "### 文件变更",
            *[
                f"- **{item.change_type}** "
                f"`{item.old_path + ' → ' if item.old_path else ''}{item.path}`"
                f"（reviewed={item.reviewed}, "
                f"requirements={len(item.requirement_ids)}）"
                for item in proof.file_changes
            ],
            "",
            "### Patch Hunks",
            *[
                f"- `{item.path} {item.header}` — {item.summary} "
                f"（reviewed={item.reviewed}）"
                for item in proof.patch_hunks
            ],
            "",
            "### Requirement-Hunk 映射",
            *mapping_lines,
            "",
            "### 修改逻辑",
            *[
                f"- {item.statement}（{item.status}, {item.provenance.kind}）"
                for item in proof.claims
            ],
            "",
            "### Evidence",
            *[
                f"- `{item.path}:{item.line or ''}` — "
                f"sha256 `{item.content_sha256 or 'unavailable'}` "
                f"（stale={item.stale}, {item.provenance.kind}/"
                f"{item.provenance.source}）"
                for item in proof.evidence
            ],
            "",
            "### 验证",
            *verification_lines,
            "",
            "### 覆盖声明",
            *assertion_lines,
            "",
            "### Reviewer Decisions",
            *decision_lines,
            "",
            "### Evidence Ledger",
            f"- integrity: **{proof.ledger_integrity}**",
            f"- last_event_hash: `{proof.last_event_hash}`",
            "",
            "### CI Verification",
            *(
                [
                    f"- `{item.run_id}` — **{item.result}** "
                    f"（trusted={item.trusted}, {item.trust_reason}, "
                    f"trust_source={item.trust_source}, "
                    f"policy={item.policy_hash or '-'}, "
                    f"artifact={item.artifact_id or '-'} "
                    f"{item.artifact_digest or '-'}, "
                    f"output_sha256={item.output_hash}）"
                    for item in proof.ci_verifications
                ]
                or ["- 尚无 CI 回填"]
            ),
            "",
            "### 残余风险",
            *risk_lines,
            "",
            f"_CodeCairn Change Proof `{proof.change_id}` · "
            f"schema {proof.schema_version}_",
            "<!-- /codecairn:change-proof -->",
        ]
    )


def _allowed_origin(
    origin: str, hosts: set[str], request: Request
) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in hosts
        and (parsed.port or 80) == (request.url.port or 80)
    )


def create_review_app(
    state: ReviewState | ProgressiveReviewState,
    *,
    session_token: str | None = None,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    token = session_token or secrets.token_urlsafe(32)
    hosts = allowed_hosts or {"127.0.0.1", "localhost"}
    app = FastAPI(title="CodeCairn Local Review", docs_url=None, redoc_url=None)
    app.state.session_token = token

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        host = request.url.hostname
        if host not in hosts:
            return JSONResponse({"detail": "invalid_host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and not _allowed_origin(origin, hosts, request):
            return JSONResponse({"detail": "invalid_origin"}, status_code=403)
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("x-codecairn-token", "")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse({"detail": "invalid_session_token"}, status_code=401)
            if (
                isinstance(state, ProgressiveReviewState)
                and not state.ready
                and request.url.path != "/api/loading"
            ):
                return JSONResponse(
                    state.loading_snapshot(),
                    status_code=425,
                )
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; style-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/assets/review.js")
    def review_javascript() -> Response:
        return Response(REVIEW_JS, media_type="application/javascript")

    @app.get("/assets/review.css")
    def review_stylesheet() -> Response:
        return Response(REVIEW_CSS, media_type="text/css")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        supplied = (
            request.query_params.get("token", "")
            or request.cookies.get("codecairn_session", "")
        )
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="invalid_session_token")
        response = HTMLResponse(
            REVIEW_HTML.replace(
                "__CODECAIRN_TOKEN__", html.escape(token, quote=True)
            )
        )
        response.set_cookie(
            "codecairn_session",
            token,
            httponly=True,
            samesite="strict",
            max_age=8 * 60 * 60,
        )
        return response

    @app.get("/api/loading")
    def get_loading_state() -> dict:
        if isinstance(state, ProgressiveReviewState):
            return state.loading_snapshot()
        return {
            "status": "ready",
            "phase": "ready",
            "message": "代码变更已载入。",
            "total": len(state.proof.file_changes),
            "loaded": len(state.proof.file_changes),
            "files": [],
            "error": "",
        }

    @app.get("/api/proof")
    def get_proof() -> dict:
        before = [
            state.current_stale,
            [(item.id, item.stale) for item in state.proof.evidence],
            [(item.id, item.status) for item in state.proof.verifications],
        ]
        state.refresh()
        after = [
            state.current_stale,
            [(item.id, item.stale) for item in state.proof.evidence],
            [(item.id, item.status) for item in state.proof.verifications],
        ]
        if before != after:
            state.commit(
                "stale_state_refreshed",
                {"before": before, "after": after},
                actor_type="system",
                actor_id="stale_detector",
            )
        payload = state.proof.model_dump(mode="json")
        payload["stale"] = state.stale
        config_preview = load_repo_config(state.repo, validate=False)
        payload["suggested_test_command"] = config_preview.test_command
        payload["suggested_install_command"] = config_preview.install_command
        return payload

    def validated_category(value: str) -> str:
        if value not in {
            "requirement",
            "acceptance_criterion",
            "constraint",
        }:
            raise HTTPException(
                status_code=400, detail="invalid_requirement_category"
            )
        return value

    @app.post("/api/requirements")
    def create_requirement(request: RequirementCreate) -> dict:
        text_value = " ".join(request.text.split())
        if not text_value:
            raise HTTPException(status_code=400, detail="empty_requirement")
        category = validated_category(request.category)
        requirements = [
            item.model_copy(deep=True) for item in state.proof.requirements
        ]
        requirement = Requirement(
            id=_id(
                "req",
                state.proof.review_family_id,
                str(len(state.proof.requirement_revisions) + 1),
                text_value,
            ),
            text=text_value,
            original_text=request.text,
            category=category,
            provenance=Provenance(
                kind="captured", source="reviewer_requirement"
            ),
        )
        requirements.append(requirement)
        revisions = [
            item.model_copy(deep=True)
            for item in state.proof.requirement_revisions
        ]
        revisions.append(
            RequirementRevision(
                requirement_id=requirement.id,
                revision=1,
                text=requirement.text,
                original_text=requirement.original_text,
                category=requirement.category,
                actor="local_reviewer",
            )
        )
        state.revise_requirements(
            requirements,
            revisions,
            action="requirement_created",
            details={"requirement_id": requirement.id},
        )
        return requirement.model_dump(mode="json")

    @app.patch("/api/requirements/{requirement_id}")
    def update_requirement(
        requirement_id: str, request: RequirementUpdate
    ) -> dict:
        requirements = [
            item.model_copy(deep=True) for item in state.proof.requirements
        ]
        requirement = next(
            (item for item in requirements if item.id == requirement_id),
            None,
        )
        if requirement is None:
            raise HTTPException(status_code=404, detail="requirement_not_found")
        text_value = " ".join(request.text.split())
        if not text_value:
            raise HTTPException(status_code=400, detail="empty_requirement")
        category = validated_category(request.category)
        if (
            text_value == requirement.text
            and category == requirement.category
        ):
            return requirement.model_dump(mode="json")
        previous_text = requirement.text
        requirement.text = text_value
        requirement.category = category
        requirement.revision += 1
        revisions = [
            item.model_copy(deep=True)
            for item in state.proof.requirement_revisions
        ]
        revisions.append(
            RequirementRevision(
                requirement_id=requirement.id,
                revision=requirement.revision,
                text=requirement.text,
                original_text=previous_text,
                category=requirement.category,
                deleted=requirement.deleted,
                actor="local_reviewer",
            )
        )
        state.revise_requirements(
            requirements,
            revisions,
            action="requirement_updated",
            details={
                "requirement_id": requirement.id,
                "revision": requirement.revision,
            },
        )
        return requirement.model_dump(mode="json")

    def set_requirement_deleted(
        requirement_id: str, deleted: bool
    ) -> dict:
        requirements = [
            item.model_copy(deep=True) for item in state.proof.requirements
        ]
        requirement = next(
            (item for item in requirements if item.id == requirement_id),
            None,
        )
        if requirement is None:
            raise HTTPException(status_code=404, detail="requirement_not_found")
        if requirement.deleted == deleted:
            return requirement.model_dump(mode="json")
        requirement.deleted = deleted
        requirement.revision += 1
        revisions = [
            item.model_copy(deep=True)
            for item in state.proof.requirement_revisions
        ]
        revisions.append(
            RequirementRevision(
                requirement_id=requirement.id,
                revision=requirement.revision,
                text=requirement.text,
                original_text=requirement.text,
                category=requirement.category,
                deleted=deleted,
                actor="local_reviewer",
            )
        )
        action = "requirement_deleted" if deleted else "requirement_restored"
        state.revise_requirements(
            requirements,
            revisions,
            action=action,
            details={
                "requirement_id": requirement.id,
                "revision": requirement.revision,
            },
        )
        return requirement.model_dump(mode="json")

    @app.delete("/api/requirements/{requirement_id}")
    def delete_requirement(requirement_id: str) -> dict:
        return set_requirement_deleted(requirement_id, True)

    @app.post("/api/requirements/{requirement_id}/restore")
    def restore_requirement(requirement_id: str) -> dict:
        return set_requirement_deleted(requirement_id, False)

    @app.get("/api/requirements/{requirement_id}/history")
    def requirement_history(requirement_id: str) -> list[dict]:
        if not any(
            item.id == requirement_id for item in state.proof.requirements
        ):
            raise HTTPException(status_code=404, detail="requirement_not_found")
        return [
            item.model_dump(mode="json")
            for item in state.proof.requirement_revisions
            if item.requirement_id == requirement_id
        ]

    @app.patch("/api/hunks/{hunk_id}/mapping")
    def update_mapping(hunk_id: str, request: MappingUpdate) -> dict:
        hunk = next(
            (item for item in state.proof.patch_hunks if item.id == hunk_id),
            None,
        )
        if hunk is None:
            raise HTTPException(status_code=404, detail="hunk_not_found")
        known = {
            item.id for item in state.proof.requirements if not item.deleted
        }
        requested = set(request.requirement_ids)
        if not requested <= known:
            raise HTTPException(status_code=400, detail="unknown_requirement")
        existing = {
            item.from_id: item
            for item in state.proof.mappings
            if item.relation == "implemented_by" and item.to_id == hunk_id
        }
        for requirement_id, mapping in existing.items():
            if requirement_id not in requested:
                state.decide(
                    target_type="mapping",
                    target_id=mapping.id,
                    decision="revoked",
                    explanation="Reviewer 取消 Requirement-Hunk 映射。",
                )
        for requirement_id in request.requirement_ids:
            mapping = existing.get(requirement_id)
            if mapping is None:
                state.proof.mappings.append(
                    Mapping(
                        id=_id("map", requirement_id, hunk_id),
                        from_id=requirement_id,
                        to_id=hunk_id,
                        relation="implemented_by",
                        explanation="Reviewer 在本地 Review Workspace 中确认映射。",
                        confirmed=False,
                        provenance=Provenance(
                            kind="captured",
                            source="reviewer_mapping",
                            confidence=1.0,
                        ),
                    )
                )
                mapping = state.proof.mappings[-1]
            else:
                mapping.explanation = "Reviewer 在本地 Review Workspace 中确认映射。"
            state.decide(
                target_type="mapping",
                target_id=mapping.id,
                decision="confirmed",
                explanation="Reviewer 确认 Requirement-Hunk 映射。",
            )
        state.commit(
            "requirement_hunk_mapping_updated",
            {"hunk_id": hunk_id, "requirement_ids": request.requirement_ids},
        )
        return {
            "hunk": hunk.model_dump(mode="json"),
            "mappings": [
                item.model_dump(mode="json")
                for item in state.proof.mappings
                if item.to_id == hunk_id and item.relation == "implemented_by"
            ],
        }

    @app.patch("/api/hunks/{hunk_id}/reviewed")
    def update_reviewed(hunk_id: str, request: ReviewUpdate) -> dict:
        hunk = next(
            (item for item in state.proof.patch_hunks if item.id == hunk_id),
            None,
        )
        if hunk is None:
            raise HTTPException(status_code=404, detail="hunk_not_found")
        hunk.reviewed = request.reviewed
        state.commit(
            "hunk_review_status_updated",
            {"hunk_id": hunk_id, "reviewed": request.reviewed},
        )
        return hunk.model_dump(mode="json")

    @app.patch("/api/files/{file_change_id}/mapping")
    def update_file_mapping(
        file_change_id: str, request: MappingUpdate
    ) -> dict:
        file_change = next(
            (
                item
                for item in state.proof.file_changes
                if item.id == file_change_id
            ),
            None,
        )
        if file_change is None:
            raise HTTPException(status_code=404, detail="file_change_not_found")
        known = {
            item.id for item in state.proof.requirements if not item.deleted
        }
        requested = set(request.requirement_ids)
        if not requested <= known:
            raise HTTPException(status_code=400, detail="unknown_requirement")
        existing = {
            item.from_id: item
            for item in state.proof.mappings
            if item.relation == "implemented_by_file"
            and item.to_id == file_change_id
        }
        for requirement_id, mapping in existing.items():
            if requirement_id not in requested:
                state.decide(
                    target_type="mapping",
                    target_id=mapping.id,
                    decision="revoked",
                    explanation="Reviewer 取消 Requirement-FileChange 映射。",
                )
        for requirement_id in request.requirement_ids:
            mapping = existing.get(requirement_id)
            if mapping is None:
                state.proof.mappings.append(
                    Mapping(
                        id=_id("map_file", requirement_id, file_change_id),
                        from_id=requirement_id,
                        to_id=file_change_id,
                        relation="implemented_by_file",
                        explanation="Reviewer 确认 Requirement 与 FileChange 的关系。",
                        confirmed=False,
                        provenance=Provenance(
                            kind="captured",
                            source="reviewer_mapping",
                            confidence=1.0,
                        ),
                    )
                )
                mapping = state.proof.mappings[-1]
            else:
                mapping.explanation = (
                    "Reviewer 确认 Requirement 与 FileChange 的关系。"
                )
            state.decide(
                target_type="mapping",
                target_id=mapping.id,
                decision="confirmed",
                explanation="Reviewer 确认 Requirement-FileChange 映射。",
            )
        state.commit(
            "requirement_file_mapping_updated",
            {
                "file_change_id": file_change_id,
                "requirement_ids": request.requirement_ids,
            },
        )
        return {
            "file_change": file_change.model_dump(mode="json"),
            "mappings": [
                item.model_dump(mode="json")
                for item in state.proof.mappings
                if item.to_id == file_change_id
                and item.relation == "implemented_by_file"
            ],
        }

    @app.patch("/api/files/{file_change_id}/reviewed")
    def update_file_reviewed(
        file_change_id: str, request: ReviewUpdate
    ) -> dict:
        file_change = next(
            (
                item
                for item in state.proof.file_changes
                if item.id == file_change_id
            ),
            None,
        )
        if file_change is None:
            raise HTTPException(status_code=404, detail="file_change_not_found")
        file_change.reviewed = request.reviewed
        state.commit(
            "file_change_review_status_updated",
            {
                "file_change_id": file_change_id,
                "reviewed": request.reviewed,
            },
        )
        return file_change.model_dump(mode="json")

    @app.patch("/api/claims/{claim_id}")
    def update_claim(claim_id: str, request: ClaimUpdate) -> dict:
        if request.status not in {"proposed", "confirmed", "rejected"}:
            raise HTTPException(status_code=400, detail="invalid_claim_status")
        claim = next(
            (item for item in state.proof.claims if item.id == claim_id), None
        )
        if claim is None:
            raise HTTPException(status_code=404, detail="claim_not_found")
        state.decide(
            target_type="claim",
            target_id=claim_id,
            decision=(
                "revoked" if request.status == "proposed" else request.status
            ),
            explanation="Reviewer 更新 Claim 判断。",
        )
        state.commit(
            "claim_status_updated",
            {"claim_id": claim_id, "status": request.status},
        )
        return claim.model_dump(mode="json")

    @app.patch("/api/risks/{risk_id}")
    def update_risk(risk_id: str, request: RiskUpdate) -> dict:
        if request.status not in {"open", "accepted", "resolved"}:
            raise HTTPException(status_code=400, detail="invalid_risk_status")
        risk = next(
            (item for item in state.proof.risks if item.id == risk_id), None
        )
        if risk is None:
            raise HTTPException(status_code=404, detail="risk_not_found")
        risk.status = request.status
        state.commit(
            "risk_status_updated",
            {"risk_id": risk_id, "status": request.status},
        )
        return risk.model_dump(mode="json")

    @app.patch("/api/coverage-assertions/{assertion_id}")
    def update_coverage_assertion(
        assertion_id: str, request: CoverageAssertionUpdate
    ) -> dict:
        if request.status not in {"proposed", "confirmed", "rejected"}:
            raise HTTPException(
                status_code=400, detail="invalid_coverage_assertion_status"
            )
        assertion = next(
            (
                item
                for item in state.proof.coverage_assertions
                if item.id == assertion_id
            ),
            None,
        )
        if assertion is None:
            raise HTTPException(
                status_code=404, detail="coverage_assertion_not_found"
            )
        if request.explanation:
            assertion.explanation = request.explanation
        state.decide(
            target_type="coverage_assertion",
            target_id=assertion_id,
            decision=(
                "revoked" if request.status == "proposed" else request.status
            ),
            explanation=request.explanation or "Reviewer 更新覆盖判断。",
        )
        state.commit(
            f"coverage_assertion_{request.status}",
            {
                "assertion_id": assertion_id,
                "status": request.status,
            },
        )
        return assertion.model_dump(mode="json")

    @app.post("/api/verifications")
    def run_verification(request: VerificationRequest) -> dict:
        try:
            parsed_test = parse_command(request.command)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"unsafe_verification_command:{exc}"
            ) from exc
        requirement_ids = {
            item.id for item in state.proof.requirements if not item.deleted
        }
        hunk_ids = {item.id for item in state.proof.patch_hunks}
        file_change_ids = {item.id for item in state.proof.file_changes}
        if not set(request.requirement_ids) <= requirement_ids:
            raise HTTPException(status_code=400, detail="unknown_requirement")
        if not set(request.hunk_ids) <= hunk_ids:
            raise HTTPException(status_code=400, detail="unknown_hunk")
        if not set(request.file_change_ids) <= file_change_ids:
            raise HTTPException(status_code=400, detail="unknown_file_change")
        result_status = "aborted"
        exit_code: int | None = None
        output = ""
        install_command: str | None = None
        install_status = "not_run"
        install_output = ""
        command_argv = parsed_test.argv
        install_command_argv: list[str] = []
        install_network_enabled = False
        test_network_enabled = False
        environment_status = "supported"
        environment_details = {
            "runner": type(state.runner).__name__,
            "platform": platform.system(),
        }
        try:
            with tempfile.TemporaryDirectory(prefix="codecairn-verify-") as name:
                verification_repo = Path(name) / "workspace"
                verification_repo.mkdir()
                build_verification_snapshot(state.repo, verification_repo)
                if request.prepare_dependencies:
                    config = load_repo_config(state.repo)
                    install_command = config.install_command
                    install_result = state.runner.install_dependencies(
                        verification_repo, install_command
                    )
                    install_status = (
                        "passed"
                        if install_result.exit_code == 0
                        else "failed"
                    )
                    install_output = (
                        install_result.stderr or install_result.stdout
                    )
                    install_command_argv = install_result.command_argv
                    install_network_enabled = install_result.network_enabled
                    environment_details.update(
                        {
                            "language": install_result.language,
                            "image": install_result.image,
                            "toolchain": install_result.toolchain,
                            "user": install_result.container_user,
                            "preflight_status": install_result.preflight_status,
                        }
                    )
                    if install_result.exit_code != 0:
                        result_status = "failed"
                        exit_code = install_result.exit_code
                        output = (
                            "Dependency preparation failed; tests were not run."
                        )
                    else:
                        result = state.runner.run_tests(
                            verification_repo, request.command
                        )
                        exit_code = result.exit_code
                        result_status = (
                            "passed" if result.exit_code == 0 else "failed"
                        )
                        output = result.stderr or result.stdout
                        command_argv = result.command_argv or command_argv
                        test_network_enabled = result.network_enabled
                        environment_details.update(
                            {
                                "language": result.language,
                                "image": result.image,
                                "toolchain": result.toolchain,
                                "user": result.container_user,
                                "preflight_status": result.preflight_status,
                            }
                        )
                else:
                    result = state.runner.run_tests(
                        verification_repo, request.command
                    )
                    exit_code = result.exit_code
                    result_status = (
                        "passed" if result.exit_code == 0 else "failed"
                    )
                    output = result.stderr or result.stdout
                    command_argv = result.command_argv or command_argv
                    test_network_enabled = result.network_enabled
                    environment_details.update(
                        {
                            "language": result.language,
                            "image": result.image,
                            "toolchain": result.toolchain,
                            "user": result.container_user,
                            "preflight_status": result.preflight_status,
                        }
                    )
                    if result.exit_code != 0:
                        output += (
                            "\nDependencies were not prepared. "
                            "Enable prepare_dependencies if the command or "
                            "project dependencies are unavailable."
                        )
        except UnsafeExternalSymlink as exc:
            result_status = "aborted"
            output = str(exc)
        except UnsupportedEnvironment as exc:
            result_status = "aborted"
            environment_status = "unsupported"
            environment_details.update(
                exc.environment.model_dump(mode="json")
            )
            output = str(exc)
        except WorkspacePermissionDenied as exc:
            result_status = "aborted"
            exit_code = exc.result.exit_code
            environment_details.update(
                {
                    "language": exc.result.language,
                    "image": exc.result.image,
                    "toolchain": exc.result.toolchain,
                    "user": exc.result.container_user,
                    "preflight_status": "failed",
                }
            )
            output = str(exc)
        except subprocess.TimeoutExpired as exc:
            output = f"Timed out after {exc.timeout} seconds"
        except FileNotFoundError as exc:
            result_status = "failed"
            exit_code = 127
            output = (
                f"Command runner unavailable: {exc}. "
                "Dependencies were not prepared; enable prepare_dependencies "
                "when appropriate."
            )
        except Exception as exc:  # Runner failures are evidence, not API failures.
            result_status = "failed"
            exit_code = getattr(exc, "returncode", None)
            output = f"Verification execution failed: {exc}"
        current_tree = _workspace_tree(state.repo)
        verification = Verification(
            id=_id(
                "verify",
                request.command,
                str(len(state.proof.verifications)),
                current_tree,
                *request.requirement_ids,
                *request.hunk_ids,
                *request.file_change_ids,
            ),
            command=request.command,
            command_argv=command_argv,
            result_status=result_status,
            effective_status=result_status,
            requirement_ids=request.requirement_ids,
            hunk_ids=request.hunk_ids,
            file_change_ids=request.file_change_ids,
            exit_code=exit_code,
            output_summary=redact_sensitive_output(output),
            commit_sha=state.proof.git_snapshot.head_sha,
            workspace_tree_sha=current_tree,
            content_tree_hash=current_tree,
            patch_fingerprint=state.proof.git_snapshot.patch_fingerprint,
            prepare_dependencies=request.prepare_dependencies,
            install_command=install_command,
            install_command_argv=install_command_argv,
            install_status=install_status,
            install_output_summary=redact_sensitive_output(install_output),
            install_network_enabled=install_network_enabled,
            test_network_enabled=test_network_enabled,
            environment_status=environment_status,
            environment=environment_details,
            provenance=Provenance(kind="verified", source="sandbox_execution"),
        )
        state.proof.verifications.append(verification)
        proposed: list[CoverageAssertion] = []
        for target_type, target_ids in (
            ("requirement", request.requirement_ids),
            ("hunk", request.hunk_ids),
            ("file_change", request.file_change_ids),
        ):
            for target_id in target_ids:
                proposed.append(
                    CoverageAssertion(
                        id=_id(
                            "coverage",
                            verification.id,
                            target_type,
                            target_id,
                        ),
                        verification_id=verification.id,
                        target_type=target_type,
                        target_id=target_id,
                        status="proposed",
                        explanation=(
                            "运行验证时由用户选择的候选覆盖关系，"
                            "尚需 Reviewer 确认。"
                        ),
                        provenance=Provenance(
                            kind="captured",
                            source="reviewer_selection",
                            confidence=1.0,
                        ),
                    )
                )
        state.proof.coverage_assertions.extend(proposed)
        if proposed:
            state.audit(
                "coverage_assertions_proposed",
                {"assertion_ids": [item.id for item in proposed]},
                actor_type="reviewer",
                actor_id="local_reviewer",
            )
        state.commit(
            "verification_run",
            {
                "verification_id": verification.id,
                "requirement_ids": request.requirement_ids,
                "hunk_ids": request.hunk_ids,
                "file_change_ids": request.file_change_ids,
                "prepare_dependencies": request.prepare_dependencies,
                "coverage_assertion_ids": [item.id for item in proposed],
            },
            actor_type="sandbox",
            actor_id="sandbox_execution",
        )
        return verification.model_dump(mode="json")

    @app.get("/api/export/markdown", response_class=PlainTextResponse)
    def export_markdown() -> str:
        state.refresh()
        return proof_markdown(state.proof, stale=state.stale)

    @app.get("/api/export/json")
    def export_json() -> dict:
        state.refresh()
        return state.proof.model_dump(mode="json")

    @app.get("/api/files/{file_change_id}/comparison")
    def get_file_comparison(file_change_id: str) -> dict:
        file_change = next(
            (
                item
                for item in state.proof.file_changes
                if item.id == file_change_id
            ),
            None,
        )
        if file_change is None:
            raise HTTPException(status_code=404, detail="file_change_not_found")
        return build_file_comparison(
            state.repo,
            state.proof.git_snapshot.base_sha,
            file_change,
            state.proof.patch_hunks,
        )

    @app.get("/api/graph")
    def get_graph() -> dict:
        state.refresh()
        return build_evidence_graph(state.proof).model_dump(mode="json")

    @app.get("/api/export/html", response_class=HTMLResponse)
    def export_html() -> str:
        state.refresh()
        graph = build_evidence_graph(state.proof)
        return graph_html(state.proof, graph)

    @app.get("/api/export/svg")
    def export_svg() -> Response:
        state.refresh()
        return Response(
            graph_svg(build_evidence_graph(state.proof)),
            media_type="image/svg+xml",
        )

    @app.get("/api/export/png")
    def export_png() -> Response:
        state.refresh()
        try:
            payload = graph_png(build_evidence_graph(state.proof))
        except ExportRenderError as exc:
            return JSONResponse(
                {"detail": str(exc)}, status_code=503
            )
        return Response(payload, media_type="image/png")

    @app.get("/api/revisions")
    def list_revisions() -> list[dict]:
        return [
            item.model_dump(mode="json")
            for item in load_review_revisions(
                state.proof.review_series_id, state.storage_root
            )
        ]

    @app.get("/api/revision-family")
    def list_revision_family() -> list[dict]:
        return [
            item.model_dump(mode="json")
            for item in load_review_family_revisions(
                state.proof.review_family_id, state.storage_root
            )
        ]

    return app


def create_app(
    repo: Path,
    *,
    base_ref: str | None = None,
    requirement_texts: list[str] | None = None,
) -> FastAPI:
    state = load_or_create_review_state(
        repo, base_ref=base_ref, requirement_texts=requirement_texts
    )
    return create_review_app(state)


REVIEW_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="codecairn-session" content="__CODECAIRN_TOKEN__">
<title>CodeCairn Review</title>
<link rel="stylesheet" href="/assets/review.css">
<script src="/assets/review.js" defer></script></head>
<body>
<header class="topbar">
  <div class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></div>
  <div class="brand-copy"><strong>CodeCairn</strong><span>Review</span></div>
  <div class="review-title"><h1 id="title">正在载入变更…</h1><p id="summary"></p></div>
  <nav class="view-tabs" aria-label="审查视图">
    <button id="diffButton" class="tab active">变更</button>
    <button id="graphButton" class="tab">逻辑图</button>
  </nav>
  <div class="review-state">
    <span id="stale" class="status stale" hidden></span>
    <span id="assurance" class="status" title="当前变更的证据完整度"></span>
  </div>
  <details class="export-menu">
    <summary id="exportButton">导出</summary>
    <div class="export-options">
      <button data-export="markdown">Markdown</button>
      <button data-export="html">静态 HTML</button>
      <button data-export="svg">SVG 图谱</button>
      <button data-export="png">PNG 图谱</button>
      <button data-export="json">原始 JSON</button>
    </div>
  </details>
</header>
<div class="workspace">
  <aside class="file-panel">
    <div class="panel-heading"><h2>变更文件</h2><span id="fileCount"></span></div>
    <input id="fileSearch" type="search" placeholder="搜索路径" aria-label="搜索变更文件">
    <div id="loadingProgress" class="loading-progress" hidden>
      <div><span id="loadingMessage">正在读取代码变更…</span><strong id="loadingCount"></strong></div>
      <progress id="loadingBar" value="0" max="1"></progress>
    </div>
    <div id="files" class="file-list"></div>
    <div class="change-stats" id="changeStats"></div>
  </aside>
  <main class="review-panel">
    <section id="diffView">
      <div class="file-toolbar">
        <div><strong id="currentPath">选择文件</strong><span id="fileMeta"></span></div>
        <button id="toggleDrawer" class="logic-toggle" title="显示修改逻辑">修改逻辑</button>
      </div>
      <div class="column-heads"><span>变更前</span><span>变更后</span></div>
      <div id="comparison" class="diff-table" aria-live="polite"></div>
    </section>
    <section id="graphView" class="graph-view" hidden>
      <div class="graph-heading">
        <div>
          <span class="eyebrow">全局证据链</span>
          <h2>开发逻辑链</h2>
          <p class="graph-description">从需求和决策追踪到代码、证据与验证。</p>
        </div>
        <p id="graphSummary"></p>
      </div>
      <div id="graphContent"></div>
    </section>
  </main>
  <aside class="evidence-panel" id="evidencePanel">
    <div class="logic-heading">
      <div><span class="eyebrow">变更依据</span><h2>修改逻辑</h2></div>
      <button id="closeDrawer" class="icon-button" title="关闭修改逻辑" aria-label="关闭修改逻辑">×</button>
    </div>
    <div id="logic" class="logic-content"></div>
    <div id="ciTrust" hidden></div>
  </aside>
</div>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
</body></html>"""


REVIEW_CSS = """
:root{color-scheme:light;--bg:#eef1f4;--panel:#fff;--panel-subtle:#f7f8fa;--panel-strong:#f0f3f5;--line:#dfe3e8;--line-strong:#c4cbd3;--text:#17202a;--muted:#687483;--faint:#8d98a5;--brand:#08aeb5;--brand-dark:#087d83;--brand-soft:#e6f7f7;--green:#16834a;--green-soft:#e6f5ec;--red:#bb3b45;--red-soft:#fcebed;--amber:#95620a;--amber-soft:#fff5db;--blue:#3468a8;--blue-soft:#edf4fb;--code:#232a33;--shadow:0 16px 38px rgba(30,42,55,.15)}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#10151b;--panel:#171d24;--panel-subtle:#1c232b;--panel-strong:#222a34;--line:#303944;--line-strong:#4a5663;--text:#e8edf2;--muted:#a1acb8;--faint:#7d8996;--brand:#24c2c8;--brand-dark:#56d4d8;--brand-soft:#14383a;--green:#77d29b;--green-soft:#18382a;--red:#f18b91;--red-soft:#3c2227;--amber:#f4c56e;--amber-soft:#3a311c;--blue:#8bb8ec;--blue-soft:#1b2d42;--code:#e6ebf1;--shadow:0 18px 44px rgba(0,0,0,.35)}}
*{box-sizing:border-box;letter-spacing:0}[hidden]{display:none!important}html,body{height:100%}body{margin:0;overflow:hidden;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select,summary{font:inherit}button,summary{color:var(--text);cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.topbar{height:64px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;padding:8px 16px;position:relative;z-index:8}.brand-mark{width:31px;height:31px;display:flex;align-items:flex-end;gap:2px;flex:0 0 auto}.brand-mark i{display:block;width:8px;background:var(--brand)}.brand-mark i:nth-child(1){height:13px}.brand-mark i:nth-child(2){height:22px}.brand-mark i:nth-child(3){height:31px}.brand-copy{display:flex;flex-direction:column;min-width:94px}.brand-copy strong{font-size:14px}.brand-copy span,.review-title p{color:var(--muted);font-size:11px}.review-title{min-width:160px;flex:1;overflow:hidden;border-left:1px solid var(--line);padding-left:14px}.review-title h1{margin:0;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.review-title p{margin:2px 0 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.view-tabs{display:flex;height:40px;background:var(--panel-subtle);border:1px solid var(--line);border-radius:6px;padding:3px}.tab{border:0;background:transparent;padding:0 14px;border-radius:4px;color:var(--muted);font-weight:650}.tab.active{background:var(--panel);color:var(--text);box-shadow:0 1px 3px rgba(20,30,40,.1)}.review-state{display:flex;gap:6px}.status{border:1px solid var(--line);border-radius:4px;padding:4px 7px;font-size:10px;font-weight:750;text-transform:uppercase;color:var(--muted)}.status.stale{border-color:var(--amber);color:var(--amber);background:var(--amber-soft)}.export-menu{position:relative}.export-menu summary{list-style:none;border:1px solid var(--brand);background:var(--brand);color:#052f31;border-radius:5px;padding:7px 12px;font-weight:750}.export-menu summary::-webkit-details-marker{display:none}.export-options{position:absolute;right:0;top:42px;width:178px;background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow);padding:5px;border-radius:6px}.export-options button{display:block;width:100%;border:0;background:transparent;text-align:left;padding:8px 9px;border-radius:4px}.export-options button:hover{background:var(--brand-soft)}
.workspace{height:calc(100vh - 64px);display:grid;grid-template-columns:252px minmax(540px,1fr) minmax(360px,420px);min-height:0}.workspace.drawer-closed{grid-template-columns:252px minmax(540px,1fr)}.file-panel,.evidence-panel{background:var(--panel);overflow:auto;min-height:0}.file-panel{border-right:1px solid var(--line);padding:16px 10px 12px}.evidence-panel{border-left:1px solid var(--line);padding:0 18px 24px}.evidence-panel.closed{display:none}.panel-heading,.logic-heading{display:flex;align-items:center;justify-content:space-between;gap:8px}.panel-heading{margin:0 5px 12px}.panel-heading h2,.logic-heading h2,.graph-heading h2{margin:0;font-size:13px}.panel-heading>span{color:var(--muted);font-size:11px}.logic-heading{position:sticky;top:0;z-index:2;background:var(--panel);padding:16px 0 12px;border-bottom:1px solid var(--line)}.eyebrow{display:block;color:var(--brand-dark);font-size:9px;font-weight:850;margin-bottom:2px}#fileSearch{width:100%;height:34px;border:1px solid var(--line);background:var(--panel-subtle);border-radius:5px;padding:0 10px;color:var(--text);margin-bottom:10px}.file-list{display:flex;flex-direction:column;gap:2px}.file-row{width:100%;min-height:44px;border:0;border-left:3px solid transparent;background:transparent;text-align:left;padding:7px 8px;display:grid;grid-template-columns:24px minmax(0,1fr);align-items:center;border-radius:0 4px 4px 0}.file-row:hover{background:var(--panel-subtle)}.file-row.active{background:var(--brand-soft);border-left-color:var(--brand)}.file-kind{display:grid;place-items:center;width:18px;height:18px;border:1px solid var(--line-strong);border-radius:3px;font-size:9px;font-weight:850;color:var(--muted)}.file-row.active .file-kind{border-color:var(--brand);color:var(--brand-dark)}.file-copy{min-width:0}.file-name{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:12px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace}.file-folder{display:block;color:var(--muted);font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}.change-stats{border-top:1px solid var(--line);margin:14px 5px 0;padding-top:12px;color:var(--muted);font-size:11px}
.workspace.graph-active{grid-template-columns:minmax(0,1fr) minmax(360px,420px)}.workspace.graph-active.drawer-closed{grid-template-columns:minmax(0,1fr)}.workspace.graph-active .file-panel{display:none}
.loading-progress{position:sticky;top:0;z-index:3;margin:0 0 10px;padding:9px 9px 8px;background:var(--panel);border:1px solid var(--line);border-radius:5px;box-shadow:0 6px 14px rgba(20,35,45,.08)}.loading-progress>div{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:7px;color:var(--muted);font-size:10px}.loading-progress strong{color:var(--brand-dark);font-size:10px}.loading-progress progress{display:block;width:100%;height:4px;border:0;border-radius:3px;overflow:hidden;background:var(--line)}.loading-progress progress::-webkit-progress-bar{background:var(--line)}.loading-progress progress::-webkit-progress-value{background:var(--brand);transition:width .22s ease}.loading-progress progress::-moz-progress-bar{background:var(--brand)}.loading-file-row{pointer-events:none;animation:file-arrive .42s cubic-bezier(.2,.8,.2,1) both;background:var(--brand-soft);border-left-color:var(--brand)}.loading-file-row::after{content:"";grid-column:2;width:42%;height:2px;margin-top:5px;background:linear-gradient(90deg,var(--brand),transparent);animation:file-scan .8s ease both}.loading-file-row .file-kind{border-color:var(--brand);color:var(--brand-dark)}@keyframes file-arrive{0%{opacity:0;transform:translateY(-8px)}65%{opacity:1;transform:translateY(1px)}100%{opacity:1;transform:none;background:transparent}}@keyframes file-scan{0%{opacity:0;transform:scaleX(.2);transform-origin:left}45%{opacity:1}100%{opacity:0;transform:scaleX(1);transform-origin:left}}.loading-shell{max-width:430px;margin:72px auto;padding:0 24px;text-align:center;color:var(--muted)}.loading-shell-mark{width:36px;height:28px;margin:0 auto 15px;display:flex;align-items:flex-end;justify-content:center;gap:3px}.loading-shell-mark i{width:7px;background:var(--brand);animation:loading-bars 1s ease-in-out infinite}.loading-shell-mark i:nth-child(1){height:12px}.loading-shell-mark i:nth-child(2){height:21px;animation-delay:.12s}.loading-shell-mark i:nth-child(3){height:28px;animation-delay:.24s}.loading-shell strong{display:block;color:var(--text);margin-bottom:5px}.loading-shell p{margin:0;font-size:12px}@keyframes loading-bars{0%,100%{opacity:.35;transform:scaleY(.65);transform-origin:bottom}50%{opacity:1;transform:scaleY(1)}}.topbar.loading .view-tabs button,.topbar.loading .export-menu{opacity:.45;pointer-events:none}.topbar.loading .status{color:var(--brand-dark);border-color:var(--brand)}
.review-panel{min-width:0;overflow:hidden;background:var(--panel)}#diffView{height:100%;display:flex;flex-direction:column}.file-toolbar{height:48px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 12px;gap:12px}.file-toolbar>div{min-width:0;overflow:hidden}.file-toolbar strong{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}.file-toolbar span{margin-left:9px;color:var(--muted);font-size:10px;text-transform:uppercase}.logic-toggle,.icon-button{border:1px solid var(--line);background:var(--panel);border-radius:4px;padding:5px 8px;white-space:nowrap}.logic-toggle:hover,.icon-button:hover{border-color:var(--brand);color:var(--brand-dark)}.icon-button{width:29px;height:29px;padding:0;font-size:18px;color:var(--muted)}.column-heads{height:34px;display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--line);background:var(--panel-subtle);color:var(--muted);font-size:10px;font-weight:750}.column-heads span{padding:9px 48px}.column-heads span+span{border-left:1px solid var(--line)}.diff-table{flex:1;overflow:auto;font:12px/20px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--code);scrollbar-color:var(--line-strong) transparent}.diff-row{display:grid;grid-template-columns:minmax(360px,1fr) minmax(360px,1fr);min-height:20px;border-bottom:1px solid transparent;align-items:stretch}.diff-row.changed{cursor:pointer}.diff-row.changed:hover{box-shadow:inset 3px 0 var(--brand)}.diff-row.selected{box-shadow:inset 3px 0 var(--brand);outline:1px solid var(--brand);outline-offset:-1px}.code-side{display:grid;grid-template-columns:42px minmax(0,1fr);min-width:0;align-items:stretch}.code-side+.code-side{border-left:1px solid var(--line)}.line-number{background:var(--panel-subtle);border-right:1px solid var(--line);color:var(--faint);text-align:right;padding-right:8px;user-select:none}.code-text{white-space:pre-wrap;overflow-wrap:anywhere;word-break:normal;min-width:0;min-height:20px;padding:0 9px;overflow:hidden;tab-size:4}.diff-row.insert .after,.diff-row.replace .after{background:var(--green-soft)}.diff-row.insert .after .line-number,.diff-row.replace .after .line-number{background:color-mix(in srgb,var(--green-soft) 70%,var(--green) 30%)}.diff-row.delete .before,.diff-row.replace .before{background:var(--red-soft)}.diff-row.delete .before .line-number,.diff-row.replace .before .line-number{background:color-mix(in srgb,var(--red-soft) 70%,var(--red) 30%)}.empty-message{padding:40px 18px;color:var(--muted);text-align:center}
.logic-content{display:flex;flex-direction:column}.logic-hero{padding:14px 0 13px;border-bottom:1px solid var(--line)}.logic-hero strong{display:block;font-size:13px;margin-bottom:4px}.logic-hero p{margin:0;color:var(--muted);font-size:12px}.logic-section{padding:14px 0;border-bottom:1px solid var(--line)}.logic-section.primary{border-left:3px solid var(--brand);padding-left:12px}.logic-section.warning{border-left:3px solid var(--amber);padding-left:12px}.logic-section-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:7px}.logic-section h3{font-size:13px;margin:0;overflow-wrap:anywhere}.section-label{font-size:9px;font-weight:850;color:var(--muted);text-transform:uppercase}.source-badge,.state-badge{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:3px;padding:2px 5px;font-size:9px;font-weight:750;color:var(--muted);white-space:nowrap}.source-badge.captured{color:var(--brand-dark);border-color:var(--brand);background:var(--brand-soft)}.source-badge.derived,.source-badge.inferred{color:var(--blue);border-color:var(--blue);background:var(--blue-soft)}.source-badge.verified{color:var(--green);border-color:var(--green);background:var(--green-soft)}.logic-section p{margin:5px 0;color:var(--muted);overflow-wrap:anywhere}.logic-section .logic-main{color:var(--text);font-weight:600}.logic-section pre{margin:8px 0 0;padding:9px 10px;background:var(--panel-subtle);border-left:2px solid var(--line-strong);white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}.logic-list{margin:7px 0 0;padding-left:18px;color:var(--muted)}.logic-list li+li{margin-top:4px}.logic-kv{display:grid;grid-template-columns:82px minmax(0,1fr);gap:5px 10px;margin-top:10px;font-size:11px}.logic-kv dt{color:var(--faint)}.logic-kv dd{margin:0;color:var(--text);overflow-wrap:anywhere}.logic-details{border-bottom:1px solid var(--line);padding:11px 0}.logic-details summary{color:var(--muted);font-size:11px;font-weight:700;list-style:none}.logic-details summary::before{content:"+";display:inline-block;width:16px;color:var(--brand-dark)}.logic-details[open] summary::before{content:"−"}.logic-details .logic-section{border-bottom:0;padding-bottom:2px}.logic-empty{color:var(--muted);padding:18px 0}.requirement-list{margin:6px 0;padding-left:18px}
.graph-view{height:100%;overflow:auto;padding:22px 24px;background:var(--panel-subtle)}.graph-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;border-bottom:1px solid var(--line);padding-bottom:14px}.graph-heading p{margin:0;color:var(--muted);font-size:11px}.graph-description{margin-top:4px!important}.graph-overview{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:1px;margin:16px 0;background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden}.graph-step{min-height:78px;background:var(--panel);padding:11px 13px;position:relative}.graph-step+.graph-step::before{content:"›";position:absolute;left:-8px;top:25px;z-index:1;width:16px;height:24px;display:grid;place-items:center;background:var(--panel);color:var(--brand-dark);font-size:18px}.graph-step span{display:block;color:var(--muted);font-size:10px}.graph-step strong{display:block;margin-top:3px;font-size:20px}.graph-step small{display:block;margin-top:2px;color:var(--faint);font-size:9px}.graph-alert{display:flex;align-items:flex-start;gap:9px;margin:12px 0;padding:10px 12px;border-left:3px solid var(--amber);background:var(--amber-soft);color:var(--amber);font-size:11px}.graph-alert.success{border-left-color:var(--green);background:var(--green-soft);color:var(--green)}.graph-toolbar{display:flex;align-items:center;gap:8px;margin:14px 0}.graph-mode{display:flex;padding:3px;border:1px solid var(--line);background:var(--panel);border-radius:5px}.graph-mode button{border:0;background:transparent;color:var(--muted);padding:5px 10px;border-radius:3px;font-size:11px;font-weight:700}.graph-mode button.active{background:var(--brand-soft);color:var(--brand-dark)}.graph-search{height:32px;min-width:180px;flex:1;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:4px;padding:0 9px}.graph-controls{display:flex;gap:8px}.graph-controls select{height:32px;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:4px;padding:0 28px 0 9px}.graph-result-summary{margin:0 0 9px;color:var(--muted);font-size:10px}.graph-group{margin:8px 0;border:1px solid var(--line);background:var(--panel);border-radius:5px;overflow:hidden}.graph-group>summary{list-style:none;padding:11px 13px;font-size:11px;font-weight:750;color:var(--text);display:flex;align-items:center;gap:7px}.graph-group>summary::before{content:"+";display:inline-grid;place-items:center;width:18px;height:18px;color:var(--brand-dark);background:var(--brand-soft);border-radius:3px}.graph-group[open]>summary::before{content:"−"}.graph-group-description{margin:-5px 13px 11px 38px;color:var(--muted);font-size:10px}.graph-layer{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:7px;padding:0 12px 12px}.graph-node{border:1px solid var(--line);border-left:3px solid var(--line-strong);background:var(--panel-subtle);border-radius:4px;padding:9px 10px;text-align:left;min-height:64px;white-space:pre-line}.graph-node:hover{border-left-color:var(--brand);background:var(--brand-soft)}.graph-node.incomplete{border-left-color:var(--amber)}.graph-more{margin:0 12px 12px;border:1px solid var(--line);background:var(--panel-subtle);color:var(--muted);border-radius:4px;padding:6px 10px}.graph-more:hover{border-color:var(--brand);color:var(--brand-dark)}.toast{position:fixed;right:18px;bottom:18px;z-index:10;background:#17212b;color:#fff;border-radius:5px;padding:9px 13px;opacity:0;transform:translateY(8px);pointer-events:none;transition:.18s}.toast.visible{opacity:1;transform:none}
@media(max-width:1360px){.workspace{grid-template-columns:220px minmax(500px,1fr)}.workspace.drawer-closed{grid-template-columns:220px minmax(500px,1fr)}.evidence-panel{position:fixed;z-index:7;right:0;top:64px;bottom:0;width:min(380px,92vw);box-shadow:var(--shadow)}}
@media(max-width:900px){.graph-overview{grid-template-columns:repeat(2,1fr)}.graph-step:last-child:nth-child(odd){grid-column:1/-1}.graph-step+.graph-step::before{display:none}.graph-toolbar{align-items:stretch;flex-wrap:wrap}.graph-search{order:3;flex-basis:100%}}
@media(max-width:760px){.topbar{height:58px;padding:7px 9px;gap:8px}.brand-copy,.review-title,.review-state{display:none}.brand-mark{width:28px;height:28px}.view-tabs{margin-left:2px;height:36px}.tab{padding:0 10px}.export-menu{margin-left:auto}.export-menu summary{padding:6px 9px}.workspace,.workspace.drawer-closed{height:calc(100vh - 58px);display:grid;grid-template-columns:1fr;grid-template-rows:176px minmax(0,1fr)}.workspace.graph-active,.workspace.graph-active.drawer-closed{grid-template-rows:minmax(0,1fr)}.file-panel{border-right:0;border-bottom:1px solid var(--line);padding-top:10px}.file-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.change-stats{display:none}.review-panel{min-height:0}.evidence-panel{top:58px;width:calc(100vw - 16px)}.column-heads span{padding-left:47px}.diff-row{grid-template-columns:minmax(300px,1fr) minmax(300px,1fr)}.graph-view{padding:14px}.graph-heading{align-items:flex-start;flex-direction:column;gap:4px}.graph-overview{grid-template-columns:1fr 1fr}.graph-step{min-height:66px}.graph-mode,.graph-controls{width:100%}.graph-mode button,.graph-controls select{flex:1;min-width:0}.graph-layer{grid-template-columns:1fr}}
"""


REVIEW_JS = r"""
'use strict';
const meta = document.querySelector('meta[name="codecairn-session"]');
const token = meta ? meta.content : sessionStorage.getItem('codecairn-session');
if (token) sessionStorage.setItem('codecairn-session', token);
if (meta) meta.remove();
history.replaceState({}, document.title, location.pathname + location.hash);
let proof = null;
let selectedFileId = null;
let selectedRow = null;
let comparison = null;
let graphCache = null;
const comparisonCache = new Map();
const byId = id => document.getElementById(id);
const clear = node => { while (node.firstChild) node.removeChild(node.firstChild); };
const text = (tag, value, className) => {
  const node = document.createElement(tag);
  node.textContent = String(value ?? '');
  if (className) node.className = className;
  return node;
};
const button = (label, handler, className) => {
  const node = text('button', label, className);
  node.type = 'button';
  node.addEventListener('click', handler);
  return node;
};
async function api(path, options = {}) {
  options.headers = {...(options.headers || {}), 'X-CodeCairn-Token': token};
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return response;
}
function toast(message) {
  const node = byId('toast');
  node.textContent = message;
  node.classList.add('visible');
  window.setTimeout(() => node.classList.remove('visible'), 2200);
}
function fileLabel(item) {
  const parts = item.path.split('/');
  return {name: parts.pop() || item.path, folder: parts.join('/')};
}
function renderFiles(filter = '') {
  const target = byId('files');
  clear(target);
  const query = filter.trim().toLowerCase();
  proof.file_changes.filter(item => item.path.toLowerCase().includes(query)).forEach(item => {
    const label = fileLabel(item);
    const row = button('', () => selectFile(item.id), 'file-row');
    row.dataset.fileId = item.id;
    row.title = item.old_path ? `${item.old_path} → ${item.path}` : item.path;
    const kind = text('span', item.change_type.slice(0, 1).toUpperCase(), 'file-kind');
    const copy = text('span', '', 'file-copy');
    copy.append(text('span', label.name, 'file-name'));
    if (label.folder) copy.append(text('span', label.folder, 'file-folder'));
    row.append(kind, copy);
    row.classList.toggle('active', item.id === selectedFileId);
    target.appendChild(row);
  });
}
function codeSide(side, line, value) {
  const node = text('div', '', `code-side ${side}`);
  node.append(text('span', line ?? '', 'line-number'), text('span', value, 'code-text'));
  return node;
}
function renderComparison() {
  const target = byId('comparison');
  clear(target);
  if (!comparison) {
    target.appendChild(text('p', '从左侧选择一个修改文件。', 'empty-message'));
    return;
  }
  if (comparison.binary) {
    target.appendChild(text('p', '这是二进制文件，无法显示文本对比。', 'empty-message'));
    return;
  }
  if (!comparison.rows.length) {
    target.appendChild(text('p', '文件内容没有可显示的文本行。', 'empty-message'));
    return;
  }
  comparison.rows.forEach((row, index) => {
    const line = text('div', '', `diff-row ${row.kind}`);
    if (row.kind !== 'context') {
      line.classList.add('changed');
      line.tabIndex = 0;
      line.title = '查看这段修改的证据链';
      const choose = () => selectChangedRow(index, line);
      line.addEventListener('click', choose);
      line.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') choose();
      });
    }
    line.append(
      codeSide('before', row.old_line, row.old_text),
      codeSide('after', row.new_line, row.new_text)
    );
    target.appendChild(line);
  });
}
async function selectFile(id) {
  selectedFileId = id;
  selectedRow = null;
  renderFiles(byId('fileSearch').value);
  const file = proof.file_changes.find(item => item.id === id);
  byId('currentPath').textContent = file.old_path ?
    `${file.old_path} → ${file.path}` : file.path;
  byId('fileMeta').textContent = ({
    added: '新增',
    modified: '修改',
    deleted: '删除',
    renamed: '重命名',
    binary: '二进制'
  })[file.change_type] || file.change_type;
  byId('comparison').replaceChildren(text('p', '正在对齐文件内容…', 'empty-message'));
  if (!comparisonCache.has(id)) {
    comparisonCache.set(
      id,
      await (await api(`/api/files/${id}/comparison`)).json()
    );
  }
  comparison = comparisonCache.get(id);
  renderComparison();
  renderFileOverview(file);
}
function openDrawer() {
  byId('evidencePanel').classList.remove('closed');
  document.querySelector('.workspace').classList.remove('drawer-closed');
}
function closeDrawer() {
  byId('evidencePanel').classList.add('closed');
  document.querySelector('.workspace').classList.add('drawer-closed');
}
const sourceLabel = value => ({
  captured: 'Agent 记录',
  derived: '系统推导',
  inferred: '静态分析',
  verified: '验证结果',
  unknown: '来源未知'
}[value] || value || '来源未知');
const statusLabel = value => ({
  accepted: '已记录',
  confirmed: '已确认',
  proposed: '待确认',
  rejected: '已拒绝',
  passed: '通过',
  failed: '失败',
  stale: '已过期',
  not_run: '未运行',
  aborted: '已中止',
  active: '当前版本',
  open: '未处理',
  unreviewed: '待评审',
  reviewed: '已评审',
  warning: '需关注',
  incomplete: '信息不完整'
}[value] || value || '');
const graphTypeLabel = value => ({
  requirement: '需求',
  requirement_revision: '需求版本',
  file_change: '文件变更',
  hunk: '代码块',
  implementation_decision: '修改决策',
  claim: '变更结论',
  evidence: '代码证据',
  verification: '验证结果',
  ci_verification: 'CI 验证',
  risk: '风险与缺口',
  capture_event: '采集事件',
  capture_event: '采集事件',
  ledger_event: '审计事件',
  review_decision: '评审决定',
  git_snapshot_revision: 'Git 快照',
  publication: '发布记录',
  coverage_assertion: '覆盖声明',
  revision: '变更版本'
}[value] || value || '未知节点');
const graphNodeLabel = value => String(value || '')
  .replace(/^added:\s*/i, '新增：')
  .replace(/^modified:\s*/i, '修改：')
  .replace(/^deleted:\s*/i, '删除：')
  .replace(/^renamed:\s*/i, '重命名：');
function badge(value, kind = 'source-badge') {
  const node = text('span', sourceLabel(value), `${kind} ${value || 'unknown'}`);
  return node;
}
function logicSection(label, title, body = '', options = {}) {
  const node = text('section', '', `logic-section ${options.tone || ''}`.trim());
  const head = text('div', '', 'logic-section-head');
  const heading = text('div', '', '');
  heading.append(text('span', label, 'section-label'), text('h3', title));
  head.appendChild(heading);
  if (options.source) head.appendChild(badge(options.source));
  else if (options.status) {
    head.appendChild(text('span', statusLabel(options.status), 'state-badge'));
  }
  node.appendChild(head);
  if (body) node.appendChild(text('p', body, options.main ? 'logic-main' : ''));
  if (options.detail) node.appendChild(text('pre', options.detail));
  return node;
}
function addList(node, values) {
  const items = (values || []).filter(Boolean);
  if (!items.length) return;
  const list = text('ul', '', 'logic-list');
  items.forEach(item => list.appendChild(text('li', item)));
  node.appendChild(list);
}
function addKeyValues(node, values) {
  const entries = values.filter(([, value]) =>
    Array.isArray(value) ? value.length : Boolean(value));
  if (!entries.length) return;
  const list = text('dl', '', 'logic-kv');
  entries.forEach(([key, value]) => {
    list.append(text('dt', key), text('dd',
      Array.isArray(value) ? value.join('；') : value));
  });
  node.appendChild(list);
}
function decisionEventIds(decisionId) {
  return new Set((proof.capture_events || [])
    .filter(event => event.payload && event.payload.decision &&
      event.payload.decision.decision_id === decisionId)
    .map(event => event.event_id));
}
function relevantDecisions(file, claims = []) {
  const claimEvents = new Set(claims.flatMap(item =>
    item.provenance.source_event_ids || []));
  return (proof.decision_records || []).filter(decision => {
    const eventIds = decisionEventIds(decision.decision_id);
    return [...eventIds].some(id => claimEvents.has(id)) ||
      decision.affected_paths.includes(file.path);
  });
}
function decisionSection(decision) {
  const node = logicSection(
    '修改决策',
    decision.summary,
    decision.rationale,
    {status: decision.status, tone: 'primary', main: true}
  );
  addKeyValues(node, [
    ['影响文件', decision.affected_paths],
    ['备选方案', decision.alternatives],
    ['风险', decision.risks],
    ['验证计划', decision.verification_plan]
  ]);
  return node;
}
function renderFileOverview(file) {
  const target = byId('logic');
  clear(target);
  const hunks = proof.patch_hunks.filter(item => item.file_change_id === file.id);
  const decisions = relevantDecisions(file);
  const hero = text('div', '', 'logic-hero');
  hero.append(
    text('strong', file.path),
    text('p', `${file.summary} · ${hunks.length} 个代码块 · ${decisions.length} 个决策`)
  );
  target.appendChild(hero);
  decisions.forEach(item => target.appendChild(decisionSection(item)));
  const requirements = proof.requirements.filter(item =>
    !item.deleted && file.requirement_ids.includes(item.id));
  if (requirements.length) {
    const section = logicSection('关联需求', '本次变更对应的需求');
    addList(section, requirements.map(item => item.text));
    target.appendChild(section);
  }
  if (!decisions.length) {
    target.appendChild(logicSection(
      '决策记录缺失',
      '没有捕获到文件级修改决策',
      '当前变更只能依赖静态分析解释，无法还原 Agent 修改前的判断依据。',
      {tone: 'warning'}
    ));
  }
}
function selectChangedRow(index, node) {
  selectedRow = index;
  document.querySelectorAll('.diff-row.selected').forEach(item =>
    item.classList.remove('selected'));
  node.classList.add('selected');
  openDrawer();
  const row = comparison.rows[index];
  const hunk = proof.patch_hunks.find(item => row.hunk_ids.includes(item.id));
  renderLogicChain(hunk, row);
}
function renderLogicChain(hunk, row) {
  const target = byId('logic');
  clear(target);
  const file = proof.file_changes.find(item => item.id === selectedFileId);
  if (!hunk) {
    target.append(
      text('p', '这行属于文件级变更，但当前没有可关联的文本 Hunk。', 'logic-empty'),
      logicSection('FILE CHANGE', file.path, file.summary)
    );
    return;
  }
  const requirements = proof.requirements.filter(item =>
    !item.deleted && hunk.requirement_ids.includes(item.id));
  const allClaims = proof.claims.filter(item => hunk.claim_ids.includes(item.id));
  const capturedClaims = allClaims.filter(item => item.provenance.kind === 'captured');
  const primaryClaims = capturedClaims.length ? capturedClaims : allClaims;
  const derivedClaims = capturedClaims.length ?
    allClaims.filter(item => item.provenance.kind !== 'captured') : [];
  const evidenceIds = new Set(primaryClaims.flatMap(item => item.evidence_ids));
  const evidence = proof.evidence.filter(item => evidenceIds.has(item.id));
  const capturedEvidence = evidence.filter(item => item.provenance.kind === 'captured');
  const derivedEvidence = evidence.filter(item => item.provenance.kind !== 'captured');
  const decisions = relevantDecisions(file, primaryClaims);
  const verifications = proof.verifications.filter(item =>
    item.hunk_ids.includes(hunk.id) || item.file_change_ids.includes(file.id) ||
    item.requirement_ids.some(id => hunk.requirement_ids.includes(id)));
  const lineLabel = row.old_line && row.new_line ?
    `L${row.old_line} → L${row.new_line}` :
    row.new_line ? `新增 L${row.new_line}` : `删除 L${row.old_line}`;
  const hero = text('div', '', 'logic-hero');
  hero.append(text('strong', `${file.path} · ${lineLabel}`), text('p', hunk.summary));
  target.appendChild(hero);
  decisions.forEach(item => target.appendChild(decisionSection(item)));
  if (!decisions.length) {
    target.appendChild(logicSection(
      '决策记录缺失',
      '没有捕获到修改前决策',
      '下面的说明来自代码静态分析，不代表 Agent 当时的真实判断。',
      {tone: 'warning'}
    ));
  }
  requirements.forEach(item => target.appendChild(logicSection(
    '需求',
    item.text,
    `${item.category} · revision ${item.revision}`,
    {source: item.provenance.kind}
  )));
  target.appendChild(logicSection(
    '代码变更',
    lineLabel,
    hunk.summary,
    {
      source: hunk.provenance.kind,
      detail: [
        row.old_text ? `- ${row.old_text}` : '',
        row.new_text ? `+ ${row.new_text}` : ''
      ].filter(Boolean).join('\n')
    }
  ));
  primaryClaims.forEach(item => {
    target.appendChild(logicSection(
      '改动说明',
      item.statement,
      statusLabel(item.status),
      {source: item.provenance.kind}
    ));
  });
  capturedEvidence.forEach(item => {
    const excerpt = item.content_excerpt.length > 1200 ?
      `${item.content_excerpt.slice(0, 1200)}\n…` : item.content_excerpt;
    target.appendChild(logicSection(
      item.stale ? '源码证据 · 已过期' : '源码证据',
      `${item.path}${item.line ? ':' + item.line : ''}`,
      item.statement,
      {source: item.provenance.kind, detail: excerpt}
    ));
  });
  if (derivedClaims.length || derivedEvidence.length) {
    const details = text('details', '', 'logic-details');
    details.appendChild(text(
      'summary',
      `系统推导信息 ${derivedClaims.length + derivedEvidence.length} 项`
    ));
    derivedClaims.forEach(item => details.appendChild(logicSection(
      '推导结论',
      item.statement,
      statusLabel(item.status),
      {source: item.provenance.kind}
    )));
    derivedEvidence.forEach(item => details.appendChild(logicSection(
      '推导上下文',
      `${item.path}${item.line ? ':' + item.line : ''}`,
      item.statement,
      {
        source: item.provenance.kind,
        detail: item.content_excerpt.length > 600 ?
          `${item.content_excerpt.slice(0, 600)}\n…` : item.content_excerpt
      }
    )));
    target.appendChild(details);
  }
  verifications.forEach(item => {
    target.appendChild(logicSection(
      '验证结果',
      statusLabel(item.effective_status),
      item.command,
      {
        source: item.provenance.kind,
        detail: item.output_summary || item.install_output_summary || ''
      }
    ));
  });
  if (!requirements.length && !allClaims.length && !evidence.length) {
    target.appendChild(text('p',
      '当前 Hunk 尚未建立需求、判断和证据映射。导出结果会保留这个缺口。',
      'logic-empty'));
  }
}
function renderCITrust() {
  const target = byId('ciTrust');
  clear(target);
  (proof.ci_verifications || []).forEach(item => {
    const status = item.trusted ? 'VERIFIED' : 'CAPTURED / UNTRUSTED';
    target.append(text('span', status), text('span', item.trust_source),
      text('span', item.policy_hash));
  });
}
async function showGraph() {
  closeDrawer();
  document.querySelector('.workspace').classList.add('graph-active');
  byId('diffView').hidden = true;
  byId('graphView').hidden = false;
  byId('graphView').scrollTop = 0;
  byId('diffButton').classList.remove('active');
  byId('graphButton').classList.add('active');
  const target = byId('graphContent');
  clear(target);
  target.appendChild(text('p', '正在生成修改逻辑图…', 'empty-message'));
  try {
    graphCache = graphCache || await (await api('/api/graph')).json();
  } catch (error) {
    clear(target);
    target.appendChild(text(
      'p',
      `修改逻辑图载入失败：${error.message}`,
      'empty-message'
    ));
    return;
  }
  clear(target);
  const graph = graphCache;
  byId('graphSummary').textContent =
    `${(proof.decision_records || []).length} 个决策 · ${proof.claims.length} 个结论 · ${proof.evidence.length} 条证据 · ${proof.verifications.length} 项验证`;
  const typeMeta = {
    implementation_decision: ['修改决策', 'Agent 修改前记录的方案、依据、风险和备选选择。', 0],
    requirement: ['需求', '本次修改需要解决的问题或实现的目标。', 1],
    file_change: ['文件变更', '相对于基线发生新增、修改、删除或重命名的文件。', 2],
    hunk: ['代码块', 'Git Diff 中可以独立定位的一段修改。', 3],
    claim: ['变更结论', '这段修改产生的行为或实现效果。', 4],
    evidence: ['代码证据', '支撑结论的具体代码位置和上下文。', 5],
    verification: ['验证结果', '与修改关联的测试、检查或构建结果。', 6],
    ci_verification: ['CI 验证', '由可信 CI 环境返回并校验过的验证结果。', 7],
    risk: ['风险与缺口', '尚未验证、缺少映射或证据不完整的位置。', 8],
    requirement_revision: ['需求版本', '需求内容的修订历史。', 20],
    capture_event: ['采集事件', 'Agent 执行过程中捕获的原始操作事件。', 21],
    capture_event: ['采集事件', '结构化 Agent 轨迹事件。', 22],
    ledger_event: ['审计事件', '证据创建、关联和变更的不可变日志。', 23],
    git_snapshot_revision: ['Git 快照', '分析前后的仓库版本与工作区状态。', 24],
    revision: ['变更版本', '同一份修改逻辑链的历史版本。', 25],
    coverage_assertion: ['覆盖声明', '需求、代码和验证之间的覆盖关系。', 26],
    review_decision: ['评审决定', 'Reviewer 对修改或证据作出的确认与拒绝。', 27],
    publication: ['发布记录', '证据链发布到 PR 或外部系统的记录。', 28]
  };
  const reviewTypes = new Set([
    'implementation_decision', 'requirement', 'file_change', 'hunk',
    'claim', 'evidence', 'verification', 'ci_verification', 'risk'
  ]);
  const nodeCount = kind => graph.nodes.filter(node => node.node_type === kind).length;
  const overview = text('div', '', 'graph-overview');
  [
    ['需求', nodeCount('requirement'), '要解决什么'],
    ['修改决策', nodeCount('implementation_decision'), '为什么这样改'],
    ['代码', nodeCount('file_change'), `${nodeCount('hunk')} 个代码块`],
    ['代码证据', nodeCount('evidence'), '依据在哪里'],
    ['验证', nodeCount('verification') + nodeCount('ci_verification'), '改动是否正确']
  ].forEach(([label, value, help]) => {
    const step = text('div', '', 'graph-step');
    step.append(text('span', label), text('strong', value), text('small', help));
    overview.appendChild(step);
  });
  target.appendChild(overview);
  if (!proof.verifications.length && !(proof.ci_verifications || []).length) {
    target.appendChild(text(
      'div',
      '当前没有关联的测试或 CI 结果。逻辑链可以解释修改，但还不能证明修改正确。',
      'graph-alert'
    ));
  } else if (!(proof.decision_records || []).length) {
    target.appendChild(text(
      'div',
      '当前没有捕获到 Agent 的修改决策，部分解释来自系统事后推导。',
      'graph-alert'
    ));
  } else {
    target.appendChild(text(
      'div',
      '已捕获修改决策和验证信息，可以沿逻辑链检查实现依据。',
      'graph-alert success'
    ));
  }
  const toolbar = text('div', '', 'graph-toolbar');
  const modeSwitch = text('div', '', 'graph-mode');
  const reviewMode = button('审阅逻辑', () => {}, 'active');
  const auditMode = button('审计数据', () => {});
  modeSwitch.append(reviewMode, auditMode);
  const search = document.createElement('input');
  search.type = 'search';
  search.className = 'graph-search';
  search.placeholder = '搜索决策、文件、证据或验证';
  search.setAttribute('aria-label', '搜索逻辑链节点');
  const controls = text('div', '', 'graph-controls');
  const provenance = document.createElement('select');
  provenance.setAttribute('aria-label', '按来源筛选');
  const status = document.createElement('select');
  status.setAttribute('aria-label', '按状态筛选');
  [['', '全部来源'], ['captured','Agent 记录'], ['derived','系统推导'],
   ['verified','验证结果'], ['inferred','静态分析']].forEach(([value,label]) => {
    const option = text('option', label); option.value=value; provenance.appendChild(option);
  });
  [['','全部状态'],['accepted','已记录'],['confirmed','已确认'],
   ['rejected','已拒绝'],['proposed','待确认'],['passed','通过'],
   ['failed','失败'],['unreviewed','待评审'],['open','未处理']].forEach(([value,label]) => {
    const option = text('option', label); option.value=value; status.appendChild(option);
  });
  controls.append(provenance, status);
  toolbar.append(modeSwitch, search, controls);
  target.appendChild(toolbar);
  const resultSummary = text('p', '', 'graph-result-summary');
  target.appendChild(resultSummary);
  const grid = text('div', '', 'graph-groups'); target.appendChild(grid);
  let mode = 'review';
  const limits = new Map();
  const openKinds = new Set(['implementation_decision', 'requirement', 'verification']);
  const render = () => {
    clear(grid);
    const visible = graph.nodes.filter(node =>
      (mode === 'review' ? reviewTypes.has(node.node_type) : !reviewTypes.has(node.node_type)) &&
      (!provenance.value || node.provenance === provenance.value) &&
      (!status.value || node.status === status.value) &&
      (!search.value.trim() ||
        `${node.label} ${node.status} ${node.provenance}`
          .toLowerCase().includes(search.value.trim().toLowerCase())));
    resultSummary.textContent = mode === 'review' ?
      `显示 ${visible.length} 个审阅节点。采集日志和版本快照已收纳到“审计数据”。` :
      `显示 ${visible.length} 个审计节点。这些数据主要用于追溯和完整性检查。`;
    if (!visible.length) {
      grid.appendChild(text('p', '没有符合当前筛选条件的内容。', 'empty-message'));
      return;
    }
    const groups = new Map();
    visible.forEach(node => {
      if (!groups.has(node.node_type)) groups.set(node.node_type, []);
      groups.get(node.node_type).push(node);
    });
    [...groups.entries()]
      .sort(([left], [right]) =>
        (typeMeta[left]?.[2] ?? 50) - (typeMeta[right]?.[2] ?? 50))
      .forEach(([kind, nodes]) => {
        const meta = typeMeta[kind] || [kind, '证据链节点。', 50];
        const group = text('details', '', 'graph-group');
        group.open = search.value.trim() ? true : openKinds.has(kind);
        group.addEventListener('toggle', () => {
          if (group.open) openKinds.add(kind);
          else openKinds.delete(kind);
        });
        group.appendChild(text('summary', `${meta[0]} · ${nodes.length}`));
        group.appendChild(text('p', meta[1], 'graph-group-description'));
        const layer = text('div', '', 'graph-layer');
        const limit = limits.get(kind) || 36;
        nodes.slice(0, limit).forEach(item => layer.appendChild(button(
          `${graphNodeLabel(item.label)}\n${statusLabel(item.status)} · ${sourceLabel(item.provenance)}`,
          () => selectGraphNode(item, graph),
          `graph-node${item.incomplete ? ' incomplete' : ''}`)));
        group.appendChild(layer);
        if (nodes.length > limit) {
          group.appendChild(button(
            `再显示 ${Math.min(36, nodes.length - limit)} 项`,
            () => {
              openKinds.add(kind);
              limits.set(kind, limit + 36);
              render();
            },
            'graph-more'
          ));
        }
        grid.appendChild(group);
      });
  };
  const selectMode = value => {
    mode = value;
    reviewMode.classList.toggle('active', mode === 'review');
    auditMode.classList.toggle('active', mode === 'audit');
    render();
  };
  reviewMode.addEventListener('click', () => selectMode('review'));
  auditMode.addEventListener('click', () => selectMode('audit'));
  search.addEventListener('input', render);
  provenance.addEventListener('change', render);
  status.addEventListener('change', render);
  render();
}
function selectGraphNode(node, graph) {
  openDrawer();
  const target = byId('logic');
  clear(target);
  const hero = text('div', '', 'logic-hero');
  hero.append(text('strong', graphNodeLabel(node.label)), text('p',
    `${statusLabel(node.status)} · ${sourceLabel(node.provenance)}${node.stale ? ' · 已过期' : ''}`));
  target.appendChild(hero);
  if (node.details && Object.keys(node.details).length) {
    const detailLabel = {
      rationale: '判断依据',
      alternatives: '备选方案',
      affected_paths: '影响文件',
      risks: '风险',
      verification_plan: '验证计划',
      requirement_ids: '关联需求',
      hunk_ids: '关联代码块',
      file_change_ids: '关联文件',
      command: '执行命令',
      output_summary: '输出摘要',
      event_type: '事件类型',
      timestamp: '记录时间'
    };
    const details = logicSection('节点信息', '详细信息', '', {source: node.provenance});
    addKeyValues(details, Object.entries(node.details).map(([key, value]) => [
      detailLabel[key] || key.replaceAll('_', ' '),
      Array.isArray(value) ? value : typeof value === 'object' ?
        JSON.stringify(value) : String(value ?? '')
    ]));
    target.appendChild(details);
  }
  const relationLabel = {
    justifies: '支撑结论',
    supports: '提供证据',
    explains_change: '解释变更',
    contains: '包含',
    records: '记录',
    produced: '产生',
    covers: '覆盖',
    implemented_by: '由代码实现',
    previous_event: '前序事件',
    parent_of: '父级'
  };
  graph.edges.filter(edge => edge.source === node.id || edge.target === node.id)
    .forEach(edge => {
      const otherId = edge.source === node.id ? edge.target : edge.source;
      const other = graph.nodes.find(item => item.id === otherId);
      if (other) {
        const relation = logicSection(
          '关联节点',
          graphNodeLabel(other.label),
          `${relationLabel[edge.relation] || edge.relation} · ${statusLabel(other.status)}`,
          {source: other.provenance}
        );
        relation.appendChild(button(`查看${graphTypeLabel(other.node_type)}`,
          () => navigateGraphNode(other, graph), 'logic-toggle'));
        target.appendChild(relation);
      }
    });
}
async function navigateGraphNode(node, graph) {
  if (node.node_type === 'file_change') {
    showDiff();
    await selectFile(node.id);
    return;
  }
  if (node.node_type === 'hunk') {
    const hunk = proof.patch_hunks.find(item => item.id === node.id);
    if (hunk) {
      showDiff();
      await selectFile(hunk.file_change_id);
      const rowIndex = comparison.rows.findIndex(item =>
        item.hunk_ids.includes(hunk.id) && item.kind !== 'context');
      const changedRows = document.querySelectorAll('.diff-row');
      if (rowIndex >= 0 && changedRows[rowIndex]) {
        changedRows[rowIndex].scrollIntoView({block:'center'});
        selectChangedRow(rowIndex, changedRows[rowIndex]);
      }
      return;
    }
  }
  selectGraphNode(node, graph);
}
function showDiff() {
  document.querySelector('.workspace').classList.remove('graph-active');
  byId('graphView').hidden = true;
  byId('diffView').hidden = false;
  byId('graphButton').classList.remove('active');
  byId('diffButton').classList.add('active');
}
async function exportEvidence(format) {
  const formats = {
    markdown: ['markdown', 'change-proof.md'],
    html: ['html', 'change-proof.html'],
    svg: ['svg', 'evidence-graph.svg'],
    png: ['png', 'evidence-graph.png'],
    json: ['json', 'change-proof.json']
  };
  const [endpoint, filename] = formats[format];
  try {
    const response = await api(`/api/export/${endpoint}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url; anchor.download = filename;
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
    URL.revokeObjectURL(url);
    document.querySelector('.export-menu').open = false;
    toast(`已导出 ${filename}`);
  } catch (failure) {
    toast(`导出失败：${failure.message}`);
  }
}
const loadingFileIds = new Set();
const sleep = milliseconds => new Promise(resolve =>
  window.setTimeout(resolve, milliseconds));
function setLoadingMode(active) {
  document.querySelector('.topbar').classList.toggle('loading', active);
  byId('fileSearch').disabled = active;
  byId('toggleDrawer').disabled = active;
  byId('diffButton').disabled = active;
  byId('graphButton').disabled = active;
  document.querySelectorAll('[data-export]').forEach(item => {
    item.disabled = active;
  });
  byId('loadingProgress').hidden = !active;
  if (active) {
    byId('assurance').textContent = '分析中';
    byId('stale').hidden = true;
  }
}
function renderLoadingShell(message) {
  const target = byId('comparison');
  if (target.dataset.loadingMessage === message) return;
  target.dataset.loadingMessage = message;
  clear(target);
  const shell = text('div', '', 'loading-shell');
  const mark = text('div', '', 'loading-shell-mark');
  mark.append(text('i', ''), text('i', ''), text('i', ''));
  shell.append(mark, text('strong', message), text(
    'p',
    '页面已经就绪，完成分析的文件会逐步出现在左侧。'
  ));
  target.appendChild(shell);
}
function appendLoadingFile(item) {
  if (loadingFileIds.has(item.id)) return;
  loadingFileIds.add(item.id);
  const panel = document.querySelector('.file-panel');
  const followLatest = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 96;
  const label = fileLabel(item);
  const row = text('div', '', 'file-row loading-file-row');
  const kind = text(
    'span',
    String(item.change_type || '?').slice(0, 1).toUpperCase(),
    'file-kind'
  );
  const copy = text('span', '', 'file-copy');
  copy.append(text('span', label.name, 'file-name'));
  if (label.folder) copy.append(text('span', label.folder, 'file-folder'));
  row.append(kind, copy);
  byId('files').appendChild(row);
  if (followLatest) row.scrollIntoView({block: 'nearest'});
}
function renderLoadingState(snapshot) {
  const total = Number(snapshot.total || 0);
  const loaded = Number(snapshot.loaded || 0);
  byId('title').textContent = snapshot.message || '正在分析代码变更…';
  byId('summary').textContent = total ?
    `已载入 ${loaded}/${total} 个文件` : '正在准备文件列表';
  byId('loadingMessage').textContent = snapshot.message || '正在分析…';
  byId('loadingCount').textContent = total ? `${loaded}/${total}` : '';
  byId('fileCount').textContent = total ? `${loaded}/${total}` : String(loaded);
  const progress = byId('loadingBar');
  if (total) {
    progress.max = total;
    progress.value = loaded;
  } else {
    progress.removeAttribute('value');
  }
  (snapshot.files || []).forEach(appendLoadingFile);
  renderLoadingShell(snapshot.message || '正在分析代码变更…');
}
async function waitForReview() {
  setLoadingMode(true);
  while (true) {
    const snapshot = await (await api('/api/loading')).json();
    renderLoadingState(snapshot);
    if (snapshot.status === 'failed') {
      throw new Error(snapshot.error || snapshot.message);
    }
    if (snapshot.status === 'ready') return;
    await sleep(120);
  }
}
async function load() {
  await waitForReview();
  proof = await (await api('/api/proof')).json();
  graphCache = null;
  setLoadingMode(false);
  byId('title').textContent = proof.title;
  byId('summary').textContent =
    `${proof.file_changes.length} 个文件 · ${proof.patch_hunks.length} 个代码块 · ${(proof.decision_records || []).length} 个决策`;
  byId('assurance').textContent = ({
    high: '证据完整',
    medium: '部分完整',
    low: '证据不足',
    unrated: '未评级'
  })[proof.assurance.level] || proof.assurance.level;
  byId('stale').textContent = proof.stale ? '内容已变化' : '';
  byId('stale').hidden = !proof.stale;
  byId('fileCount').textContent = String(proof.file_changes.length);
  const additions = proof.patch_hunks.reduce((sum, item) => sum + item.added_lines, 0);
  const deletions = proof.patch_hunks.reduce((sum, item) => sum + item.deleted_lines, 0);
  byId('changeStats').textContent = `新增 ${additions} 行 · 删除 ${deletions} 行`;
  renderFiles();
  renderCITrust();
  if (proof.file_changes.length) await selectFile(
    proof.file_changes.some(item => item.id === selectedFileId) ?
      selectedFileId : proof.file_changes[0].id);
  else renderComparison();
  if (window.matchMedia('(max-width: 1360px)').matches) closeDrawer();
}
byId('fileSearch').addEventListener('input', event => renderFiles(event.target.value));
byId('toggleDrawer').addEventListener('click', openDrawer);
byId('closeDrawer').addEventListener('click', closeDrawer);
byId('diffButton').addEventListener('click', showDiff);
byId('graphButton').addEventListener('click', showGraph);
document.querySelectorAll('[data-export]').forEach(item =>
  item.addEventListener('click', () => exportEvidence(item.dataset.export)));
load().catch(failure => {
  setLoadingMode(false);
  byId('title').textContent = '无法载入代码变更';
  byId('summary').textContent = failure.message;
  byId('assurance').textContent = '加载失败';
  byId('fileSearch').disabled = true;
  byId('comparison').replaceChildren(text('p', failure.message, 'empty-message'));
});
"""
