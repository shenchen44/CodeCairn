"""Repository discovery helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path


def resolve_repository(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError(f"repository_not_found:{candidate}")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=candidate,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise ValueError(
            f"not_a_git_repository:{candidate}: {details}"
        ) from exc
    return Path(result.stdout.strip()).resolve()
