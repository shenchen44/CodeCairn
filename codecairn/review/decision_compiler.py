from __future__ import annotations

import hashlib
from pathlib import Path

from codecairn.review.models import (
    ChangeProof,
    Claim,
    DecisionRecord,
    Evidence,
    Mapping,
    Provenance,
    CaptureEvent,
)


def _id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _safe_decision(
    event: CaptureEvent, repo: Path
) -> DecisionRecord | None:
    if event.event_type != "decision_recorded":
        return None
    raw = event.payload.get("decision")
    if not isinstance(raw, dict):
        return None
    decision = DecisionRecord.model_validate(raw)
    root = repo.resolve()
    paths = decision.affected_paths + [
        reference.path for reference in decision.evidence
    ]
    for path in paths:
        try:
            (root / path).resolve().relative_to(root)
        except ValueError:
            return None
    return decision


def compile_decision_events(
    proof: ChangeProof,
    events: list[CaptureEvent],
    *,
    repo: Path,
) -> int:
    """Materialize accepted pre-mutation decisions into the Change Proof."""
    known_events = {item.event_id for item in proof.capture_events}
    known_decisions = {item.decision_id for item in proof.decision_records}
    known_claims = {item.id for item in proof.claims}
    known_evidence = {item.id for item in proof.evidence}
    known_mappings = {item.id for item in proof.mappings}
    imported = 0

    for event in events:
        if event.event_id not in known_events:
            proof.capture_events.append(event.model_copy(deep=True))
            known_events.add(event.event_id)
            imported += 1
        decision = _safe_decision(event, repo)
        if decision is None or decision.status != "accepted":
            continue
        if decision.decision_id not in known_decisions:
            proof.decision_records.append(decision)
            known_decisions.add(decision.decision_id)

        source_ids = [event.event_id]
        source_ids.extend(
            ref.source_event_id
            for ref in decision.evidence
            if ref.source_event_id
        )
        provenance = Provenance(
            kind="captured",
            source=event.host,
            source_event_ids=sorted(set(source_ids)),
            model=event.provenance.model,
            confidence=1.0,
        )
        evidence_ids: list[str] = []
        for reference in decision.evidence:
            evidence_id = _id(
                "evidence",
                decision.decision_id,
                reference.path,
                str(reference.line or ""),
                reference.statement,
            )
            evidence_ids.append(evidence_id)
            if evidence_id in known_evidence:
                continue
            proof.evidence.append(
                Evidence(
                    id=evidence_id,
                    path=reference.path,
                    line=reference.line,
                    symbol=reference.symbol,
                    statement=reference.statement,
                    provenance=provenance,
                )
            )
            known_evidence.add(evidence_id)

        claim_id = _id("claim", decision.decision_id)
        if claim_id not in known_claims:
            proof.claims.append(
                Claim(
                    id=claim_id,
                    statement=(
                        f"{decision.summary}: {decision.rationale}"
                    ),
                    evidence_ids=evidence_ids,
                    provenance=provenance,
                )
            )
            known_claims.add(claim_id)

        for hunk in proof.patch_hunks:
            if hunk.path not in decision.affected_paths:
                continue
            if claim_id not in hunk.claim_ids:
                hunk.claim_ids.append(claim_id)
            mapping_id = _id(
                "mapping", decision.decision_id, claim_id, hunk.id
            )
            if mapping_id in known_mappings:
                continue
            proof.mappings.append(
                Mapping(
                    id=mapping_id,
                    from_id=claim_id,
                    to_id=hunk.id,
                    relation="explains_change",
                    explanation=decision.rationale,
                    provenance=provenance,
                )
            )
            known_mappings.add(mapping_id)
    return imported
