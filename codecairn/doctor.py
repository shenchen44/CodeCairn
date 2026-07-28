from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from codecairn.github.publishing import (
    GitHubIntegrationError,
    GitHubPublicationService,
    resolve_github_credential,
)
from codecairn.review.capture import default_capture_root
from codecairn.review.store import default_review_root
from codecairn.review.trust_policy import (
    TRUST_POLICY_PATH,
    TrustPolicyError,
    parse_trust_policy,
)


def _command_status(command: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "unavailable"
    output = (result.stdout or result.stderr).strip().splitlines()
    return result.returncode == 0, output[0][:160] if output else ""


def _directory_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".doctor-", dir=path
        )
        os.close(descriptor)
        Path(temporary).unlink()
        return True, str(path)
    except OSError:
        return False, "not_writable"


def run_doctor(
    repo: Path,
    *,
    api_base: str,
    strict: bool = False,
    operation: str | None = None,
) -> tuple[dict, int]:
    repo = repo.expanduser().resolve()
    checks: dict[str, dict[str, object]] = {}
    git_ok, branch = _command_status(
        ["git", "branch", "--show-current"], repo
    )
    inside_ok, _ = _command_status(
        ["git", "rev-parse", "--is-inside-work-tree"], repo
    )
    checks["git"] = {
        "ok": git_ok and inside_ok and bool(branch),
        "category": "core",
        "repository": inside_ok,
        "branch": branch or "(detached/unavailable)",
    }
    try:
        credential = resolve_github_credential()
        account = asyncio.run(
            GitHubPublicationService(
                credential.token, api_base=api_base
            ).request("GET", "/user")
        )
        checks["github"] = {
            "ok": True,
            "category": "integrations",
            "credential_source": credential.source,
            "login": str(account.get("login", "")),
        }
    except GitHubIntegrationError as exc:
        checks["github"] = {
            "ok": False,
            "category": "integrations",
            "error": exc.code,
        }
    docker_ok, docker_detail = _command_status(
        ["docker", "info", "--format", "{{.ServerVersion}}"]
    )
    checks["docker"] = {
        "ok": docker_ok,
        "category": "integrations",
        "version": docker_detail,
    }
    node_ok, node_detail = _command_status(["node", "--version"])
    checks["node"] = {
        "ok": node_ok,
        "category": "integrations",
        "version": node_detail,
    }
    checks["python"] = {
        "ok": True,
        "category": "core",
        "version": ".".join(str(item) for item in sys.version_info[:3]),
    }
    try:
        from PIL import Image

        output = BytesIO()
        Image.new("RGB", (1, 1), "white").save(output, format="PNG")
        png_ok = output.getvalue().startswith(b"\x89PNG\r\n\x1a\n")
    except Exception:
        png_ok = False
    checks["png_renderer"] = {
        "ok": png_ok,
        "category": "core",
        "renderer": "pillow" if png_ok else "unavailable",
    }
    policy_path = repo / TRUST_POLICY_PATH
    try:
        policy = parse_trust_policy(policy_path.read_bytes())
        checks["ci_trust_policy"] = {
            "ok": True,
            "category": "integrations",
            "policy_hash": policy.policy_hash,
            "attestation_configured": bool(
                policy.trusted_attestation_issuers and policy.public_keys
            ),
        }
        checks["attestation"] = {
            "ok": bool(
                policy.trusted_attestation_issuers and policy.public_keys
            ),
            "category": "integrations",
            "configured": bool(policy.trusted_attestation_issuers),
        }
    except (OSError, TrustPolicyError):
        checks["ci_trust_policy"] = {
            "ok": False,
            "category": "integrations",
            "error": "ci_trust_policy_missing_or_invalid",
        }
        checks["attestation"] = {
            "ok": False,
            "category": "integrations",
            "configured": False,
        }
    capture_ok, capture_detail = _directory_writable(default_capture_root())
    review_ok, review_detail = _directory_writable(default_review_root())
    checks["capture_directory"] = {
        "ok": capture_ok,
        "category": "core",
        "detail": capture_detail,
    }
    checks["review_directory"] = {
        "ok": review_ok,
        "category": "core",
        "detail": review_detail,
    }
    operation_requirements = {
        None: set(),
        "review": set(),
        "github-publish": {"github"},
        "ci-import": {"github", "ci_trust_policy", "attestation"},
    }
    required_by_operation = operation_requirements.get(operation, set())
    failures = []
    for name, check in checks.items():
        core = check["category"] == "core"
        blocking = core or name in required_by_operation
        check["blocking"] = blocking
        check["optional"] = not blocking
        if not check["ok"] and (strict or blocking):
            failures.append(name)
    all_ready = all(bool(item["ok"]) for item in checks.values())
    return {
        "status": "healthy" if all_ready else "degraded",
        "operation": operation or "default",
        "strict": strict,
        "blocking_failures": failures,
        "checks": checks,
    }, 1 if failures else 0
