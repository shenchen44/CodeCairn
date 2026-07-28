from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from codecairn.github import GitHubApiService
from codecairn.github.auth import GitHubAuthService


CHANGE_PROOF_START = "<!-- codecairn:change-proof -->"
CHANGE_PROOF_END = "<!-- /codecairn:change-proof -->"


class GitHubIntegrationError(RuntimeError):
    def __init__(
        self, code: str, message: str = "", *, details: dict | None = None
    ) -> None:
        super().__init__(f"{code}{': ' + message if message else ''}")
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class GitHubCredential:
    token: str
    source: str


def resolve_github_credential() -> GitHubCredential:
    for name in ("CODECAIRN_GITHUB_TOKEN", "GITHUB_TOKEN"):
        if token := os.environ.get(name):
            return GitHubCredential(token=token, source=name)
    installation = os.environ.get("CODECAIRN_GITHUB_INSTALLATION_ID")
    if installation:
        import asyncio

        try:
            token = asyncio.run(
                GitHubAuthService().get_installation_token(int(installation))
            )
            return GitHubCredential(token=token, source="github_app")
        except Exception as exc:
            raise GitHubIntegrationError(
                "github_app_auth_failed"
            ) from exc
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except OSError:
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return GitHubCredential(token=result.stdout.strip(), source="gh")
    raise GitHubIntegrationError("github_auth_unavailable")


def validate_api_base(value: str) -> str:
    parsed = urlsplit(value)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise GitHubIntegrationError("github_insecure_api_base")
    return value.rstrip("/")


def parse_github_remote(value: str) -> str:
    value = value.strip()
    match = re.match(
        r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
        r"([^/]+)/([^/]+?)(?:\.git)?$",
        value,
    )
    if not match:
        raise GitHubIntegrationError("github_remote_unsupported")
    return f"{match.group(1)}/{match.group(2)}"


def repository_slug(repo: Path, override: str | None = None) -> str:
    if override:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", override):
            raise GitHubIntegrationError("github_repository_invalid")
        return override
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitHubIntegrationError("github_remote_missing")
    return parse_github_remote(result.stdout)


def replace_marker(original: str, generated: str) -> str:
    block = generated.strip()
    if CHANGE_PROOF_START not in block:
        block = f"{CHANGE_PROOF_START}\n{block}\n{CHANGE_PROOF_END}"
    pattern = re.compile(
        re.escape(CHANGE_PROOF_START)
        + r".*?"
        + re.escape(CHANGE_PROOF_END),
        re.DOTALL,
    )
    if pattern.search(original):
        return pattern.sub(lambda _: block, original, count=1)
    prefix = original.rstrip()
    return f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"


def check_conclusion(
    *, gate: str, assurance: str, reasons: list[str],
    verification_failed: bool = False, head_mismatch: bool = False
) -> str:
    integrity = {
        "ledger_integrity_failed",
        "capture_integrity_failed",
        "ledger_capture_hash_binding_failed",
        "change_proof_stale",
    }
    if head_mismatch or integrity.intersection(reasons):
        return "action_required"
    if verification_failed:
        return "failure"
    if gate == "passed" and assurance == "high":
        return "success"
    return "neutral"


def validate_pr_binding(
    *,
    repository: str,
    local_head_sha: str,
    proof_base_sha: str,
    proof_base_ref: str,
    proof_patch_fingerprint: str,
    current_patch_fingerprint: str,
    workspace_dirty: bool,
    change_proof_stale: bool,
    pull_request: dict,
) -> dict:
    remote_repository = str(
        pull_request.get("base", {}).get("repo", {}).get("full_name", "")
    )
    remote_head_sha = str(pull_request.get("head", {}).get("sha", ""))
    remote_base_sha = str(pull_request.get("base", {}).get("sha", ""))
    remote_base_ref = str(pull_request.get("base", {}).get("ref", ""))
    checks = {
        "repository": remote_repository.lower() == repository.lower(),
        "head_sha": remote_head_sha == local_head_sha,
        "base_sha": remote_base_sha == proof_base_sha,
        "base_ref": _base_ref_matches(proof_base_ref, remote_base_ref),
        "patch_fingerprint": (
            current_patch_fingerprint == proof_patch_fingerprint
        ),
        "workspace_clean": not workspace_dirty,
        "change_proof_current": not change_proof_stale,
    }
    details = {
        "repository": {
            "local": repository,
            "remote": remote_repository,
        },
        "head_sha": {
            "local": local_head_sha,
            "remote": remote_head_sha,
        },
        "base_sha": {
            "proof": proof_base_sha,
            "remote": remote_base_sha,
        },
        "base_ref": {
            "proof": proof_base_ref,
            "remote": remote_base_ref,
        },
        "patch_fingerprint": {
            "proof": proof_patch_fingerprint,
            "current": current_patch_fingerprint,
        },
        "checks": checks,
    }
    if not checks["repository"]:
        raise GitHubIntegrationError(
            "github_pr_repository_mismatch", details=details
        )
    if not checks["base_sha"] or not checks["base_ref"]:
        raise GitHubIntegrationError(
            "github_pr_base_mismatch", details=details
        )
    if not checks["head_sha"]:
        raise GitHubIntegrationError(
            "github_pr_head_mismatch", details=details
        )
    if not checks["workspace_clean"]:
        raise GitHubIntegrationError(
            "github_publish_dirty_workspace", details=details
        )
    if (
        not checks["change_proof_current"]
        or not checks["patch_fingerprint"]
    ):
        raise GitHubIntegrationError(
            "change_proof_stale", details=details
        )
    return details


def _base_ref_matches(proof_ref: str, remote_ref: str) -> bool:
    if proof_ref in {"HEAD", ""} or re.fullmatch(r"[0-9a-fA-F]{40}", proof_ref):
        return True
    normalized = proof_ref.removeprefix("refs/heads/").removeprefix("origin/")
    return normalized == remote_ref


class GitHubPublicationService(GitHubApiService):
    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_base: str | None = None,
        max_retries: int = 3,
        max_pages: int = 10,
        backoff_seconds: float = 0.5,
    ) -> None:
        super().__init__(token, client=client, api_base=api_base)
        self.max_retries = max(1, max_retries)
        self.max_pages = max(1, max_pages)
        self.backoff_seconds = max(0.0, backoff_seconds)

    async def _send(
        self, method: str, url: str, payload: dict | None = None
    ) -> httpx.Response:
        safe_base = validate_api_base(self.api_base)
        if not url.startswith(("http://", "https://")):
            url = f"{safe_base}{url}"
        parsed_url = urlsplit(url)
        parsed_base = urlsplit(safe_base)
        if (
            parsed_url.scheme != parsed_base.scheme
            or parsed_url.netloc != parsed_base.netloc
        ):
            raise GitHubIntegrationError("github_pagination_url_invalid")
        for attempt in range(self.max_retries):
            try:
                if self.client is not None:
                    response = await self.client.request(
                        method, url, headers=self._headers(), json=payload
                    )
                else:
                    async with httpx.AsyncClient(
                        timeout=30, follow_redirects=True
                    ) as client:
                        response = await client.request(
                            method, url, headers=self._headers(), json=payload
                        )
            except httpx.HTTPError as exc:
                if attempt + 1 == self.max_retries:
                    raise GitHubIntegrationError(
                        "github_network_error"
                    ) from exc
                await asyncio.sleep(
                    self.backoff_seconds * (2**attempt)
                )
                continue
            rate_limited = response.status_code == 429 or (
                response.status_code == 403
                and (
                    response.headers.get("x-ratelimit-remaining") == "0"
                    or "rate limit" in response.text.lower()
                )
            )
            server_error = 500 <= response.status_code <= 599
            if rate_limited or server_error:
                if attempt + 1 < self.max_retries:
                    retry_after = response.headers.get("retry-after")
                    try:
                        delay = float(retry_after) if retry_after else (
                            self.backoff_seconds * (2**attempt)
                        )
                    except ValueError:
                        delay = self.backoff_seconds * (2**attempt)
                    await asyncio.sleep(min(max(delay, 0.0), 30.0))
                    continue
                raise GitHubIntegrationError(
                    "github_rate_limited"
                    if rate_limited
                    else "github_server_error",
                    str(response.status_code),
                )
            if response.status_code >= 400:
                code = (
                    "github_checks_permission_denied"
                    if "/check-runs" in url
                    and response.status_code in {401, 403}
                    else "github_api_error"
                )
                raise GitHubIntegrationError(
                    code, str(response.status_code)
                )
            return response
        raise GitHubIntegrationError("github_retry_exhausted")

    async def request(
        self, method: str, path: str, payload: dict | None = None
    ) -> dict | list:
        try:
            response = await self._send(method, path, payload)
            return response.json()
        except ValueError as exc:
            raise GitHubIntegrationError("github_response_invalid") from exc

    async def request_bytes(
        self, path: str, *, max_bytes: int = 8 * 1024 * 1024
    ) -> bytes:
        content, _ = await self.download_stream(path, max_bytes=max_bytes)
        return content

    async def download_stream(
        self, path: str, *, max_bytes: int = 8 * 1024 * 1024
    ) -> tuple[bytes, str]:
        """Download with a hard cap and strip auth on cross-host redirects."""
        safe_base = validate_api_base(self.api_base)
        url = path if path.startswith(("http://", "https://")) else f"{safe_base}{path}"
        api = urlsplit(safe_base)
        current = urlsplit(url)
        if (current.scheme, current.netloc) != (api.scheme, api.netloc):
            raise GitHubIntegrationError("github_artifact_url_invalid")
        client = self.client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        owns_client = self.client is None
        try:
            for _ in range(4):
                target = urlsplit(url)
                same_api = (target.scheme, target.netloc) == (
                    api.scheme,
                    api.netloc,
                )
                if target.scheme != "https" and not (
                    target.hostname in {"127.0.0.1", "localhost", "::1"}
                    and target.scheme == "http"
                ):
                    raise GitHubIntegrationError("github_artifact_url_invalid")
                request = client.build_request(
                    "GET",
                    url,
                    headers=self._headers() if same_api else {},
                )
                response = await client.send(request, stream=True)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    await response.aclose()
                    if not location:
                        raise GitHubIntegrationError(
                            "github_artifact_redirect_invalid"
                        )
                    url = str(httpx.URL(url).join(location))
                    continue
                if response.status_code >= 400:
                    await response.aclose()
                    raise GitHubIntegrationError(
                        "github_api_error", str(response.status_code)
                    )
                digest = hashlib.sha256()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        await response.aclose()
                        raise GitHubIntegrationError(
                            "github_response_too_large"
                        )
                    digest.update(chunk)
                    chunks.append(chunk)
                await response.aclose()
                return b"".join(chunks), "sha256:" + digest.hexdigest()
            raise GitHubIntegrationError("github_artifact_redirect_limit")
        finally:
            if owns_client:
                await client.aclose()

    async def paginate(
        self, path: str, *, item_key: str | None = None
    ) -> list[dict]:
        items: list[dict] = []
        next_url = path
        for _ in range(self.max_pages):
            response = await self._send("GET", next_url)
            try:
                payload = response.json()
            except ValueError as exc:
                raise GitHubIntegrationError(
                    "github_response_invalid"
                ) from exc
            page = payload.get(item_key, []) if item_key else payload
            if not isinstance(page, list):
                raise GitHubIntegrationError("github_response_invalid")
            items.extend(item for item in page if isinstance(item, dict))
            next_url = _next_link(response.headers.get("link", ""))
            if not next_url:
                return items
        raise GitHubIntegrationError("github_pagination_limit_exceeded")


def _next_link(value: str) -> str:
    for part in value.split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', part)
        if match and match.group(2) == "next":
            return match.group(1)
    return ""
