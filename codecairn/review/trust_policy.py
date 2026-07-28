from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


TRUST_POLICY_PATH = "codecairn-trust.toml"


class TrustPolicyError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CITrustPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    allowed_repository: str
    allowed_workflow_paths: list[str] = Field(default_factory=list)
    allowed_workflow_refs: list[str] = Field(default_factory=list)
    allowed_event_types: list[str] = Field(default_factory=list)
    trusted_attestation_issuers: list[str] = Field(default_factory=list)
    public_keys: dict[str, str] = Field(default_factory=dict)
    artifact_name: str = "codecairn-ci-result"
    maximum_result_age_seconds: int = Field(default=86400, ge=1)
    allow_local_manual_signing: bool = False

    @property
    def policy_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(canonical).hexdigest()


def parse_trust_policy(data: bytes) -> CITrustPolicy:
    try:
        return CITrustPolicy.model_validate(tomllib.loads(data.decode()))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise TrustPolicyError("ci_trust_policy_invalid") from exc


def load_trust_policy_from_base(
    repository: Path, base_revision: str
) -> CITrustPolicy:
    """Read policy from the trusted base commit, never from the PR worktree."""
    completed = subprocess.run(
        ["git", "-C", str(repository), "show", f"{base_revision}:{TRUST_POLICY_PATH}"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise TrustPolicyError("ci_trust_policy_missing")
    return parse_trust_policy(completed.stdout)
