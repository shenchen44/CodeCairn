from __future__ import annotations

import hashlib
import re

from app.services.agent_runtime import normalize_task
from app.services.orchestration.contracts import (
    ClaimRecord,
    EvidenceGate,
    EvidenceLedger,
    EvidenceRecord,
    PatchHunkRecord,
    RequirementRecord,
    VerificationRecord,
)


DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _requirements(issue_context: dict) -> list[RequirementRecord]:
    task = normalize_task(issue_context)
    primary = task.objective
    requirements = [
        RequirementRecord(
            id=_stable_id("req", primary),
            text=primary,
            source="task_objective",
        )
    ]
    explicit = [*task.requirements, *task.acceptance_criteria]
    for text in explicit:
        normalized = " ".join(str(text).split())[:800]
        if (
            normalized
            and normalized.lower() not in {
                item.text.lower() for item in requirements
            }
        ):
            requirements.append(
                RequirementRecord(
                    id=_stable_id("req", normalized),
                    text=normalized,
                    source="task_contract",
                )
            )
    return requirements


def _evidence_records(localization: dict | None) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    for item in (localization or {}).get("evidence", []):
        if not isinstance(item, dict) or not item.get("path"):
            continue
        identity = (
            f"{item.get('path')}:{item.get('line')}:{item.get('symbol')}:"
            f"{item.get('reason')}"
        )
        records.append(
            EvidenceRecord(
                id=_stable_id("ev", identity),
                path=str(item["path"]),
                line=item.get("line"),
                symbol=item.get("symbol"),
                reason=str(item.get("reason") or "repository evidence"),
                content_sha256=item.get("content_sha256"),
            )
        )
    return records


def _patch_hunks(
    diff_text: str,
    requirement_ids: list[str],
    claim_ids: list[str],
) -> list[PatchHunkRecord]:
    hunks: list[PatchHunkRecord] = []
    current_path = ""
    current_header = ""
    added = 0
    deleted = 0

    def flush() -> None:
        nonlocal current_header, added, deleted
        if not current_path or (not current_header and added == 0 and deleted == 0):
            return
        identity = f"{current_path}:{current_header}:{len(hunks)}"
        hunks.append(
            PatchHunkRecord(
                id=_stable_id("hunk", identity),
                path=current_path,
                header=current_header or "file-level change",
                added_lines=added,
                deleted_lines=deleted,
                requirement_ids=requirement_ids,
                claim_ids=claim_ids,
            )
        )
        current_header = ""
        added = 0
        deleted = 0

    for line in diff_text.splitlines():
        file_match = DIFF_FILE_RE.match(line)
        if file_match:
            flush()
            current_path = file_match.group(2)
            continue
        if line.startswith("@@"):
            flush()
            current_header = line
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    flush()
    return hunks


def evaluate_evidence_ledger(
    ledger: EvidenceLedger,
    *,
    require_verification: bool = False,
) -> EvidenceGate:
    requirement_ids = {item.id for item in ledger.requirements}
    covered_requirements = {
        requirement_id
        for hunk in ledger.patch_hunks
        for requirement_id in hunk.requirement_ids
    }
    claimed_hunks = [
        hunk for hunk in ledger.patch_hunks if hunk.claim_ids
    ]
    verified_requirements = {
        requirement_id
        for verification in ledger.verifications
        if verification.status == "passed"
        for requirement_id in verification.requirement_ids
    }
    reasons: list[str] = []
    if covered_requirements != requirement_ids:
        reasons.append("requirements_without_patch_coverage")
    if len(claimed_hunks) != len(ledger.patch_hunks):
        reasons.append("patch_hunks_without_claims")
    if ledger.require_grounded_evidence and any(
        not claim.evidence_ids for claim in ledger.claims
    ):
        reasons.append("claims_without_repository_evidence")
    if require_verification and verified_requirements != requirement_ids:
        reasons.append("requirements_without_passing_verification")

    total_requirements = max(len(requirement_ids), 1)
    total_hunks = max(len(ledger.patch_hunks), 1)
    return EvidenceGate(
        passed=not reasons,
        stage="verification" if require_verification else "patch",
        reasons=reasons,
        requirement_coverage=len(
            covered_requirements & requirement_ids
        ) / total_requirements,
        patch_claim_coverage=len(claimed_hunks) / total_hunks,
        verification_coverage=len(
            verified_requirements & requirement_ids
        ) / total_requirements,
    )


def build_evidence_ledger(
    *,
    issue_context: dict,
    localization: dict | None,
    summary: dict | None,
    diff_text: str,
    require_grounded_evidence: bool,
) -> EvidenceLedger:
    requirements = _requirements(issue_context)
    evidence = _evidence_records(localization)
    root_cause = str(
        (localization or {}).get("root_cause_hypothesis")
        or (summary or {}).get("root_cause")
        or "The implementation does not satisfy the task requirement."
    )
    claim = ClaimRecord(
        id=_stable_id("claim", root_cause),
        statement=root_cause,
        requirement_ids=[item.id for item in requirements],
        evidence_ids=[item.id for item in evidence],
    )
    ledger = EvidenceLedger(
        requirements=requirements,
        evidence=evidence,
        claims=[claim],
        patch_hunks=_patch_hunks(
            diff_text,
            [item.id for item in requirements],
            [claim.id],
        ),
        require_grounded_evidence=require_grounded_evidence,
    )
    ledger.gate = evaluate_evidence_ledger(ledger)
    return ledger


def attach_verification(
    ledger_payload: dict | None,
    *,
    command: str,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
) -> dict | None:
    if not ledger_payload:
        return None
    ledger = EvidenceLedger.model_validate(ledger_payload)
    requirement_ids = [item.id for item in ledger.requirements]
    verification = VerificationRecord(
        id=_stable_id(
            "verify",
            f"{command}:{exit_code}:{len(ledger.verifications)}",
        ),
        command=command,
        status="passed" if exit_code == 0 else "failed",
        requirement_ids=requirement_ids,
        exit_code=exit_code,
        output_tail=(stderr or stdout)[-2000:],
    )
    ledger.verifications.append(verification)
    ledger.gate = evaluate_evidence_ledger(
        ledger,
        require_verification=True,
    )
    return ledger.model_dump(mode="json")
