from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex

import yaml
from pydantic import BaseModel, ConfigDict, Field


@dataclass(slots=True)
class RepoConfig:
    language: str = "python"
    test_command: str = "python -m pytest -q"
    install_command: str = "pip install -r requirements.txt"
    allowed_paths: list[str] = field(default_factory=lambda: ["app/", "src/", "tests/"])
    blocked_paths: list[str] = field(default_factory=lambda: [".github/", "infra/", "deploy/", "migrations/"])
    max_changed_files: int = 5
    max_diff_lines: int = 200


class ParsedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    executable: str


SHELL_META = re.compile(r"(?:[;&|<>`\r\n]|\$\()")
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
ALLOWED_COMMANDS: dict[str, set[str] | None] = {
    "pip": {"install"},
    "pip3": {"install"},
    "python": {"-m"},
    "python3": {"-m"},
    "pytest": None,
    "tox": None,
    "make": {"test", "install", "check"},
    "flit": {"build"},
    "poetry": {"install", "run"},
    "uv": {"pip", "sync"},
    "hatch": {"run"},
    "npm": {"install", "ci", "test", "run"},
    "pnpm": {"install", "test", "run"},
    "yarn": {"install", "test", "run"},
    "bun": {"install", "test", "run"},
    "cargo": {"fetch", "test", "check", "clippy"},
    "go": {"mod", "test", "vet"},
    "mvn": {"test", "verify"},
    "./mvnw": {"test", "verify"},
    "gradle": {"test"},
    "./gradlew": {"test"},
    "bundle": {"install", "exec"},
}
PYTHON_MODULES = {"pip", "pytest", "build"}


def parse_command(command: str) -> ParsedCommand:
    """Parse untrusted config/UI input into a shell-free argv."""
    if not command.strip():
        raise ValueError("empty command")
    if SHELL_META.search(command):
        raise ValueError("shell syntax is not allowed")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(f"invalid command quoting: {exc}") from exc
    if not argv:
        raise ValueError("empty command")
    if ENV_ASSIGNMENT.match(argv[0]):
        raise ValueError("environment variable prefixes are not allowed")
    executable = argv[0]
    allowed_subcommands = ALLOWED_COMMANDS.get(executable)
    if executable not in ALLOWED_COMMANDS:
        raise ValueError(f"unsupported executable: {executable}")
    if allowed_subcommands is not None:
        if len(argv) < 2 or argv[1] not in allowed_subcommands:
            raise ValueError(
                f"unsupported subcommand for {executable}: "
                f"{argv[1] if len(argv) > 1 else '<missing>'}"
            )
    if executable in {"python", "python3"}:
        if len(argv) < 3 or argv[1] != "-m" or argv[2] not in PYTHON_MODULES:
            raise ValueError("only approved Python modules may be executed")
    if executable == "poetry" and argv[1] == "run":
        if len(argv) < 3 or argv[2] != "pytest":
            raise ValueError("only poetry run pytest is supported")
    return ParsedCommand(argv=argv, executable=executable)


def validate_command(command: str) -> None:
    parse_command(command)


def _detected_config(repo_path: Path) -> RepoConfig:
    if (repo_path / "package.json").exists():
        manager = "npm"
        if (repo_path / "pnpm-lock.yaml").exists():
            manager = "pnpm"
        elif (repo_path / "yarn.lock").exists():
            manager = "yarn"
        elif (repo_path / "bun.lockb").exists():
            manager = "bun"
        return RepoConfig(
            language="typescript/javascript",
            test_command=f"{manager} test",
            install_command=(
                f"{manager} install"
                if manager != "npm"
                else "npm install"
            ),
            allowed_paths=[""],
        )
    if (repo_path / "Cargo.toml").exists():
        return RepoConfig(
            language="rust",
            test_command="cargo test",
            install_command="cargo fetch",
            allowed_paths=[""],
        )
    if (repo_path / "go.mod").exists():
        return RepoConfig(
            language="go",
            test_command="go test ./...",
            install_command="go mod download",
            allowed_paths=[""],
        )
    if (repo_path / "pom.xml").exists():
        return RepoConfig(
            language="java",
            test_command="mvn test",
            install_command="mvn verify -DskipTests",
            allowed_paths=[""],
        )
    return RepoConfig()


def load_repo_config(
    repo_path: Path, *, validate: bool = True
) -> RepoConfig:
    config_path = repo_path / ".agent.yml"
    if not config_path.exists():
        config = _detected_config(repo_path)
    else:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config = RepoConfig(
            language=data.get("language", "python"),
            test_command=data.get("test_command", "python -m pytest -q"),
            install_command=data.get("install_command", "pip install -r requirements.txt"),
            allowed_paths=data.get("allowed_paths", ["app/", "src/", "tests/"]),
            blocked_paths=data.get("blocked_paths", [".github/", "infra/", "deploy/", "migrations/"]),
            max_changed_files=int(data.get("max_changed_files", 5)),
            max_diff_lines=int(data.get("max_diff_lines", 200)),
        )
    if validate:
        validate_command(config.install_command)
        validate_command(config.test_command)
    return config
