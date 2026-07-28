from __future__ import annotations

import hashlib
import html
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from codecairn.review.models import ChangeProof
from codecairn.review.ledger import event_hash as ledger_event_hash


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    node_type: str
    label: str
    status: str = ""
    stale: bool = False
    confidence: float = 1.0
    provenance: str = "unknown"
    details: dict[str, Any] = Field(default_factory=dict)
    x: int = 0
    y: int = 0
    incomplete: bool = False


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    target: str
    relation: str
    status: str = ""
    stale: bool = False
    confidence: float = 1.0
    provenance: str = "unknown"


class EvidenceGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proof_id: str
    semantic_hash: str
    assurance: str
    gate_status: str
    gate_reasons: list[str]
    revision_number: int
    ledger_integrity: bool
    last_event_hash: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]


def _edge_id(source: str, relation: str, target: str) -> str:
    digest = hashlib.sha256(
        f"{source}\0{relation}\0{target}".encode()
    ).hexdigest()[:16]
    return f"edge_{digest}"


def build_evidence_graph(proof: ChangeProof) -> EvidenceGraph:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    def add_node(
        item_id: str,
        node_type: str,
        label: str,
        *,
        status: str = "",
        stale: bool = False,
        provenance: str = "unknown",
        confidence: float = 1.0,
        details: dict[str, Any] | None = None,
        incomplete: bool = False,
    ) -> None:
        nodes.append(
            GraphNode(
                id=item_id,
                node_type=node_type,
                label=label,
                status=status,
                stale=stale,
                provenance=provenance,
                confidence=confidence,
                details=details or {},
                incomplete=incomplete,
            )
        )

    def add_edge(
        source: str,
        target: str,
        relation: str,
        *,
        status: str = "",
        stale: bool = False,
        provenance: str = "unknown",
        confidence: float = 1.0,
    ) -> None:
        edges.append(
            GraphEdge(
                id=_edge_id(source, relation, target),
                source=source,
                target=target,
                relation=relation,
                status=status,
                stale=stale,
                provenance=provenance,
                confidence=confidence,
            )
        )

    for item in proof.requirements:
        add_node(
            item.id,
            "requirement",
            item.text,
            status="deleted" if item.deleted else "active",
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
            details={"category": item.category, "revision": item.revision},
            incomplete=item.deleted,
        )
    for item in proof.file_changes:
        add_node(
            item.id,
            "file_change",
            f"{item.change_type}: {item.path}",
            status="reviewed" if item.reviewed else "unreviewed",
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
            incomplete=not item.reviewed,
        )
    for item in proof.patch_hunks:
        add_node(
            item.id,
            "hunk",
            f"{item.path} {item.header}",
            status="reviewed" if item.reviewed else "unreviewed",
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
            incomplete=not item.reviewed,
        )
    for item in proof.claims:
        add_node(
            item.id,
            "claim",
            item.statement,
            status=item.status,
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
            incomplete=item.status != "confirmed",
        )
    for item in proof.evidence:
        add_node(
            item.id,
            "evidence",
            f"{item.path}:{item.line or ''}",
            status="stale" if item.stale else "current",
            stale=item.stale,
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
            incomplete=item.stale,
        )
    for item in proof.verifications:
        add_node(
            item.id,
            "verification",
            " ".join(item.command_argv) or item.command,
            status=item.effective_status,
            stale=item.effective_status == "stale",
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
            incomplete=item.effective_status != "passed",
        )
    for item in proof.coverage_assertions:
        add_node(
            item.id,
            "coverage_assertion",
            f"{item.target_type}:{item.target_id}",
            status=item.status,
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
            incomplete=item.status != "confirmed",
        )
    for item in proof.risks:
        add_node(
            item.id,
            "risk",
            item.statement,
            status=item.status,
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
            incomplete=item.status == "open",
        )
    capture_by_id = {
        item.event_id: item for item in proof.capture_events
    }
    previous_capture_id: str | None = None
    for item in proof.capture_events:
        missing_parent = bool(
            item.parent_event_id
            and item.parent_event_id not in capture_by_id
        )
        add_node(
            item.event_id,
            "capture_event",
            f"{item.host}: {item.event_type}",
            status=item.integrity_status,
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
            incomplete=missing_parent or item.integrity_status != "valid",
            details={
                "sequence": item.sequence,
                "session_id": item.session_id,
                "payload_hash": item.payload_hash,
            },
        )
        if previous_capture_id:
            add_edge(
                previous_capture_id,
                item.event_id,
                "previous_event",
                status=item.integrity_status,
                provenance="captured",
            )
        if item.parent_event_id:
            add_edge(
                item.parent_event_id,
                item.event_id,
                "parent_of",
                status="incomplete" if missing_parent else "complete",
                provenance="captured",
            )
        previous_capture_id = item.event_id
    for decision in proof.decision_records:
        add_node(
            decision.decision_id,
            "implementation_decision",
            decision.summary,
            status=decision.status,
            provenance="captured",
            incomplete=decision.status != "accepted",
            details={
                "rationale": decision.rationale,
                "alternatives": decision.alternatives,
                "affected_paths": decision.affected_paths,
                "risks": decision.risks,
                "verification_plan": decision.verification_plan,
            },
        )
        source_events = [
            event
            for event in proof.capture_events
            if isinstance(event.payload.get("decision"), dict)
            and event.payload["decision"].get("decision_id")
            == decision.decision_id
        ]
        for event in source_events:
            add_edge(
                event.event_id,
                decision.decision_id,
                "records",
                provenance="captured",
            )
            for claim in proof.claims:
                if event.event_id in claim.provenance.source_event_ids:
                    add_edge(
                        decision.decision_id,
                        claim.id,
                        "justifies",
                        provenance="captured",
                    )
    previous_ledger_hash = ""
    previous_ledger_id: str | None = None
    for item in proof.audit_events:
        broken = (
            item.previous_event_hash != previous_ledger_hash
            or item.event_hash != ledger_event_hash(item)
        )
        add_node(
            item.event_id,
            "ledger_event",
            item.event_type,
            status=item.actor_type,
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
            incomplete=broken,
            details={"sequence": item.sequence},
        )
        if previous_ledger_id:
            add_edge(
                previous_ledger_id,
                item.event_id,
                "previous_event",
                status="broken" if broken else "valid",
                provenance="derived",
            )
        previous_ledger_hash = item.event_hash
        previous_ledger_id = item.event_id
    for item in proof.review_decisions:
        add_node(
            item.id,
            "review_decision",
            f"{item.decision}: {item.target_type}",
            status=item.decision,
            provenance="captured",
            details={"target_id": item.target_id, "reviewer": item.reviewer},
        )
        add_edge(
            item.id,
            item.target_id,
            item.decision,
            status=item.decision,
            provenance="captured",
        )
        add_edge(
            item.source_event_id,
            item.id,
            "records",
            provenance="captured",
        )
    for item in proof.requirement_revisions:
        node_id = f"requirement_revision:{item.requirement_id}:{item.revision}"
        add_node(
            node_id,
            "requirement_revision",
            f"r{item.revision}: {item.text}",
            status="deleted" if item.deleted else "active",
            provenance="captured",
        )
        add_edge(node_id, item.requirement_id, "revises", provenance="captured")
        if item.source_event_id:
            add_edge(
                item.source_event_id, node_id, "records", provenance="captured"
            )
    for item in proof.git_snapshot_revisions:
        add_node(
            item.revision_id,
            "git_snapshot_revision",
            f"{item.transition}: {item.head_sha[:12]}",
            status=item.transition,
            provenance="derived",
            details={
                "git_snapshot_id": item.git_snapshot_id,
                "patch_fingerprint": item.patch_fingerprint,
            },
        )
        add_edge(
            item.revision_id,
            f"revision:{proof.change_id}",
            "packages",
            provenance="derived",
        )
    for item in proof.publications:
        add_node(
            item.id,
            "publication",
            f"github {item.target} #{item.pr_number}",
            status="published",
            provenance=item.provenance.kind,
            details={"remote_id": item.remote_id, "url": item.url},
        )
        add_edge(
            item.id,
            f"revision:{proof.change_id}",
            "publishes",
            provenance=item.provenance.kind,
        )
    for item in proof.ci_verifications:
        add_node(
            item.run_id,
            "ci_verification",
            f"{item.provider}: {' '.join(item.command_argv)}",
            status=item.result if item.trusted else "captured",
            provenance=item.provenance.kind,
            incomplete=not item.trusted,
            details={
                "trust_reason": item.trust_reason,
                "trust_source": item.trust_source,
                "policy_hash": item.policy_hash,
                "artifact_id": item.artifact_id,
                "artifact_digest": item.artifact_digest,
            },
        )
        add_edge(
            item.run_id,
            f"revision:{proof.change_id}",
            "verifies_patch_fingerprint",
            status=item.result,
            provenance=item.provenance.kind,
        )
        verification_id = (
            f"verification_{item.provider}_{item.run_id}_{item.run_attempt}"
        )
        if any(
            verification.id == verification_id
            for verification in proof.verifications
        ):
            add_edge(
                item.run_id,
                verification_id,
                "produced_verification",
                status=item.result,
                provenance=item.provenance.kind,
            )
    for item in proof.ci_attestations:
        add_node(
            item.attestation_id,
            "ci_attestation",
            item.issuer,
            status="captured",
            provenance=item.provenance.kind,
            details={
                "artifact_id": item.artifact_id,
                "artifact_digest": item.artifact_digest,
                "observation_digest": item.observation_digest,
            },
        )
        add_edge(
            item.attestation_id,
            item.run_id,
            "attests_observation",
            provenance=item.provenance.kind,
        )
    revision_id = f"revision:{proof.change_id}"
    add_node(
        revision_id,
        "revision",
        f"Revision {proof.revision_number}",
        status=proof.gate.status,
        provenance="derived",
        incomplete=proof.gate.status != "passed",
    )
    if proof.parent_change_id:
        parent_id = f"revision:{proof.parent_change_id}"
        add_node(
            parent_id,
            "revision",
            "Parent revision",
            status="historical",
            provenance="derived",
        )
        add_edge(parent_id, revision_id, "parent_of", provenance="derived")

    for item in proof.mappings:
        add_edge(
            item.from_id,
            item.to_id,
            item.relation,
            status="confirmed" if item.confirmed else "unconfirmed",
            provenance=item.provenance.kind,
            confidence=item.provenance.confidence,
        )
    for hunk in proof.patch_hunks:
        add_edge(
            hunk.file_change_id,
            hunk.id,
            "contains",
            provenance="derived",
        )
        for claim_id in hunk.claim_ids:
            add_edge(hunk.id, claim_id, "supports", provenance="derived")
    for claim in proof.claims:
        for evidence_id in claim.evidence_ids:
            evidence = next(
                (item for item in proof.evidence if item.id == evidence_id),
                None,
            )
            add_edge(
                evidence_id,
                claim.id,
                "supports",
                stale=bool(evidence and evidence.stale),
                provenance="derived",
            )
    for assertion in proof.coverage_assertions:
        add_edge(
            assertion.verification_id,
            assertion.id,
            "produced",
            status=assertion.status,
            provenance="captured",
        )
        add_edge(
            assertion.id,
            assertion.target_id,
            "covers",
            status=assertion.status,
            provenance=assertion.provenance.kind,
        )
    for risk in proof.risks:
        for target in risk.related_ids:
            add_edge(
                risk.id,
                target,
                "relates_to",
                status=risk.status,
                provenance=risk.provenance.kind,
            )
    for capture in proof.capture_events:
        for path in capture.payload.get("affected_paths", []):
            for change in proof.file_changes:
                if path in {change.path, change.old_path}:
                    add_edge(
                        capture.event_id,
                        change.id,
                        "observed_or_produced",
                        provenance="captured",
                    )
    for event in proof.audit_events:
        capture_id = event.payload.get("capture_event_id")
        if capture_id in capture_by_id:
            add_edge(
                event.event_id,
                capture_id,
                "recorded",
                provenance="captured",
            )
        for key in (
            "publication_id",
            "run_id",
            "verification_id",
            "decision_id",
        ):
            target_id = event.payload.get(key)
            if target_id and any(node.id == target_id for node in nodes):
                add_edge(
                    event.event_id,
                    str(target_id),
                    "records",
                    provenance=event.provenance.kind,
                )

    layers = {
        name: index
        for index, name in enumerate(
            [
                "requirement",
                "requirement_revision",
                "file_change",
                "hunk",
                "claim",
                "evidence",
                "verification",
                "coverage_assertion",
                "risk",
                "capture_event",
                "capture_event",
                "implementation_decision",
                "ledger_event",
                "review_decision",
                "git_snapshot_revision",
                "publication",
                "ci_verification",
                "revision",
            ]
        )
    }
    nodes.sort(key=lambda item: (layers.get(item.node_type, 99), item.id))
    counts: dict[str, int] = {}
    for node in nodes:
        offset = counts.get(node.node_type, 0)
        node.x = 30 + layers.get(node.node_type, 99) * 220
        node.y = 120 + offset * 90
        counts[node.node_type] = offset + 1
    edges.sort(key=lambda item: (item.source, item.relation, item.target))
    semantic_payload = {
        "proof_id": proof.change_id,
        "assurance": proof.assurance.level,
        "gate_status": proof.gate.status,
        "gate_reasons": proof.gate.reasons,
        "revision_number": proof.revision_number,
        "ledger_integrity": proof.ledger_integrity,
        "last_event_hash": proof.last_event_hash,
        "nodes": [item.model_dump(mode="json") for item in nodes],
        "edges": [item.model_dump(mode="json") for item in edges],
    }
    semantic_hash = hashlib.sha256(
        json.dumps(
            semantic_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return EvidenceGraph(
        proof_id=proof.change_id,
        semantic_hash=semantic_hash,
        assurance=proof.assurance.level,
        gate_status=proof.gate.status,
        gate_reasons=list(proof.gate.reasons),
        revision_number=proof.revision_number,
        ledger_integrity=proof.ledger_integrity,
        last_event_hash=proof.last_event_hash,
        nodes=nodes,
        edges=edges,
    )


def graph_svg(graph: EvidenceGraph) -> str:
    by_id = {item.id: item for item in graph.nodes}
    width = max((item.x for item in graph.nodes), default=0) + 240
    height = max((item.y for item in graph.nodes), default=0) + 90
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">'
        ),
        "<style>text{font:12px sans-serif}.node{fill:#fff;stroke:#667085}"
        ".incomplete{fill:#fff3cd;stroke:#a15c00}.edge{stroke:#98a2b3}"
        "</style>",
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        f'<text x="20" y="24">Assurance: {html.escape(graph.assurance)} · '
        f'Gate: {html.escape(graph.gate_status)} · '
        f'Revision: {graph.revision_number}</text>',
        f'<text x="20" y="46">Ledger integrity: '
        f'{str(graph.ledger_integrity).lower()} · '
        f'Last hash: {html.escape(graph.last_event_hash[:32])}</text>',
        '<text x="20" y="68">Provenance: captured · derived · verified · '
        'inferred · unknown</text>',
        f'<text x="20" y="90">Gate reasons: '
        f'{html.escape(", ".join(graph.gate_reasons)[:200])}</text>',
    ]
    for edge in graph.edges:
        source, target = by_id.get(edge.source), by_id.get(edge.target)
        if source and target:
            parts.append(
                f'<line class="edge" x1="{source.x + 180}" '
                f'y1="{source.y + 25}" x2="{target.x}" '
                f'y2="{target.y + 25}"><title>'
                f"{html.escape(edge.relation)}</title></line>"
            )
    for node in graph.nodes:
        css = "node incomplete" if node.incomplete else "node"
        label = html.escape(node.label[:80])
        parts.extend(
            [
                f'<g id="{html.escape(node.id, quote=True)}">',
                f'<rect class="{css}" x="{node.x}" y="{node.y}" '
                'width="180" height="50" rx="6"/>',
                f'<text x="{node.x + 8}" y="{node.y + 19}">'
                f"{html.escape(node.node_type)}</text>",
                f'<text x="{node.x + 8}" y="{node.y + 38}">{label}</text>',
                "</g>",
            ]
        )
    parts.append("</svg>")
    return "".join(parts)


def graph_html(proof: ChangeProof, graph: EvidenceGraph) -> str:
    reasons = "".join(
        f"<li>{html.escape(reason)}</li>" for reason in proof.gate.reasons
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"Content-Security-Policy\" "
        "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
        "<title>CodeCairn Change Proof</title></head><body>"
        f"<h1>{html.escape(proof.title)}</h1>"
        f"<p>Assurance: {html.escape(proof.assurance.level)} · "
        f"Gate: {html.escape(proof.gate.status)} · "
        f"Revision: {proof.revision_number}</p>"
        f"<p>Ledger integrity: {str(proof.ledger_integrity).lower()} · "
        f"Last hash: <code>{html.escape(proof.last_event_hash)}</code></p>"
        f"<p>Graph semantic hash: <code>{graph.semantic_hash}</code></p>"
        f"<ul>{reasons}</ul>"
        "<p>Provenance: captured · derived · verified · inferred · unknown</p>"
        f"{graph_svg(graph)}</body></html>"
    )


class ExportRenderError(RuntimeError):
    pass


def graph_png(graph: EvidenceGraph) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise ExportRenderError("png_renderer_unavailable:pillow") from exc
    from io import BytesIO

    by_id = {item.id: item for item in graph.nodes}
    width = max((item.x for item in graph.nodes), default=0) + 240
    height = max((item.y for item in graph.nodes), default=0) + 90
    canvas = Image.new("RGB", (max(width, 320), max(height, 160)), "#f8fafc")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()

    def safe_text(value: str) -> str:
        # Pillow's built-in font may not contain CJK; replacement is stable.
        try:
            font.getbbox(value)
            return value
        except (UnicodeEncodeError, ValueError):
            return value.encode("ascii", errors="replace").decode("ascii")

    draw.text(
        (20, 18),
        safe_text(
            f"Assurance: {graph.assurance} | Gate: {graph.gate_status} | "
            f"Revision: {graph.revision_number}"
        ),
        fill="#101828",
        font=font,
    )
    draw.text(
        (20, 40),
        safe_text(
            f"Ledger integrity: {str(graph.ledger_integrity).lower()} | "
            f"Last hash: {graph.last_event_hash[:32]}"
        ),
        fill="#344054",
        font=font,
    )
    for edge in graph.edges:
        source, target = by_id.get(edge.source), by_id.get(edge.target)
        if source and target:
            draw.line(
                (source.x + 180, source.y + 25, target.x, target.y + 25),
                fill="#98a2b3",
                width=1,
            )
    for node in graph.nodes:
        fill = "#fff3cd" if node.incomplete else "#ffffff"
        outline = "#a15c00" if node.incomplete else "#667085"
        draw.rounded_rectangle(
            (node.x, node.y, node.x + 180, node.y + 50),
            radius=6,
            fill=fill,
            outline=outline,
            width=1,
        )
        draw.text(
            (node.x + 8, node.y + 8),
            safe_text(node.node_type[:28]),
            fill="#101828",
            font=font,
        )
        draw.text(
            (node.x + 8, node.y + 28),
            safe_text(node.label[:32]),
            fill="#344054",
            font=font,
        )
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
