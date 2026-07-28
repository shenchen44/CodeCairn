from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from codecairn.review.analyzer import canonical_change_identity
from codecairn.review.models import CIManifest, CISnapshotMeasurement


class CIMeasurementError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _git(repository: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CIMeasurementError("ci_manifest_snapshot_mismatch")
    return completed.stdout


def tracked_tree_hash(repository: Path) -> str:
    """Hash only tracked worktree entries; untracked test outputs are ignored."""
    digest = hashlib.sha256()
    entries = _git(repository, "ls-files", "-z").split(b"\0")
    for raw_path in sorted(item for item in entries if item):
        relative = os.fsdecode(raw_path)
        path = repository / relative
        digest.update(raw_path)
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"missing\0")
    return digest.hexdigest()


def measure_repository(
    repository: Path, base_sha: str
) -> CISnapshotMeasurement:
    repository = repository.resolve()
    head_sha = _git(repository, "rev-parse", "HEAD").decode().strip()
    content_tree_hash, patch_fingerprint = canonical_change_identity(
        repository, base_sha
    )
    return CISnapshotMeasurement(
        head_sha=head_sha,
        base_sha=_git(
            repository, "rev-parse", f"{base_sha}^{{commit}}"
        ).decode().strip(),
        content_tree_hash=content_tree_hash,
        patch_fingerprint=patch_fingerprint,
        tracked_tree_hash=tracked_tree_hash(repository),
    )


def validate_manifest_snapshot(
    repository: Path, manifest: CIManifest
) -> CISnapshotMeasurement:
    measured = measure_repository(repository, manifest.base_sha)
    if (
        measured.head_sha != manifest.head_sha
        or measured.base_sha != manifest.base_sha
        or measured.patch_fingerprint != manifest.patch_fingerprint
    ):
        raise CIMeasurementError("ci_manifest_snapshot_mismatch")
    return measured
