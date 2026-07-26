import subprocess
from pathlib import Path
from pathlib import PurePosixPath


IGNORED_GENERATED_PATHS = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _error_details(exc: subprocess.CalledProcessError, context: str) -> str:
    """Extract error details from a subprocess exception."""
    stderr = (exc.stderr or "").strip()
    stdout = (exc.stdout or "").strip()
    return stderr or stdout or f"{context} failed with exit code {exc.returncode}"


def run_git(repo_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=check,
    )


def clone_repo(clone_url: str, destination: Path) -> None:
    try:
        subprocess.run(["git", "clone", clone_url, str(destination)], text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        details = stderr or stdout or f"git clone failed with exit code {exc.returncode}"
        raise RuntimeError(details) from exc


def set_remote_url(repo_path: Path, remote_name: str, remote_url: str) -> None:
    try:
        subprocess.run(
            ["git", "remote", "set-url", remote_name, remote_url],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        if "No such remote" in (exc.stderr or "").strip() or "No such remote" in (exc.stdout or "").strip():
            try:
                subprocess.run(
                    ["git", "remote", "add", remote_name, remote_url],
                    cwd=repo_path,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                return
            except subprocess.CalledProcessError as add_exc:
                raise RuntimeError(f"git_remote_set_url_failed: {_error_details(add_exc, 'git remote add')}") from add_exc
        raise RuntimeError(f"git_remote_set_url_failed: {_error_details(exc, 'git remote set-url')}") from exc


def checkout_new_branch(repo_path: Path, branch_name: str, base_branch: str) -> str:
    run_git(repo_path, "checkout", base_branch)
    run_git(repo_path, "pull", "origin", base_branch)
    base_commit = run_git(repo_path, "rev-parse", "HEAD").stdout.strip()
    run_git(repo_path, "checkout", "-b", branch_name)
    return base_commit


def current_head(repo_path: Path) -> str:
    return run_git(repo_path, "rev-parse", "HEAD").stdout.strip()


def diff(repo_path: Path) -> str:
    return run_git(repo_path, "diff", "--binary").stdout


def resolve_repo_path(repo_path: Path, relative_path: str) -> Path:
    candidate = (repo_path / relative_path).resolve()
    repo_root = repo_path.resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"path_outside_repo:{relative_path}") from exc
    return candidate


def write_tracked_file(repo_path: Path, relative_path: str, content: str) -> None:
    file_path = resolve_repo_path(repo_path, relative_path)
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"tracked_file_missing:{relative_path}")
    tracked_files = set(list_files(repo_path))
    normalized = relative_path.replace("\\", "/")
    if normalized not in tracked_files:
        raise FileNotFoundError(f"tracked_file_missing:{relative_path}")
    file_path.write_text(content, encoding="utf-8")


def replace_in_tracked_file(repo_path: Path, relative_path: str, old_text: str, new_text: str) -> None:
    file_path = resolve_repo_path(repo_path, relative_path)
    content = file_path.read_text(encoding="utf-8")
    if old_text not in content:
        raise ValueError(f"old_text_not_found:{relative_path}")
    write_tracked_file(repo_path, relative_path, content.replace(old_text, new_text, 1))


def _run_git_apply(repo_path: Path, patch_text: str, reverse: bool = False) -> None:
    command = ["git", "apply", "--whitespace=nowarn", "--recount", "--ignore-space-change", "--ignore-whitespace"]
    if reverse:
        command.append("-R")
    command.append("-")
    subprocess.run(
        command,
        cwd=repo_path,
        input=patch_text,
        text=True,
        capture_output=True,
        check=True,
    )


def apply_patch(repo_path: Path, patch_text: str) -> None:
    try:
        _run_git_apply(repo_path, patch_text)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"git_apply_failed: {_error_details(exc, 'git apply')}") from exc


def reverse_patch(repo_path: Path, patch_text: str) -> None:
    try:
        _run_git_apply(repo_path, patch_text, reverse=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"git_apply_reverse_failed: {_error_details(exc, 'git apply reverse')}") from exc


def is_generated_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    pure_path = PurePosixPath(normalized)
    if any(part in IGNORED_GENERATED_PATHS for part in pure_path.parts):
        return True
    return pure_path.suffix in IGNORED_SUFFIXES


def _filter_committable_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    filtered: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/").strip("/")
        if not normalized or is_generated_path(normalized) or normalized in seen:
            continue
        seen.add(normalized)
        filtered.append(normalized)
    return filtered


def commit_all(repo_path: Path, message: str, include_paths: list[str] | None = None) -> str:
    run_git(repo_path, "config", "user.email", "codecairn@users.noreply.github.com")
    run_git(repo_path, "config", "user.name", "CodeCairn")
    run_git(repo_path, "restore", "--staged", ".")
    paths_to_stage = _filter_committable_paths(include_paths or [])
    if not paths_to_stage:
        raise RuntimeError("no_committable_files")
    run_git(repo_path, "add", "-A", "--", *paths_to_stage)
    run_git(repo_path, "commit", "-m", message)
    return current_head(repo_path)


def push_branch(repo_path: Path, branch_name: str) -> None:
    try:
        subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"git_push_failed: {_error_details(exc, 'git push')}") from exc


def list_files(repo_path: Path) -> list[str]:
    result = run_git(repo_path, "ls-files")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
