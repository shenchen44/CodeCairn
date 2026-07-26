import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings


@dataclass(slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


class SandboxRunner:
    """Runs install and test commands inside a disposable Docker container.

    Security features:
    - Network isolation (--network=none) prevents data exfiltration during tests
    - Install commands may use network access to resolve package dependencies
    - Memory and CPU limits prevent resource abuse
    - Timeout prevents hanging processes
    - Commands are validated before execution (see repo_config.validate_command)
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    def _venv_activate_prefix(self) -> str:
        return ". .venv/bin/activate"

    def _build_shell_command(self, command: str, *, create_venv: bool) -> str:
        steps = ["cd /workspace"]
        if create_venv:
            steps.append("python -m venv .venv")
            steps.append(self._venv_activate_prefix())
            steps.append("python -m pip install --upgrade pip")
        else:
            steps.append(
                "if [ -f .venv/bin/activate ]; then "
                f"{self._venv_activate_prefix()}; fi"
            )
        steps.append(command)
        return " && ".join(steps)

    def _resolve_mount_path(self, repo_path: Path) -> Path:
        host_root = self.settings.docker_bind_host_root
        container_root = self.settings.docker_bind_container_root
        if not host_root or not container_root:
            return repo_path

        try:
            relative_path = repo_path.resolve().relative_to(Path(container_root))
        except ValueError:
            return repo_path

        return Path(host_root).expanduser().resolve() / relative_path

    def run(
        self,
        repo_path: Path,
        command: str,
        *,
        create_venv: bool = False,
        allow_network: bool = False,
    ) -> CommandResult:
        shell_command = self._build_shell_command(command, create_venv=create_venv)
        mount_path = self._resolve_mount_path(repo_path)
        command_args = [
            "docker",
            "run",
            "--rm",
            # Resource limits
            "--memory",
            self.settings.sandbox_memory_limit,
            "--cpus",
            str(self.settings.sandbox_cpu_limit),
        ]
        if not allow_network:
            # Security: network isolation prevents data exfiltration during tests
            command_args.append("--network=none")

        command_args.extend(
            [
                # Security: read-only root filesystem (workspace is mounted writable)
                "--read-only",
                # Mount workspace as writable
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=100m",
                "-v",
                f"{mount_path}:/workspace:rw",
                self.settings.sandbox_base_image,
                "sh",
                "-lc",
                shell_command,
            ]
        )
        process = subprocess.run(
            command_args,
            text=True,
            capture_output=True,
            timeout=self.settings.sandbox_timeout_seconds,
            check=False,
        )
        return CommandResult(exit_code=process.returncode, stdout=process.stdout, stderr=process.stderr)

    def install_dependencies(self, repo_path: Path, install_command: str) -> CommandResult:
        normalized = install_command.strip().lower()
        create_venv = normalized.startswith(
            (
                "pip ",
                "pip3 ",
                "python -m pip",
                "python3 -m pip",
                "poetry ",
                "uv ",
                "hatch ",
            )
        )
        return self.run(
            repo_path,
            install_command,
            create_venv=create_venv,
            allow_network=True,
        )

    def run_tests(self, repo_path: Path, test_command: str) -> CommandResult:
        return self.run(repo_path, test_command, create_venv=False)
