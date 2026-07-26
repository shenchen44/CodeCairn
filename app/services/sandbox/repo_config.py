from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(slots=True)
class RepoConfig:
    language: str = "python"
    test_command: str = "pytest -q"
    install_command: str = "pip install -r requirements.txt"
    allowed_paths: list[str] = field(default_factory=lambda: ["app/", "src/", "tests/"])
    blocked_paths: list[str] = field(default_factory=lambda: [".github/", "infra/", "deploy/", "migrations/"])
    max_changed_files: int = 5
    max_diff_lines: int = 200


# --- Command validation: whitelist approach ---

# Allowed command prefixes (whitelist)
ALLOWED_COMMAND_PREFIXES = {
    "pip install",
    "pip3 install",
    "python -m pip",
    "python3 -m pip",
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "tox",
    "make test",
    "make install",
    "make check",
    "python setup.py",
    "python -m build",
    "flit build",
    "poetry install",
    "poetry run pytest",
    "uv pip",
    "uv sync",
    "hatch run",
    "npm install",
    "npm ci",
    "npm test",
    "npm run",
    "npx ",
    "pnpm install",
    "pnpm test",
    "pnpm run",
    "yarn install",
    "yarn test",
    "yarn run",
    "bun install",
    "bun test",
    "bun run",
    "cargo fetch",
    "cargo test",
    "cargo check",
    "cargo clippy",
    "go mod download",
    "go test",
    "go vet",
    "mvn test",
    "mvn verify",
    "./mvnw test",
    "./mvnw verify",
    "gradle test",
    "./gradlew test",
    "bundle install",
    "bundle exec",
}

# Explicitly blocked dangerous patterns (defense-in-depth)
DANGEROUS_PATTERNS = {
    "rm -rf",
    "shutdown",
    "reboot",
    "mkfs",
    "dd ",
    "chmod 777",
    "curl |",
    "wget |",
    "> /dev/",
    "eval(",
    "exec(",
    "__import__",
    "subprocess",
    "os.system",
    "os.popen",
    "pty.spawn",
    "/etc/shadow",
    "/etc/passwd",
    "base64",
}


def validate_command(command: str) -> None:
    """Validate that a command is safe to run in the sandbox.

    Uses a whitelist approach: the command must start with an allowed prefix.
    Additionally, explicitly blocks known dangerous patterns as defense-in-depth.
    """
    normalized = command.strip().lower()

    if not normalized:
        raise ValueError("empty command")

    # Defense-in-depth: check for dangerous patterns first
    for pattern in DANGEROUS_PATTERNS:
        if pattern in normalized:
            raise ValueError(f"dangerous command rejected: pattern '{pattern}' found")

    # Whitelist check: command must start with an allowed prefix
    # Support compound commands with && (each part must be safe)
    parts = [p.strip() for p in normalized.split("&&")]
    for part in parts:
        if not part:
            continue
        if not any(part.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES):
            raise ValueError(
                f"command not in whitelist: '{part[:60]}...' "
                f"Allowed prefixes: {sorted(ALLOWED_COMMAND_PREFIXES)}"
            )


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


def load_repo_config(repo_path: Path) -> RepoConfig:
    config_path = repo_path / ".agent.yml"
    if not config_path.exists():
        config = _detected_config(repo_path)
    else:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config = RepoConfig(
            language=data.get("language", "python"),
            test_command=data.get("test_command", "pytest -q"),
            install_command=data.get("install_command", "pip install -r requirements.txt"),
            allowed_paths=data.get("allowed_paths", ["app/", "src/", "tests/"]),
            blocked_paths=data.get("blocked_paths", [".github/", "infra/", "deploy/", "migrations/"]),
            max_changed_files=int(data.get("max_changed_files", 5)),
            max_diff_lines=int(data.get("max_diff_lines", 200)),
        )
    validate_command(config.install_command)
    validate_command(config.test_command)
    return config
