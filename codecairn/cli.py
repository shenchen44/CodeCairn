from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import uvicorn

from codecairn import __version__
from codecairn.repository import resolve_repository
from codecairn.doctor import run_doctor
from codecairn.review.server import (
    ProgressiveReviewState,
    ReviewState,
    create_review_app,
    load_or_create_review_state,
    proof_markdown,
)
from codecairn.review.store import ReviewStorageError, find_review_for_patch
from codecairn.review.analyzer import build_change_proof, canonical_change_identity
from codecairn.review.models import (
    CIArtifactDownload,
    CIAttestation,
    CIManifest,
    CIVerificationResult,
    CoverageAssertion,
    Publication,
    Provenance,
    Verification,
)
from codecairn.review.ci import (
    CIVerificationError,
    assess_attested_observation,
    assess_ci_result,
    ci_result_identity,
    ci_results_collide,
    load_ci_artifact_package,
    load_ci_result_artifact,
    load_key,
    run_ci_manifest,
)
from codecairn.review.trust_policy import (
    TrustPolicyError,
    load_trust_policy_from_base,
)
from codecairn.github.publishing import (
    CHANGE_PROOF_START,
    GitHubIntegrationError,
    GitHubPublicationService,
    check_conclusion,
    replace_marker,
    repository_slug,
    resolve_github_credential,
    validate_pr_binding,
)
from codecairn.review.capture import (
    CaptureStore,
    capture_path,
    event_from_payload,
)
from codecairn.review.graph import (
    ExportRenderError,
    build_evidence_graph,
    graph_html,
    graph_png,
    graph_svg,
)


def _available_port(host: str, requested: int) -> int:
    if requested:
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind((host, 0))
        return int(candidate.getsockname()[1])


def _review(args: argparse.Namespace) -> int:
    repo_path = resolve_repository(Path(args.repo))
    if args.format == "web":
        host = "127.0.0.1"
        port = _available_port(host, args.port)
        token = secrets.token_urlsafe(32)
        url = f"http://{host}:{port}/?token={token}"
        progressive = ProgressiveReviewState(repo_path)

        def load_review() -> None:
            try:
                materialized = load_or_create_review_state(
                    repo_path,
                    base_ref=args.base,
                    requirement_texts=args.requirement,
                    progress_callback=progressive.update,
                )
                progressive.complete(materialized)
            except Exception as exc:
                progressive.fail(exc)

        threading.Thread(target=load_review, daemon=True).start()
        print(
            f"CodeCairn review: {url}\n"
            f"Repository: {repo_path}\n"
            f"Base: {args.base or 'auto'} (loading)",
            flush=True,
        )
        if not args.no_open:
            threading.Timer(0.25, webbrowser.open, args=(url,)).start()
        uvicorn.run(
            create_review_app(
                progressive,
                session_token=token,
                allowed_hosts={host, "localhost"},
            ),
            host=host,
            port=port,
            log_level="warning",
        )
        return 0

    try:
        state = load_or_create_review_state(
            repo_path,
            base_ref=args.base,
            requirement_texts=args.requirement,
        )
    except ReviewStorageError as exc:
        print(f"CodeCairn review state error: {exc}", file=sys.stderr)
        return 2
    proof = state.proof
    if args.format == "json":
        print(proof.model_dump_json(indent=2))
        return 0
    if args.format == "markdown":
        print(proof_markdown(proof))
        return 0
    if args.format in {"html", "svg", "png"}:
        graph = build_evidence_graph(proof)
        try:
            payload: str | bytes = (
                graph_html(proof, graph)
                if args.format == "html"
                else graph_svg(graph)
                if args.format == "svg"
                else graph_png(graph)
            )
        except ExportRenderError as exc:
            print(f"CodeCairn export error: {exc}", file=sys.stderr)
            return 2
        if args.output:
            output = Path(args.output)
            if isinstance(payload, bytes):
                output.write_bytes(payload)
            else:
                output.write_text(payload, encoding="utf-8")
        elif isinstance(payload, bytes):
            sys.stdout.buffer.write(payload)
        else:
            print(payload)
        return 0
    raise AssertionError(f"unsupported review format: {args.format}")


def _capture(args: argparse.Namespace) -> int:
    if args.capture_command == "ingest":
        try:
            repo_path = resolve_repository(Path(args.repo))
            payload = json.load(sys.stdin)
            event = event_from_payload(
                payload,
                repo=repo_path,
                host=args.host,
            )
            created = CaptureStore(
                capture_path(repo_path)
            ).append(event)
            print(
                json.dumps(
                    {
                        "event_id": event.event_id,
                        "created": created,
                        "schema_version": event.schema_version,
                    }
                )
            )
            return 0
        except Exception as exc:
            print(
                f"CodeCairn capture ingest error: {exc}",
                file=sys.stderr,
            )
            return 2
    if args.capture_command in {"sessions", "show"}:
        repo_path = resolve_repository(Path(args.repo))
        sessions = CaptureStore(
            capture_path(repo_path)
        ).sessions()
        if args.capture_command == "sessions":
            print(
                json.dumps(
                    [
                        {
                            "session_id": session_id,
                            "event_count": len(events),
                            "host": events[-1].host,
                            "last_timestamp": (
                                events[-1].timestamp.isoformat()
                            ),
                        }
                        for session_id, events in sorted(sessions.items())
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        events = sessions.get(args.session_id, [])
        print(
            json.dumps(
                [item.model_dump(mode="json") for item in events],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if events else 1
    if args.capture_command == "replay":
        spool = (
            Path(args.file).expanduser()
            if args.file
            else Path.home()
            / ".codecairn"
            / "spool"
            / "pi"
            / "events.jsonl"
        )
        if not spool.exists():
            print(json.dumps({"replayed": 0, "pending": 0}))
            return 0
        records = [
            json.loads(line)
            for line in spool.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        pending: list[dict] = []
        replayed = 0
        for record in records:
            try:
                repo_path = resolve_repository(
                    Path(record.get("repository") or args.repo)
                )
                event = event_from_payload(
                    record["payload"],
                    repo=repo_path,
                    host="pi",
                )
                CaptureStore(
                    capture_path(repo_path)
                ).append(event)
                replayed += 1
            except Exception:
                pending.append(record)
        spool.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False) + "\n"
                for item in pending
            ),
            encoding="utf-8",
        )
        print(
            json.dumps({"replayed": replayed, "pending": len(pending)})
        )
        return 0 if not pending else 1
    return 2


def _doctor(args: argparse.Namespace) -> int:
    report, status = run_doctor(
        Path(args.repo),
        api_base=args.api_base,
        strict=args.strict,
        operation=args.operation,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return status


def _github_state(args: argparse.Namespace) -> tuple[Path, ReviewState]:
    repo_path = resolve_repository(Path(args.repo))
    requirements = getattr(args, "requirement", None)
    if not requirements:
        probe = build_change_proof(
            repo_path, base_ref=getattr(args, "base", None)
        )
        existing = find_review_for_patch(
            repo_path,
            base_sha=probe.git_snapshot.base_sha,
            patch_fingerprint=probe.git_snapshot.patch_fingerprint,
        )
        if existing is not None:
            requirements = [
                item.original_text
                for item in existing.requirements
                if not item.deleted
            ]
    state = load_or_create_review_state(
        repo_path,
        base_ref=getattr(args, "base", None),
        requirement_texts=requirements,
    )
    return repo_path, state


def _github_status(args: argparse.Namespace) -> int:
    try:
        repo = resolve_repository(Path(args.repo))
        credential = resolve_github_credential()
        slug = repository_slug(repo, args.github_repo)
        account = asyncio.run(
            GitHubPublicationService(
                credential.token, api_base=args.api_base
            ).request("GET", "/user")
        )
        print(json.dumps({
            "authenticated": True,
            "credential_source": credential.source,
            "repository": slug,
            "login": account.get("login", ""),
        }))
        return 0
    except (GitHubIntegrationError, ReviewStorageError) as exc:
        print(json.dumps({
            "authenticated": False,
            "error": getattr(exc, "code", "review_storage_error"),
        }), file=sys.stderr)
        return 2


async def _publish_github_async(args: argparse.Namespace) -> dict:
    repo, state = _github_state(args)
    state.refresh()
    proof = state.proof
    current_patch = proof.git_snapshot.patch_fingerprint
    if not current_patch:
        raise GitHubIntegrationError("patch_fingerprint_missing")
    current_patch_fingerprint = canonical_change_identity(
        repo, proof.git_snapshot.base_sha
    )[1]
    workspace_dirty = bool(subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    ).stdout)
    credential = resolve_github_credential()
    slug = repository_slug(repo, args.github_repo)
    owner, name = slug.split("/", 1)
    service = GitHubPublicationService(
        credential.token,
        api_base=args.api_base,
    )
    pr = await service.request(
        "GET", f"/repos/{owner}/{name}/pulls/{args.pr}"
    )
    local_head = proof.git_snapshot.head_sha
    binding = validate_pr_binding(
        repository=slug,
        local_head_sha=local_head,
        proof_base_sha=proof.git_snapshot.base_sha,
        proof_base_ref=proof.git_snapshot.base_ref,
        proof_patch_fingerprint=current_patch,
        current_patch_fingerprint=current_patch_fingerprint,
        workspace_dirty=workspace_dirty,
        change_proof_stale=state.stale,
        pull_request=pr,
    )
    remote_head = str(pr.get("head", {}).get("sha", ""))
    generated = proof_markdown(proof, stale=state.stale)
    semantic_hash = build_evidence_graph(proof).semantic_hash
    generated = generated.replace(
        "<!-- /codecairn:change-proof -->",
        f"Graph semantic hash: `{semantic_hash}`\n"
        "<!-- /codecairn:change-proof -->",
    )
    method = "PATCH"
    remote_id = str(args.pr)
    url = str(pr.get("html_url", ""))
    payload: dict
    path: str
    if args.target == "description":
        path = f"/repos/{owner}/{name}/pulls/{args.pr}"
        payload = {"body": replace_marker(str(pr.get("body") or ""), generated)}
    elif args.target == "comment":
        family_marker = (
            f"<!-- codecairn:review-family:{proof.review_family_id} -->"
        )
        body = family_marker + "\n" + generated
        comments = await service.paginate(
            f"/repos/{owner}/{name}/issues/{args.pr}/comments"
        )
        existing = next(
            (
                item for item in comments
                if family_marker in str(item.get("body", ""))
                and CHANGE_PROOF_START in str(item.get("body", ""))
            ),
            None,
        )
        if existing:
            remote_id = str(existing["id"])
            path = f"/repos/{owner}/{name}/issues/comments/{remote_id}"
            payload = {"body": body}
            url = str(existing.get("html_url", ""))
        else:
            method = "POST"
            path = f"/repos/{owner}/{name}/issues/{args.pr}/comments"
            payload = {"body": body}
    else:
        conclusion = check_conclusion(
            gate=proof.gate.status,
            assurance=proof.assurance.level,
            reasons=proof.gate.reasons,
            verification_failed=any(
                item.effective_status == "failed"
                for item in proof.verifications
            ),
        )
        check_runs = await service.paginate(
            f"/repos/{owner}/{name}/commits/{remote_head}/check-runs",
            item_key="check_runs",
        )
        external_id = f"{proof.review_family_id}/{proof.change_id}"
        existing = next(
            (
                item for item in check_runs
                if item.get("name") == "CodeCairn Change Proof"
                and item.get("external_id") == external_id
            ),
            None,
        )
        payload = {
            "name": "CodeCairn Change Proof",
            "head_sha": remote_head,
            "external_id": external_id,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": f"Assurance {proof.assurance.level.upper()}",
                "summary": generated[:65000],
            },
        }
        if existing:
            remote_id = str(existing["id"])
            path = f"/repos/{owner}/{name}/check-runs/{remote_id}"
            url = str(existing.get("html_url", ""))
            payload.pop("head_sha", None)
        else:
            method = "POST"
            path = f"/repos/{owner}/{name}/check-runs"
    preview = {
        "dry_run": not args.yes,
        "method": method,
        "path": path,
        "payload": payload,
        "head_sha": local_head,
        "patch_fingerprint": current_patch,
        "binding": binding,
    }
    if not args.yes:
        return preview
    response = await service.request(method, path, payload)
    remote_id = str(response.get("id", remote_id))
    url = str(response.get("html_url", url))
    content_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    publication = Publication(
        id="publication_" + hashlib.sha256(
            f"{args.target}:{slug}:{args.pr}:{remote_id}:{content_hash}".encode()
        ).hexdigest()[:20],
        target=args.target,
        repository=slug,
        pr_number=args.pr,
        remote_id=remote_id,
        url=url,
        local_head_sha=local_head,
        remote_head_sha=remote_head,
        patch_fingerprint=current_patch,
        review_family_id=proof.review_family_id,
        change_id=proof.change_id,
        content_hash=content_hash,
        provenance=Provenance(kind="verified", source="github_api"),
    )
    existing_publication = next(
        (item for item in proof.publications if item.id == publication.id),
        None,
    )
    if existing_publication is None:
        proof.publications.append(publication)
    else:
        publication = existing_publication
    state.commit(
        "github_publication_succeeded",
        {
            "publication_id": publication.id,
            "target": args.target,
            "remote_id": remote_id,
            "head_sha": local_head,
            "patch_fingerprint": current_patch,
        },
        actor_type="adapter",
        actor_id="github",
    )
    return {"dry_run": False, "publication": publication.model_dump(mode="json")}


def _publish_github(args: argparse.Namespace) -> int:
    try:
        print(json.dumps(
            asyncio.run(_publish_github_async(args)),
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    except (GitHubIntegrationError, ReviewStorageError) as exc:
        payload = {
            "error": getattr(exc, "code", "review_storage_error"),
        }
        if isinstance(exc, GitHubIntegrationError) and exc.details:
            payload["details"] = exc.details
        print(json.dumps(payload), file=sys.stderr)
        return 2


def _repository_identity(repo: Path) -> str:
    try:
        return repository_slug(repo)
    except GitHubIntegrationError:
        return repo.name


def _parse_github_time(value: object) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _github_metadata_result(
    run: dict, repository: str, trust_reason: str
) -> CIVerificationResult:
    semantic = json.dumps(
        run, sort_keys=True, separators=(",", ":")
    ).encode()
    pull_requests = run.get("pull_requests") or []
    base_sha = (
        str(pull_requests[0].get("base", {}).get("sha", ""))
        if pull_requests and isinstance(pull_requests[0], dict)
        else ""
    )
    conclusion = run.get("conclusion")
    return CIVerificationResult(
        run_id=str(run.get("id", "")),
        run_attempt=max(int(run.get("run_attempt") or 1), 1),
        provider="github_workflow_metadata",
        repository=repository,
        workflow_path=str(run.get("workflow_path") or run.get("path") or ""),
        workflow_ref=str(run.get("workflow_ref") or run.get("head_branch") or ""),
        event_name=str(run.get("event") or ""),
        pr_number=(
            int(pull_requests[0].get("number"))
            if pull_requests and pull_requests[0].get("number")
            else None
        ),
        head_sha=str(run.get("head_sha", "")),
        base_sha=base_sha,
        patch_fingerprint="",
        requirement_contract_hash="",
        command_argv=[],
        result=(
            "passed"
            if conclusion == "success"
            else "failed"
            if conclusion
            else "not_run"
        ),
        exit_code=None,
        environment={"workflow": str(run.get("name", ""))},
        started_at=_parse_github_time(
            run.get("run_started_at") or run.get("created_at")
        ),
        finished_at=_parse_github_time(
            run.get("updated_at") or run.get("created_at")
        ),
        output_hash=hashlib.sha256(semantic).hexdigest(),
        trust_reason=trust_reason,
        provenance=Provenance(
            kind="captured", source="github_api_metadata"
        ),
    )


async def _github_ci_artifact(
    service: GitHubPublicationService,
    owner: str,
    repository: str,
    run_id: str,
    artifact_name: str = "codecairn-ci-result",
) -> tuple[dict, CIArtifactDownload]:
    run = await service.request(
        "GET",
        f"/repos/{owner}/{repository}/actions/runs/{run_id}",
    )
    artifacts = await service.paginate(
        f"/repos/{owner}/{repository}/actions/runs/{run_id}/artifacts"
        "?per_page=100",
        item_key="artifacts",
    )
    matches = [
        item
        for item in artifacts
        if item.get("name") == artifact_name
        and not item.get("expired", False)
    ]
    if not matches:
        return run, CIArtifactDownload(
            artifact_id="",
            name=artifact_name,
            size_in_bytes=0,
            error="ci_artifact_missing",
        )
    if len(matches) != 1:
        return run, CIArtifactDownload(
            artifact_id="",
            name=artifact_name,
            size_in_bytes=0,
            error="ci_artifact_ambiguous",
        )
    artifact = matches[0]
    artifact_id = str(artifact.get("id", ""))
    declared_digest = str(artifact.get("digest") or "")
    size = int(artifact.get("size_in_bytes") or 0)
    if bool(artifact.get("expired", False)):
        return run, CIArtifactDownload(
            artifact_id=artifact_id,
            name=artifact_name,
            size_in_bytes=size,
            expired=True,
            declared_digest=declared_digest,
            error="ci_artifact_expired",
        )
    download = str(
        artifact.get("archive_download_url")
        or f"/repos/{owner}/{repository}/actions/artifacts/"
        f"{artifact.get('id')}/zip"
    )
    content, measured_digest = await service.download_stream(download)
    error = ""
    if declared_digest and declared_digest.lower() != measured_digest.lower():
        error = "ci_artifact_digest_mismatch"
        content = b""
    elif not declared_digest:
        error = "ci_artifact_digest_missing"
    return run, CIArtifactDownload(
        artifact_id=artifact_id,
        name=artifact_name,
        size_in_bytes=size,
        declared_digest=declared_digest,
        measured_digest=measured_digest,
        content=content,
        error=error,
    )


def _ci(args: argparse.Namespace) -> int:
    try:
        if args.ci_command == "run":
            manifest = CIManifest.model_validate_json(
                Path(args.manifest).read_text(encoding="utf-8")
            )
            private_key = load_key(
                args.private_key
                or os.environ.get("CODECAIRN_CI_PRIVATE_KEY")
            )
            result = run_ci_manifest(
                manifest,
                repository=Path(args.repo).resolve(),
                private_key=private_key,
                signer=args.signer or os.environ.get("CODECAIRN_CI_SIGNER", ""),
            )
            Path(args.output).write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
            return 0 if result.result == "passed" else 1
        repo, state = _github_state(args)
        proof = state.proof
        if args.ci_command == "manifest":
            command = args.command or ["python", "-m", "pytest", "-q"]
            manifest = CIManifest(
                repository=_repository_identity(repo),
                workflow_path=getattr(args, "workflow_path", "") or "",
                workflow_ref=getattr(args, "workflow_ref", "") or "",
                event_name=getattr(args, "event_name", "") or "",
                pr_number=getattr(args, "pr_number", None),
                head_sha=proof.git_snapshot.head_sha,
                base_sha=proof.git_snapshot.base_sha,
                patch_fingerprint=proof.git_snapshot.patch_fingerprint,
                requirement_contract_hash=proof.requirement_contract_hash,
                command_argv=command,
                requirement_ids=[item.id for item in proof.requirements if not item.deleted],
                hunk_ids=[item.id for item in proof.patch_hunks],
                file_change_ids=[item.id for item in proof.file_changes],
            )
            Path(args.output).write_text(
                manifest.model_dump_json(indent=2), encoding="utf-8"
            )
            return 0
        github_run: dict | None = None
        imported_attestation: CIAttestation | None = None
        if args.ci_command == "import-github":
            credential = resolve_github_credential()
            slug = repository_slug(repo, args.github_repo)
            owner, name = slug.split("/", 1)
            service = GitHubPublicationService(
                credential.token, api_base=args.api_base
            )
            try:
                policy = load_trust_policy_from_base(
                    repo, proof.git_snapshot.base_sha
                )
                artifact_name = policy.artifact_name
            except TrustPolicyError:
                policy = None
                artifact_name = "codecairn-ci-result"
            artifact_response = asyncio.run(
                _github_ci_artifact(
                    service, owner, name, str(args.run_id), artifact_name
                )
            )
            if len(artifact_response) == 3:
                # Compatibility for integrations using the legacy adapter tuple.
                github_run, legacy_content, legacy_error = artifact_response
                artifact_download = CIArtifactDownload(
                    artifact_id="",
                    name=artifact_name,
                    size_in_bytes=len(legacy_content or b""),
                    content=legacy_content or b"",
                    error=legacy_error or "ci_artifact_digest_missing",
                )
            else:
                github_run, artifact_download = artifact_response
            if not artifact_download.content:
                result = _github_metadata_result(
                    github_run, slug, artifact_download.error
                )
                result.artifact_id = artifact_download.artifact_id
                result.artifact_digest = artifact_download.declared_digest
                result.artifact_size = artifact_download.size_in_bytes
            else:
                try:
                    result, attestation = load_ci_artifact_package(
                        artifact_download.content
                    )
                    if getattr(args, "attestation_file", None):
                        attestation = CIAttestation.model_validate_json(
                            Path(args.attestation_file).read_text(
                                encoding="utf-8"
                            )
                        )
                    imported_attestation = attestation
                except CIVerificationError as exc:
                    result = _github_metadata_result(
                        github_run, slug, exc.code
                    )
                    attestation = None
        else:
            result = CIVerificationResult.model_validate_json(
                Path(args.file).read_text(encoding="utf-8")
            )
        public_key = load_key(
            getattr(args, "public_key", None)
            or os.environ.get("CODECAIRN_CI_PUBLIC_KEY")
        )
        if args.ci_command == "import-github" and result.provider != "github_workflow_metadata":
            workflow_path = str(
                github_run.get("workflow_path")
                or github_run.get("path")
                or result.workflow_path
            )
            workflow_ref = str(
                github_run.get("workflow_ref")
                or result.workflow_ref
            )
            event_name = str(github_run.get("event") or result.event_name)
            pulls = github_run.get("pull_requests") or []
            pull = pulls[0] if pulls else {}
            pr_number = int(pull.get("number") or result.pr_number or 0)
            remote_base = str(pull.get("base", {}).get("sha", ""))
            remote_head = str(github_run.get("head_sha") or "")
            if remote_head and remote_head != proof.git_snapshot.head_sha:
                result.trusted = False
                result.trust_reason = "workflow_head_mismatch"
            elif remote_base and remote_base != proof.git_snapshot.base_sha:
                result.trusted = False
                result.trust_reason = "workflow_base_mismatch"
            elif policy is None:
                result.trusted = False
                result.trust_reason = "ci_trust_policy_missing"
            elif pr_number < 1:
                result.trusted = False
                result.trust_reason = "ci_pr_identity_missing"
            else:
                result = assess_attested_observation(
                    result,
                    attestation,
                    policy=policy,
                    repository=_repository_identity(repo),
                    artifact_id=artifact_download.artifact_id,
                    artifact_digest=artifact_download.declared_digest,
                    run_id=str(github_run.get("id", "")),
                    run_attempt=int(github_run.get("run_attempt") or 1),
                    workflow_path=workflow_path,
                    workflow_ref=workflow_ref,
                    event_name=event_name,
                    pr_number=pr_number,
                    head_sha=proof.git_snapshot.head_sha,
                    base_sha=proof.git_snapshot.base_sha,
                    patch_fingerprint=proof.git_snapshot.patch_fingerprint,
                    requirement_contract_hash=proof.requirement_contract_hash,
                    requirement_ids={
                        item.id for item in proof.requirements if not item.deleted
                    },
                    hunk_ids={item.id for item in proof.patch_hunks},
                    file_change_ids={item.id for item in proof.file_changes},
                )
        elif result.provider != "github_workflow_metadata":
            try:
                local_policy = load_trust_policy_from_base(
                    repo, proof.git_snapshot.base_sha
                )
            except TrustPolicyError:
                local_policy = None
            if local_policy is None or not local_policy.allow_local_manual_signing:
                result.trusted = False
                result.trust_reason = (
                    "ci_trust_policy_missing"
                    if local_policy is None
                    else "ci_local_signing_not_allowed"
                )
                result.trust_source = "user_managed_ed25519"
            else:
                result = assess_ci_result(
                    result,
                    public_key=public_key,
                    repository=_repository_identity(repo),
                    run_id=None,
                    run_attempt=None,
                    workflow_head_sha=None,
                    workflow_base_sha=None,
                    head_sha=proof.git_snapshot.head_sha,
                    base_sha=proof.git_snapshot.base_sha,
                    patch_fingerprint=proof.git_snapshot.patch_fingerprint,
                    requirement_contract_hash=proof.requirement_contract_hash,
                    requirement_ids={
                        item.id for item in proof.requirements if not item.deleted
                    },
                    hunk_ids={item.id for item in proof.patch_hunks},
                    file_change_ids={item.id for item in proof.file_changes},
                )
                result.trust_source = "user_managed_ed25519"
                result.policy_hash = local_policy.policy_hash
        else:
            result.trusted = False
            result.provenance = result.provenance.model_copy(
                update={"kind": "captured"}
            )
        existing_ci = next(
            (
                item for item in proof.ci_verifications
                if ci_result_identity(item) == ci_result_identity(result)
            ),
            None,
        )
        if existing_ci is not None:
            if ci_results_collide(existing_ci, result):
                result.trusted = False
                result.trust_reason = "ci_run_id_collision"
                state.commit(
                    "ci_verification_run_id_collision",
                    {
                        "run_id": result.run_id,
                        "patch_fingerprint": result.patch_fingerprint,
                    },
                    actor_type="adapter",
                    actor_id="ci_import",
                )
                print(json.dumps({
                    "run_id": result.run_id,
                    "trusted": False,
                    "trust_reason": result.trust_reason,
                }))
                return 1
            else:
                state.commit(
                    "ci_verification_duplicate_ignored",
                    {"run_id": result.run_id},
                    actor_type="adapter",
                    actor_id="ci_import",
                )
                print(json.dumps({
                    "run_id": result.run_id,
                    "trusted": existing_ci.trusted,
                    "duplicate": True,
                }))
                return 0 if existing_ci.trusted else 1
        proof.ci_verifications.append(result)
        if imported_attestation is not None:
            proof.ci_attestations.append(imported_attestation)
        prior_policy_hashes = {
            item.policy_hash
            for item in proof.ci_verifications[:-1]
            if item.policy_hash
        }
        if result.policy_hash and (
            not prior_policy_hashes
            or result.policy_hash not in prior_policy_hashes
        ):
            state.audit(
                "ci_trust_policy_changed",
                {
                    "policy_hash": result.policy_hash,
                    "previous_policy_hashes": sorted(prior_policy_hashes),
                },
                actor_type="adapter",
                actor_id="ci_import",
            )
        event = state.audit(
            "ci_verification_imported",
            {
                "run_id": result.run_id,
                "trusted": result.trusted,
                "trust_reason": result.trust_reason,
                "patch_fingerprint": result.patch_fingerprint,
                "head_sha": result.head_sha,
                "result": result.result,
                "artifact_id": result.artifact_id,
                "artifact_digest": result.artifact_digest,
                "trust_source": result.trust_source,
                "policy_hash": result.policy_hash,
            },
            actor_type="adapter",
            actor_id="ci_import",
        )
        if result.trusted:
            verification_id = (
                f"verification_{result.provider}_{result.run_id}_"
                f"{result.run_attempt}"
            )
            verification = Verification(
                id=verification_id,
                command=" ".join(result.command_argv),
                command_argv=result.command_argv,
                result_status=result.result,
                effective_status=result.result,
                requirement_ids=result.requirement_ids,
                hunk_ids=result.hunk_ids,
                file_change_ids=result.file_change_ids,
                exit_code=result.exit_code,
                output_summary=f"CI output sha256:{result.output_hash}",
                commit_sha=result.head_sha,
                workspace_tree_sha=proof.git_snapshot.content_tree_hash,
                content_tree_hash=proof.git_snapshot.content_tree_hash,
                patch_fingerprint=result.patch_fingerprint,
                provenance=Provenance(kind="verified", source="ci_attestation"),
            )
            proof.verifications.append(verification)
            if result.result == "passed":
                for target_type, ids in (
                    ("requirement", result.requirement_ids),
                    ("hunk", result.hunk_ids),
                    ("file_change", result.file_change_ids),
                ):
                    for target_id in ids:
                        assertion = CoverageAssertion(
                            id=(
                                f"coverage_{result.provider}_{result.run_id}_"
                                f"{result.run_attempt}_{target_type}_{target_id}"
                            ),
                            verification_id=verification.id,
                            target_type=target_type,
                            target_id=target_id,
                            status="confirmed",
                            explanation="Confirmed by trusted CI attestation.",
                            provenance=Provenance(kind="verified", source="ci_attestation"),
                        )
                        proof.coverage_assertions.append(assertion)
                        state.decide(
                            target_type="coverage_assertion",
                            target_id=assertion.id,
                            decision="confirmed",
                            explanation="Trusted CI attestation.",
                            reviewer="ci_attestation",
                        )
        state.commit(
            "ci_verification_import_completed",
            {"run_id": result.run_id, "source_event_id": event.event_id},
            actor_type="system",
            actor_id="ci_trust_policy",
        )
        print(json.dumps({
            "run_id": result.run_id,
            "trusted": result.trusted,
            "trust_reason": result.trust_reason,
        }))
        return 0 if result.trusted else 1
    except Exception as exc:
        code = getattr(exc, "code", "ci_operation_failed")
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn",
        description="CodeCairn evidence layer for AI coding and review",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"CodeCairn {__version__}",
    )
    commands = parser.add_subparsers(dest="command")
    doctor = commands.add_parser(
        "doctor", help="Diagnose local CodeCairn dependencies"
    )
    doctor.add_argument("--repo", default=".")
    doctor.add_argument("--strict", action="store_true")
    doctor.add_argument(
        "--operation",
        choices=["review", "github-publish", "ci-import"],
    )
    doctor.add_argument(
        "--api-base",
        default=os.environ.get(
            "CODECAIRN_GITHUB_API_BASE", "https://api.github.com"
        ),
    )
    doctor.set_defaults(handler=_doctor)
    review = commands.add_parser(
        "review",
        help="Build a Change Proof and open the local review workspace",
    )
    review.add_argument("--repo", default=".")
    review.add_argument(
        "--base",
        help="Base Git ref (default: main/master/origin fallback)",
    )
    review.add_argument(
        "--requirement",
        action="append",
        default=[],
        help="Requirement text; may be repeated",
    )
    review.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local port (default: choose an available port)",
    )
    review.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the browser automatically",
    )
    review.add_argument(
        "--format",
        choices=["web", "json", "markdown", "html", "svg", "png"],
        default="web",
        help="Output format (default: web)",
    )
    review.add_argument("--output", help="Write export output to this file")
    review.set_defaults(handler=_review)
    capture = commands.add_parser(
        "capture", help="Capture coding-agent events into the local ledger"
    )
    capture_commands = capture.add_subparsers(dest="capture_command", required=True)
    capture_ingest = capture_commands.add_parser(
        "ingest",
        help="Ingest one coding-agent event payload from stdin",
    )
    capture_ingest.add_argument(
        "--host",
        choices=[
            "pi",
            "claude_code",
            "codex",
            "cursor",
            "manual",
            "unknown",
        ],
        default="unknown",
    )
    capture_ingest.add_argument("--repo", default=".")
    capture_ingest.set_defaults(handler=_capture)
    capture_sessions = capture_commands.add_parser("sessions")
    capture_sessions.add_argument("--repo", default=".")
    capture_sessions.set_defaults(handler=_capture)
    capture_show = capture_commands.add_parser("show")
    capture_show.add_argument("session_id")
    capture_show.add_argument("--repo", default=".")
    capture_show.set_defaults(handler=_capture)
    capture_replay = capture_commands.add_parser(
        "replay",
        help="Replay Pi events spooled while the local collector was unavailable",
    )
    capture_replay.add_argument("--repo", default=".")
    capture_replay.add_argument("--file")
    capture_replay.set_defaults(handler=_capture)
    github = commands.add_parser("github", help="Inspect GitHub authentication")
    github_commands = github.add_subparsers(
        dest="github_command", required=True
    )
    github_status = github_commands.add_parser("status")
    github_status.add_argument("--repo", default=".")
    github_status.add_argument("--github-repo", metavar="OWNER/NAME")
    github_status.add_argument(
        "--api-base",
        default=os.environ.get(
            "CODECAIRN_GITHUB_API_BASE", "https://api.github.com"
        ),
    )
    github_status.add_argument("--base")
    github_status.add_argument("--requirement", action="append", default=[])
    github_status.set_defaults(handler=_github_status)
    publish = commands.add_parser(
        "publish", help="Publish a Change Proof"
    )
    publish_commands = publish.add_subparsers(
        dest="publish_provider", required=True
    )
    publish_github = publish_commands.add_parser("github")
    publish_github.add_argument("--repo", default=".")
    publish_github.add_argument("--github-repo", metavar="OWNER/NAME")
    publish_github.add_argument("--base")
    publish_github.add_argument("--requirement", action="append", default=[])
    publish_github.add_argument("--pr", type=int, required=True)
    publish_github.add_argument(
        "--target",
        choices=["description", "comment", "check"],
        required=True,
    )
    publish_github.add_argument(
        "--api-base",
        default=os.environ.get(
            "CODECAIRN_GITHUB_API_BASE", "https://api.github.com"
        ),
    )
    confirmation = publish_github.add_mutually_exclusive_group()
    confirmation.add_argument("--yes", action="store_true")
    confirmation.add_argument("--dry-run", action="store_true")
    publish_github.set_defaults(handler=_publish_github)
    ci = commands.add_parser("ci", help="Trusted CI verification exchange")
    ci_commands = ci.add_subparsers(dest="ci_command", required=True)
    ci_manifest = ci_commands.add_parser("manifest")
    ci_manifest.add_argument("--repo", default=".")
    ci_manifest.add_argument("--base")
    ci_manifest.add_argument("--requirement", action="append", default=[])
    ci_manifest.add_argument("--output", required=True)
    ci_manifest.add_argument("--workflow-path", default="")
    ci_manifest.add_argument("--workflow-ref", default="")
    ci_manifest.add_argument("--event-name", default="")
    ci_manifest.add_argument("--pr-number", type=int)
    ci_manifest.add_argument(
        "--command", nargs=argparse.REMAINDER,
        help="Exact command argv (must be the final manifest options)",
    )
    ci_manifest.set_defaults(handler=_ci)
    ci_run = ci_commands.add_parser("run")
    ci_run.add_argument("--repo", default=".")
    ci_run.add_argument("--manifest", required=True)
    ci_run.add_argument("--output", required=True)
    ci_run.add_argument("--private-key")
    ci_run.add_argument("--signer", default="")
    ci_run.set_defaults(handler=_ci)
    ci_import = ci_commands.add_parser("import")
    ci_import.add_argument("--repo", default=".")
    ci_import.add_argument("--base")
    ci_import.add_argument("--requirement", action="append", default=[])
    ci_import.add_argument("--file", required=True)
    ci_import.add_argument("--public-key")
    ci_import.set_defaults(handler=_ci)
    ci_github = ci_commands.add_parser("import-github")
    ci_github.add_argument("--repo", default=".")
    ci_github.add_argument("--base")
    ci_github.add_argument("--requirement", action="append", default=[])
    ci_github.add_argument("--run-id", required=True)
    ci_github.add_argument("--github-repo", metavar="OWNER/NAME")
    ci_github.add_argument("--public-key")
    ci_github.add_argument(
        "--attestation-file",
        help="Independent attestation JSON (user-managed Alpha interface)",
    )
    ci_github.add_argument(
        "--api-base",
        default=os.environ.get(
            "CODECAIRN_GITHUB_API_BASE", "https://api.github.com"
        ),
    )
    ci_github.set_defaults(handler=_ci)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
