from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import zipfile
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from codecairn.review.ci_measurement import (
    CIMeasurementError,
    measure_repository,
    validate_manifest_snapshot,
)
from codecairn.review.models import (
    CIAttestation,
    CIManifest,
    CIVerificationResult,
    Provenance,
)
from codecairn.review.trust_policy import CITrustPolicy


class CIVerificationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


CI_ARTIFACT_NAME = "codecairn-ci-result.json"
CI_ARTIFACT_MAX_FILES = 4
CI_ARTIFACT_MAX_FILE_SIZE = 2 * 1024 * 1024
CI_ARTIFACT_MAX_TOTAL_SIZE = 4 * 1024 * 1024


def canonical_ci_result(result: CIVerificationResult) -> bytes:
    payload = result.model_dump(mode="json")
    payload["signature"] = ""
    payload["trusted"] = False
    payload["trust_reason"] = "unsigned"
    payload["provenance"] = {
        **payload["provenance"],
        "kind": "captured",
        "source": "ci_result",
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def ci_observation_digest(result: CIVerificationResult) -> str:
    return "sha256:" + hashlib.sha256(canonical_ci_result(result)).hexdigest()


def canonical_ci_attestation(attestation: CIAttestation) -> bytes:
    payload = attestation.model_dump(mode="json")
    payload["signature"] = ""
    payload["provenance"] = {
        **payload["provenance"],
        "kind": "captured",
        "source": "ci_attestation",
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def ci_result_identity(
    result: CIVerificationResult,
) -> tuple[str, str, str, int]:
    return (
        result.provider,
        result.repository,
        result.run_id,
        result.run_attempt,
    )


def ci_results_collide(
    first: CIVerificationResult, second: CIVerificationResult
) -> bool:
    return (
        ci_result_identity(first) == ci_result_identity(second)
        and canonical_ci_result(first) != canonical_ci_result(second)
    )


def _private_key(value: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(value, password=None)
    except ValueError:
        key = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(value.strip())
        )
    if not isinstance(key, Ed25519PrivateKey):
        raise CIVerificationError("ci_signing_key_not_ed25519")
    return key


def _public_key(value: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(value)
    except ValueError:
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(value.strip())
        )
    if not isinstance(key, Ed25519PublicKey):
        raise CIVerificationError("ci_public_key_not_ed25519")
    return key


def validate_public_key(value: bytes) -> bool:
    try:
        _public_key(value)
        return True
    except (CIVerificationError, ValueError):
        return False


def sign_ci_result(
    result: CIVerificationResult, private_key: bytes, signer: str
) -> CIVerificationResult:
    signed = result.model_copy(deep=True)
    signed.signer = signer
    signed.signature = base64.b64encode(
        _private_key(private_key).sign(canonical_ci_result(signed))
    ).decode()
    return signed


def verify_ci_signature(
    result: CIVerificationResult, public_key: bytes
) -> bool:
    if not result.signature:
        return False
    try:
        _public_key(public_key).verify(
            base64.b64decode(result.signature),
            canonical_ci_result(result),
        )
        return True
    except (InvalidSignature, ValueError):
        return False


def sign_ci_attestation(
    attestation: CIAttestation, private_key: bytes
) -> CIAttestation:
    signed = attestation.model_copy(deep=True)
    signed.signature = base64.b64encode(
        _private_key(private_key).sign(canonical_ci_attestation(signed))
    ).decode()
    return signed


def verify_ci_attestation_signature(
    attestation: CIAttestation, public_key: bytes
) -> bool:
    if not attestation.signature:
        return False
    try:
        _public_key(public_key).verify(
            base64.b64decode(attestation.signature),
            canonical_ci_attestation(attestation),
        )
        return True
    except (InvalidSignature, ValueError):
        return False


def load_ci_artifact_package(
    data: bytes,
) -> tuple[CIVerificationResult, CIAttestation | None]:
    """Read only the declared CI files without extracting to disk."""
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise CIVerificationError("ci_artifact_invalid_zip") from exc
    members = archive.infolist()
    if len(members) > CI_ARTIFACT_MAX_FILES:
        raise CIVerificationError("ci_artifact_too_many_files")
    seen: set[str] = set()
    total = 0
    payloads: dict[str, bytes] = {}
    for member in members:
        name = member.filename.replace("\\", "/")
        path = Path(name)
        if (
            member.is_dir()
            or path.is_absolute()
            or ".." in path.parts
            or name != CI_ARTIFACT_NAME
        ):
            raise CIVerificationError("ci_artifact_unexpected_file")
        if name in seen:
            raise CIVerificationError("ci_artifact_duplicate_path")
        seen.add(name)
        if member.file_size > CI_ARTIFACT_MAX_FILE_SIZE:
            raise CIVerificationError("ci_artifact_file_too_large")
        total += member.file_size
        if total > CI_ARTIFACT_MAX_TOTAL_SIZE:
            raise CIVerificationError("ci_artifact_total_too_large")
        with archive.open(member) as source:
            payload = source.read(CI_ARTIFACT_MAX_FILE_SIZE + 1)
        if len(payload) > CI_ARTIFACT_MAX_FILE_SIZE:
            raise CIVerificationError("ci_artifact_file_too_large")
        payloads[name] = payload
    if CI_ARTIFACT_NAME not in payloads:
        raise CIVerificationError("ci_artifact_result_missing")
    try:
        result = CIVerificationResult.model_validate_json(
            payloads[CI_ARTIFACT_NAME]
        )
        return result, None
    except ValueError as exc:
        raise CIVerificationError("ci_artifact_result_invalid") from exc


def load_ci_result_artifact(data: bytes) -> CIVerificationResult:
    return load_ci_artifact_package(data)[0]


def assess_ci_result(
    result: CIVerificationResult,
    *,
    public_key: bytes | None,
    repository: str,
    run_id: str | None,
    run_attempt: int | None,
    workflow_head_sha: str | None,
    workflow_base_sha: str | None,
    head_sha: str,
    base_sha: str,
    patch_fingerprint: str,
    requirement_contract_hash: str,
    requirement_ids: set[str],
    hunk_ids: set[str],
    file_change_ids: set[str],
) -> CIVerificationResult:
    assessed = result.model_copy(deep=True)
    signature_valid = bool(
        public_key and verify_ci_signature(assessed, public_key)
    )
    bindings = [
        ("repository_mismatch", assessed.repository == repository),
        (
            "run_id_mismatch",
            run_id is None or assessed.run_id == str(run_id),
        ),
        (
            "run_attempt_mismatch",
            run_attempt is None or assessed.run_attempt == run_attempt,
        ),
        (
            "workflow_head_mismatch",
            workflow_head_sha is None
            or assessed.head_sha == workflow_head_sha,
        ),
        (
            "workflow_base_mismatch",
            not workflow_base_sha
            or assessed.base_sha == workflow_base_sha,
        ),
        ("head_mismatch", assessed.head_sha == head_sha),
        ("base_mismatch", assessed.base_sha == base_sha),
        (
            "patch_mismatch",
            assessed.patch_fingerprint == patch_fingerprint,
        ),
        (
            "requirement_contract_mismatch",
            assessed.requirement_contract_hash
            == requirement_contract_hash,
        ),
        (
            "coverage_ids_invalid",
            set(assessed.requirement_ids) <= requirement_ids
            and set(assessed.hunk_ids) <= hunk_ids
            and set(assessed.file_change_ids) <= file_change_ids,
        ),
    ]
    failed_binding = next(
        (code for code, valid in bindings if not valid), None
    )
    assessed.trusted = signature_valid and failed_binding is None
    assessed.trust_reason = (
        "verified"
        if assessed.trusted
        else "ci_public_key_missing"
        if public_key is None
        else "ci_signature_missing"
        if not assessed.signature
        else "ci_signature_invalid"
        if not signature_valid
        else failed_binding or "ci_untrusted"
    )
    assessed.provenance = assessed.provenance.model_copy(
        update={
            "kind": "verified" if assessed.trusted else "captured",
            "source": "ed25519_ci" if assessed.trusted else "ci_import",
        }
    )
    return assessed


def assess_attested_observation(
    result: CIVerificationResult,
    attestation: CIAttestation | None,
    *,
    policy: CITrustPolicy,
    repository: str,
    artifact_id: str,
    artifact_digest: str,
    run_id: str,
    run_attempt: int,
    workflow_path: str,
    workflow_ref: str,
    event_name: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    patch_fingerprint: str,
    requirement_contract_hash: str,
    requirement_ids: set[str],
    hunk_ids: set[str],
    file_change_ids: set[str],
    now: datetime | None = None,
) -> CIVerificationResult:
    """Promote an Observation only after policy and attestation verification."""
    assessed = result.model_copy(deep=True)
    assessed.trusted = False
    assessed.policy_hash = policy.policy_hash
    assessed.trust_source = "observation"
    assessed.artifact_id = artifact_id
    assessed.artifact_digest = artifact_digest
    if not artifact_digest:
        assessed.trust_reason = "ci_artifact_digest_missing"
        return assessed
    if attestation is None:
        assessed.trust_reason = "ci_attestation_missing"
        return assessed
    current = now or datetime.now(timezone.utc)
    public_key_value = policy.public_keys.get(attestation.issuer)
    public_key = (
        public_key_value.encode() if public_key_value is not None else None
    )
    bindings = [
        ("ci_policy_repository_denied", policy.allowed_repository == repository),
        (
            "ci_policy_workflow_denied",
            workflow_path in policy.allowed_workflow_paths,
        ),
        ("ci_policy_ref_denied", workflow_ref in policy.allowed_workflow_refs),
        ("ci_policy_event_denied", event_name in policy.allowed_event_types),
        (
            "ci_attestation_issuer_denied",
            attestation.issuer in policy.trusted_attestation_issuers,
        ),
        (
            "ci_attestation_expired",
            attestation.issued_at <= current <= attestation.expires_at
            and (current - attestation.finished_at).total_seconds()
            <= policy.maximum_result_age_seconds,
        ),
        (
            "ci_observation_digest_mismatch",
            attestation.observation_digest == ci_observation_digest(result),
        ),
        ("repository_mismatch", result.repository == repository == attestation.repository),
        (
            "workflow_path_mismatch",
            result.workflow_path == workflow_path == attestation.workflow_path,
        ),
        (
            "workflow_ref_mismatch",
            result.workflow_ref == workflow_ref == attestation.workflow_ref,
        ),
        (
            "event_name_mismatch",
            result.event_name == event_name == attestation.event_name,
        ),
        ("pr_number_mismatch", result.pr_number == pr_number == attestation.pr_number),
        ("run_id_mismatch", result.run_id == run_id == attestation.run_id),
        (
            "run_attempt_mismatch",
            result.run_attempt == run_attempt == attestation.run_attempt,
        ),
        ("head_mismatch", result.head_sha == head_sha == attestation.head_sha),
        ("base_mismatch", result.base_sha == base_sha == attestation.base_sha),
        (
            "artifact_id_mismatch",
            artifact_id == attestation.artifact_id,
        ),
        (
            "artifact_digest_mismatch",
            artifact_digest == attestation.artifact_digest,
        ),
        (
            "result_schema_mismatch",
            result.schema_version == attestation.result_schema_version,
        ),
        (
            "patch_mismatch",
            result.patch_fingerprint
            == patch_fingerprint
            == attestation.patch_fingerprint,
        ),
        (
            "requirement_contract_mismatch",
            result.requirement_contract_hash
            == requirement_contract_hash
            == attestation.requirement_contract_hash,
        ),
        ("command_argv_mismatch", result.command_argv == attestation.command_argv),
        ("output_hash_mismatch", result.output_hash == attestation.output_hash),
        (
            "coverage_ids_invalid",
            result.requirement_ids == attestation.requirement_ids
            and result.hunk_ids == attestation.hunk_ids
            and result.file_change_ids == attestation.file_change_ids
            and set(result.requirement_ids) <= requirement_ids
            and set(result.hunk_ids) <= hunk_ids
            and set(result.file_change_ids) <= file_change_ids,
        ),
        (
            "started_at_mismatch",
            result.started_at == attestation.started_at,
        ),
        (
            "finished_at_mismatch",
            result.finished_at == attestation.finished_at,
        ),
    ]
    failed = next((code for code, valid in bindings if not valid), None)
    if failed is None and public_key is None:
        failed = "ci_attestation_public_key_missing"
    if (
        failed is None
        and public_key is not None
        and not verify_ci_attestation_signature(attestation, public_key)
    ):
        failed = "ci_attestation_signature_invalid"
    assessed.trusted = failed is None
    assessed.trust_reason = "verified_attestation" if assessed.trusted else failed or "ci_untrusted"
    assessed.trust_source = (
        f"attestation:{attestation.issuer}"
        if assessed.trusted
        else "observation"
    )
    assessed.attestation_id = attestation.attestation_id
    assessed.attestation_issuer = attestation.issuer
    assessed.provenance = assessed.provenance.model_copy(
        update={
            "kind": "verified" if assessed.trusted else "captured",
            "source": (
                "ci_attestation" if assessed.trusted else "ci_observation"
            ),
        }
    )
    return assessed


def run_ci_manifest(
    manifest: CIManifest,
    *,
    repository: Path | None = None,
    private_key: bytes | None = None,
    signer: str = "",
) -> CIVerificationResult:
    if not manifest.command_argv:
        raise CIVerificationError("ci_command_empty")
    pre_snapshot = (
        validate_manifest_snapshot(repository, manifest)
        if repository is not None
        else None
    )
    started = datetime.now(timezone.utc)
    try:
        completed = subprocess.run(
            manifest.command_argv,
            capture_output=True,
            timeout=1800,
            check=False,
            cwd=repository,
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "CI": "true",
            },
        )
        output = completed.stdout + completed.stderr
        exit_code = completed.returncode
        result_status = "passed" if exit_code == 0 else "failed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        output = str(exc).encode()
        exit_code = None
        result_status = "aborted"
    finished = datetime.now(timezone.utc)
    post_snapshot = (
        measure_repository(repository, manifest.base_sha)
        if repository is not None
        else None
    )
    if (
        pre_snapshot is not None
        and post_snapshot is not None
        and (
            pre_snapshot.head_sha != post_snapshot.head_sha
            or pre_snapshot.tracked_tree_hash != post_snapshot.tracked_tree_hash
        )
    ):
        result_status = "aborted"
        exit_code = None
        output += b"\nci_workspace_mutated_during_verification"
    github_run_id = os.environ.get("GITHUB_RUN_ID")
    run_id = github_run_id or "ci_" + hashlib.sha256(
        (
            manifest.repository
            + manifest.head_sha
            + manifest.patch_fingerprint
            + started.isoformat()
        ).encode()
    ).hexdigest()[:24]
    try:
        run_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    except ValueError:
        run_attempt = 1
    result = CIVerificationResult(
        run_id=run_id,
        run_attempt=max(run_attempt, 1),
        provider=os.environ.get("GITHUB_ACTIONS") and "github_actions" or "local_ci",
        repository=manifest.repository,
        workflow_path=manifest.workflow_path,
        workflow_ref=manifest.workflow_ref,
        event_name=manifest.event_name,
        pr_number=manifest.pr_number,
        head_sha=pre_snapshot.head_sha if pre_snapshot else manifest.head_sha,
        base_sha=pre_snapshot.base_sha if pre_snapshot else manifest.base_sha,
        patch_fingerprint=(
            pre_snapshot.patch_fingerprint
            if pre_snapshot
            else manifest.patch_fingerprint
        ),
        requirement_contract_hash=manifest.requirement_contract_hash,
        command_argv=manifest.command_argv,
        result=result_status,
        exit_code=exit_code,
        environment={
            "ci": os.environ.get("CI", ""),
            "runner_os": os.environ.get("RUNNER_OS", ""),
        },
        started_at=started,
        finished_at=finished,
        output_hash=hashlib.sha256(output).hexdigest(),
        requirement_ids=manifest.requirement_ids,
        hunk_ids=manifest.hunk_ids,
        file_change_ids=manifest.file_change_ids,
        pre_snapshot=pre_snapshot,
        post_snapshot=post_snapshot,
        signer=signer,
        provenance=Provenance(kind="captured", source="ci_result"),
    )
    return (
        sign_ci_result(result, private_key, signer)
        if private_key is not None
        else result
    )


def load_key(path_or_value: str | None) -> bytes | None:
    if not path_or_value:
        return None
    if "\n" in path_or_value or path_or_value.startswith("-----BEGIN"):
        return path_or_value.encode()
    candidate = Path(path_or_value).expanduser()
    try:
        return (
            candidate.read_bytes()
            if candidate.is_file()
            else path_or_value.encode()
        )
    except OSError:
        return path_or_value.encode()
