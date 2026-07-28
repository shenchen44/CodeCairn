from __future__ import annotations

import subprocess
import os
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from codecairn.config import get_settings
from codecairn.verification.config import ParsedCommand, parse_command


@dataclass(slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    command_argv: list[str] = field(default_factory=list)
    network_enabled: bool = False
    language: str = ""
    image: str = ""
    toolchain: str = ""
    container_user: str = ""
    preflight_status: str = "not_run"


class VerificationEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str
    image: str
    toolchain: str
    supported: bool
    unsupported_reason: str | None = None
    user: str = ""


class UnsupportedEnvironment(RuntimeError):
    def __init__(self, environment: VerificationEnvironment) -> None:
        self.environment = environment
        super().__init__(
            f"unsupported_environment:{environment.language}:"
            f"{environment.unsupported_reason or 'no runtime'}"
        )


class WorkspacePermissionDenied(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        super().__init__(
            "workspace_permission_denied:"
            + (result.stderr or result.stdout or "container cannot write workspace")
        )


class SandboxRunner:
    """Execute validated argv directly in a hardened Docker sandbox.

    Avoiding a shell prevents command-string injection. Dependency installation
    still executes third-party package scripts and is therefore an explicit,
    network-enabled trust boundary.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    def _container_user(self) -> str:
        uid = (
            self.settings.sandbox_uid
            if self.settings.sandbox_uid is not None
            else getattr(os, "getuid", lambda: 1000)()
        )
        gid = (
            self.settings.sandbox_gid
            if self.settings.sandbox_gid is not None
            else getattr(os, "getgid", lambda: 1000)()
        )
        return f"{uid}:{gid}"

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

    def resolve_environment(self, repo_path: Path) -> VerificationEnvironment:
        if (repo_path / "package.json").exists():
            if (repo_path / "pnpm-lock.yaml").exists():
                return VerificationEnvironment(
                    language="javascript",
                    image="",
                    toolchain="pnpm",
                    supported=False,
                    unsupported_reason="pnpm runtime is not bundled",
                    user=self._container_user(),
                )
            if (repo_path / "yarn.lock").exists():
                return VerificationEnvironment(
                    language="javascript",
                    image="",
                    toolchain="yarn",
                    supported=False,
                    unsupported_reason="yarn runtime is not bundled",
                    user=self._container_user(),
                )
            if (repo_path / "bun.lockb").exists():
                return VerificationEnvironment(
                    language="javascript",
                    image="",
                    toolchain="bun",
                    supported=False,
                    unsupported_reason="bun runtime is not bundled",
                    user=self._container_user(),
                )
            return VerificationEnvironment(
                language="javascript",
                image="node:22-slim",
                toolchain="npm",
                supported=True,
                user=self._container_user(),
            )
        for marker, language in (
            ("Cargo.toml", "rust"),
            ("go.mod", "go"),
            ("pom.xml", "java"),
            ("build.gradle", "java"),
        ):
            if (repo_path / marker).exists():
                return VerificationEnvironment(
                    language=language,
                    image="",
                    toolchain=language,
                    supported=False,
                    unsupported_reason=(
                        f"{language} verification image is not configured"
                    ),
                    user=self._container_user(),
                )
        return VerificationEnvironment(
            language="python",
            image=self.settings.sandbox_base_image,
            toolchain="python",
            supported=True,
            user=self._container_user(),
        )

    @staticmethod
    def _python_argv(repo_path: Path, parsed: ParsedCommand) -> list[str]:
        venv_python = "/workspace/.venv/bin/python"
        host_venv_python = repo_path / ".venv" / "bin" / "python"
        # A container-created venv uses an absolute symlink whose target does
        # not exist on the host. The symlink itself is sufficient evidence.
        has_venv = host_venv_python.exists() or host_venv_python.is_symlink()
        python = venv_python if has_venv else "python"
        argv = parsed.argv
        if parsed.executable in {"pytest"}:
            return [python, "-m", "pytest", *argv[1:]]
        if parsed.executable in {"pip", "pip3"}:
            return [venv_python, "-m", "pip", *argv[1:]]
        if parsed.executable in {"python", "python3"}:
            return [python, *argv[1:]]
        return argv

    def _runtime_argv(
        self,
        repo_path: Path,
        parsed: ParsedCommand,
        environment: VerificationEnvironment,
    ) -> list[str]:
        if environment.language == "python":
            if parsed.executable not in {
                "python",
                "python3",
                "pip",
                "pip3",
                "pytest",
            }:
                raise UnsupportedEnvironment(
                    environment.model_copy(
                        update={
                            "supported": False,
                            "unsupported_reason": (
                                f"{parsed.executable} is not available in the "
                                "configured Python runtime"
                            ),
                        }
                    )
                )
            return self._python_argv(repo_path, parsed)
        if (
            environment.language == "javascript"
            and parsed.executable != "npm"
        ):
            raise UnsupportedEnvironment(
                environment.model_copy(
                    update={
                        "supported": False,
                        "unsupported_reason": (
                            f"{parsed.executable} does not match the "
                            "configured npm runtime"
                        ),
                    }
                )
            )
        return parsed.argv

    def _docker_run(
        self,
        repo_path: Path,
        argv: list[str],
        environment: VerificationEnvironment,
        *,
        allow_network: bool,
    ) -> CommandResult:
        mount_path = self._resolve_mount_path(repo_path)
        command = [
            "docker",
            "run",
            "--rm",
            "--memory",
            self.settings.sandbox_memory_limit,
            "--cpus",
            str(self.settings.sandbox_cpu_limit),
            "--pids-limit",
            str(self.settings.sandbox_pids_limit),
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            environment.user,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=100m",
            "--env",
            "HOME=/tmp",
            "--env",
            "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "--env",
            "npm_config_cache=/tmp/npm-cache",
        ]
        if not allow_network:
            command.append("--network=none")
        command.extend(
            [
                "-v",
                f"{mount_path}:/workspace:rw",
                "-w",
                "/workspace",
                environment.image,
                *argv,
            ]
        )
        process = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=self.settings.sandbox_timeout_seconds,
            check=False,
        )
        return CommandResult(
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            command_argv=argv,
            network_enabled=allow_network,
            language=environment.language,
            image=environment.image,
            toolchain=environment.toolchain,
            container_user=environment.user,
        )

    def _preflight_workspace(
        self,
        repo_path: Path,
        environment: VerificationEnvironment,
    ) -> None:
        if environment.language == "python":
            argv = [
                "python",
                "-c",
                (
                    "from pathlib import Path;"
                    "d=Path('.codecairn-write-probe');d.mkdir();"
                    "p=d/'temp';p.write_text('ok');p.unlink();d.rmdir()"
                ),
            ]
        else:
            argv = [
                "node",
                "-e",
                (
                    "const fs=require('fs'),d='.codecairn-write-probe',f=d+'/temp';"
                    "fs.mkdirSync(d);fs.writeFileSync(f,'ok');"
                    "fs.unlinkSync(f);fs.rmdirSync(d)"
                ),
            ]
        result = self._docker_run(
            repo_path, argv, environment, allow_network=False
        )
        if result.exit_code != 0:
            result.preflight_status = "failed"
            raise WorkspacePermissionDenied(result)

    def _environment_or_raise(
        self, repo_path: Path
    ) -> VerificationEnvironment:
        environment = self.resolve_environment(repo_path)
        if not environment.supported:
            raise UnsupportedEnvironment(environment)
        return environment

    def run(
        self,
        repo_path: Path,
        command: str,
        *,
        create_venv: bool = False,
        allow_network: bool = False,
    ) -> CommandResult:
        environment = self._environment_or_raise(repo_path)
        parsed = parse_command(command)
        self._preflight_workspace(repo_path, environment)
        if create_venv and environment.language == "python":
            bootstrap = self._docker_run(
                repo_path,
                ["python", "-m", "venv", "/workspace/.venv"],
                environment,
                allow_network=allow_network,
            )
            if bootstrap.exit_code != 0:
                bootstrap.preflight_status = "passed"
                return bootstrap
        argv = self._runtime_argv(repo_path, parsed, environment)
        result = self._docker_run(
            repo_path, argv, environment, allow_network=allow_network
        )
        result.preflight_status = "passed"
        return result

    def install_dependencies(
        self, repo_path: Path, install_command: str
    ) -> CommandResult:
        environment = self._environment_or_raise(repo_path)
        parsed = parse_command(install_command)
        self._preflight_workspace(repo_path, environment)
        if environment.language == "python":
            bootstrap = self._docker_run(
                repo_path,
                ["python", "-m", "venv", "/workspace/.venv"],
                environment,
                allow_network=True,
            )
            if bootstrap.exit_code != 0:
                bootstrap.preflight_status = "passed"
                return bootstrap
        argv = self._runtime_argv(repo_path, parsed, environment)
        result = self._docker_run(
            repo_path, argv, environment, allow_network=True
        )
        result.preflight_status = "passed"
        return result

    def run_tests(self, repo_path: Path, test_command: str) -> CommandResult:
        environment = self._environment_or_raise(repo_path)
        parsed = parse_command(test_command)
        self._preflight_workspace(repo_path, environment)
        argv = self._runtime_argv(repo_path, parsed, environment)
        result = self._docker_run(
            repo_path, argv, environment, allow_network=False
        )
        result.preflight_status = "passed"
        return result
