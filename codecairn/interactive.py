from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from app.services.adapters import interactive_task
from app.services.agent_runtime import (
    AgentSession,
    CodingTask,
    ExtensionEvent,
    ExtensionManager,
    ExtensionResult,
    TaskIntent,
)
from app.core.config import get_settings
from app.services.openai.agent_loop import AgentRunResult
from app.services.openai.policy import RuntimePolicy
from app.services.openai.staged_runtime import StagedAgentRuntime
from app.services.openai.tools import AgentToolbox
from app.services.sandbox.git_ops import diff
from app.services.sandbox.repo_config import load_repo_config
from app.services.sandbox.runner import SandboxRunner
from codecairn import __version__


def _run_git(
    repo_path: Path,
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )


def resolve_repository(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError(f"repository_not_found:{candidate}")
    try:
        root = _run_git(
            candidate,
            "rev-parse",
            "--show-toplevel",
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise ValueError(
            f"not_a_git_repository:{candidate}: {details}"
        ) from exc
    return Path(root).resolve()


def default_session_path(repo_path: Path) -> Path:
    digest = hashlib.sha256(str(repo_path).encode("utf-8")).hexdigest()[:12]
    safe_name = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-"
        for char in repo_path.name
    )
    return (
        Path.home()
        / ".codecairn"
        / "sessions"
        / f"{safe_name}-{digest}.jsonl"
    )


def load_session(path: Path) -> AgentSession:
    if path.exists():
        return AgentSession.load(path)
    return AgentSession(path=path)


@dataclass(frozen=True, slots=True)
class WorkspaceTree:
    tree_id: str

    @classmethod
    def capture(cls, repo_path: Path) -> WorkspaceTree:
        descriptor, index_name = tempfile.mkstemp(
            prefix="codecairn-index-",
        )
        os.close(descriptor)
        Path(index_name).unlink(missing_ok=True)
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = index_name
        try:
            _run_git(repo_path, "read-tree", "HEAD", env=env)
            _run_git(repo_path, "add", "-A", env=env)
            tree_id = _run_git(
                repo_path,
                "write-tree",
                env=env,
            ).stdout.strip()
        finally:
            Path(index_name).unlink(missing_ok=True)
        return cls(tree_id=tree_id)


@dataclass(slots=True)
class UndoRecord:
    repo_path: Path
    before: WorkspaceTree
    after: WorkspaceTree
    objective: str

    def undo(self) -> None:
        current = WorkspaceTree.capture(self.repo_path)
        if current != self.after:
            raise RuntimeError(
                "workspace_changed_since_task; inspect /diff before undoing"
            )
        patch = _run_git(
            self.repo_path,
            "diff",
            "--binary",
            self.after.tree_id,
            self.before.tree_id,
        ).stdout
        if patch:
            _run_git(
                self.repo_path,
                "apply",
                "--whitespace=nowarn",
                "-",
                input_text=patch,
            )
        restored = WorkspaceTree.capture(self.repo_path)
        if restored != self.before:
            raise RuntimeError("workspace_undo_verification_failed")


class TerminalProgressExtension:
    def __init__(self, output: TextIO) -> None:
        self.output = output

    def on_event(
        self,
        event: ExtensionEvent,
    ) -> ExtensionResult | None:
        if event.name != "tool_call":
            return None
        name = str(event.payload.get("name") or "tool")
        detail = self._tool_detail(event.payload.get("arguments"))
        suffix = f" {detail}" if detail else ""
        self.output.write(f"  [{event.turn or '-'}] {name}{suffix}\n")
        self.output.flush()
        return None

    @staticmethod
    def _tool_detail(arguments: object) -> str:
        try:
            payload = json.loads(str(arguments or "{}"))
        except json.JSONDecodeError:
            return ""
        for key in ("path", "test_path", "query", "command"):
            value = payload.get(key)
            if value:
                compact = " ".join(str(value).split())
                return compact[:100]
        return ""


TaskExecutor = Callable[[CodingTask, AgentSession], AgentRunResult]


class InteractiveShell:
    def __init__(
        self,
        *,
        repo_path: Path,
        policy: RuntimePolicy,
        session: AgentSession,
        input_fn: Callable[[str], str] = input,
        output: TextIO = sys.stdout,
        task_executor: TaskExecutor | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.policy = policy
        self.session = session
        self.input_fn = input_fn
        self.output = output
        self.color = (
            bool(getattr(output, "isatty", lambda: False)())
            and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM") != "dumb"
        )
        self.default_intent = TaskIntent.change
        self.undo_stack: list[UndoRecord] = []
        self.repo_config = load_repo_config(repo_path)
        self.sandbox_runner = SandboxRunner()
        self._task_executor = task_executor
        extensions = ExtensionManager(
            [TerminalProgressExtension(output)]
        )
        self.runtime = StagedAgentRuntime(
            policy=policy,
            extensions=extensions,
        )

    def run(self) -> int:
        self._show_banner()
        while True:
            try:
                line = self.input_fn(
                    f"cairn[{self.default_intent.value}]> "
                ).strip()
            except EOFError:
                self._write("\n")
                return 0
            except KeyboardInterrupt:
                self._write("\n")
                continue
            if not line:
                continue
            if line.startswith("/"):
                if self._handle_command(line):
                    return 0
                continue
            self._execute(line, self.default_intent)

    def _handle_command(self, line: str) -> bool:
        command, _, argument = line.partition(" ")
        command = command.lower()
        argument = argument.strip()
        if command in {"/exit", "/quit"}:
            return True
        if command == "/help":
            self._show_help()
        elif command == "/status":
            self._show_status()
        elif command == "/diff":
            self._show_diff()
        elif command == "/test":
            self._run_tests()
        elif command == "/undo":
            self._undo()
        elif command == "/clear":
            self.session.head_id = None
            self.session.append("session_reset", {})
            self._write("Conversation context cleared.\n")
        elif command == "/mode":
            self._set_mode(argument)
        elif command in {
            "/change",
            "/review",
            "/investigate",
            "/explain",
        }:
            if not argument:
                self._write(f"Usage: {command} <task>\n")
            else:
                self._execute(
                    argument,
                    TaskIntent(command.removeprefix("/")),
                )
        else:
            self._write(f"Unknown command: {command}. Try /help.\n")
        return False

    def _execute(self, objective: str, intent: TaskIntent) -> None:
        before = WorkspaceTree.capture(self.repo_path)
        task = interactive_task(objective, intent=intent)
        self._write(f"Running {intent.value} task...\n")
        result: AgentRunResult | None = None
        try:
            result = self._execute_task(task)
        except KeyboardInterrupt:
            self._write("\nTask interrupted.\n")
        except Exception as exc:
            self._write(f"Task failed: {exc}\n")
        finally:
            after = WorkspaceTree.capture(self.repo_path)
            if after != before:
                self.undo_stack.append(
                    UndoRecord(
                        repo_path=self.repo_path,
                        before=before,
                        after=after,
                        objective=objective,
                    )
                )
                self._write("Workspace changed. Use /diff or /undo.\n")
        if result is not None:
            self._render_result(result)

    def _execute_task(self, task: CodingTask) -> AgentRunResult:
        if self._task_executor is not None:
            return self._task_executor(task, self.session)
        toolbox = AgentToolbox(
            repo_path=self.repo_path,
            repo_config=self.repo_config,
            task_context=task,
            sandbox_runner=self.sandbox_runner,
            runtime_policy=self.policy,
        )
        return self.runtime.run(toolbox, session=self.session)

    def _render_result(self, result: AgentRunResult) -> None:
        summary = result.summary
        if isinstance(summary, dict):
            status = summary.get("status")
            if status:
                self._write(f"\nStatus: {status}\n")
            rendered_keys: set[str] = set()
            for key in (
                "findings",
                "changes",
                "verification",
                "remaining_risks",
                "notes",
            ):
                value = summary.get(key)
                if not value:
                    continue
                rendered_keys.add(key)
                self._write(f"{key.replace('_', ' ').title()}:\n")
                items = value if isinstance(value, list) else [value]
                for item in items:
                    self._write(f"  - {item}\n")
            remaining = {
                key: value
                for key, value in summary.items()
                if key not in rendered_keys | {"status"} and value
            }
            if remaining:
                self._write(
                    json.dumps(
                        remaining,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
        else:
            self._write(f"\n{summary}\n")
        self._write(
            f"Calls: {result.model_call_count} model, "
            f"{result.tool_call_count} tool | "
            f"Tokens: {result.total_input_tokens} in, "
            f"{result.total_output_tokens} out\n"
        )

    def _show_help(self) -> None:
        self._write(
            "Commands:\n"
            "  /change <task>       modify code for one task\n"
            "  /review <task>       review without mutation tools\n"
            "  /investigate <task>  investigate without mutation tools\n"
            "  /explain <task>      explain code without mutation tools\n"
            "  /mode <intent>       set the default prompt intent\n"
            "  /diff                show workspace status and diff\n"
            "  /test                run the repository test command\n"
            "  /undo                undo the latest CodeCairn turn\n"
            "  /clear               clear conversation context\n"
            "  /status              show repository and session state\n"
            "  /exit                leave CodeCairn\n"
        )

    def _show_banner(self) -> None:
        settings = get_settings()
        branch = self._branch_label()
        path = self._display_path(self.repo_path)
        session = (
            self._display_path(self.session.path)
            if self.session.path
            else self.session.session_id
        )
        width = max(
            56,
            min(shutil.get_terminal_size(fallback=(88, 24)).columns, 110),
        )
        logo = [
            "       .----.",
            "      /______\\",
            "        .--.",
            "      _/____\\_",
            "     /________\\",
        ]
        info = [
            self._style(
                f"CodeCairn v{__version__}",
                "bold",
            ),
            (
                f"{settings.openai_model}  |  "
                f"{settings.openai_provider}  |  {self.policy.name}"
            ),
            f"{path}  |  {branch}  |  {self.repo_config.language}",
            (
                f"Sandbox: {settings.sandbox_base_image}  |  "
                f"Mode: {self.default_intent.value}"
            ),
            f"Session: {session}",
        ]
        self._write("\n")
        for index, line in enumerate(info):
            mark = self._style(logo[index], "accent")
            self._write(f"{mark}    {line}\n")
        self._write(self._style("-" * width, "muted") + "\n")
        self._write(
            self._style(
                "Natural-language tasks are ready. /help lists commands.",
                "muted",
            )
            + "\n\n"
        )

    def _show_status(self) -> None:
        settings = get_settings()
        status = _run_git(
            self.repo_path,
            "status",
            "--short",
        ).stdout.strip()
        self._write(
            f"Repository: {self.repo_path}\n"
            f"Branch: {self._branch_label()}\n"
            f"Model: {settings.openai_model}\n"
            f"Provider: {settings.openai_provider}\n"
            f"Runtime: {self.policy.name}\n"
            f"Language: {self.repo_config.language}\n"
            f"Test command: {self.repo_config.test_command}\n"
            f"Sandbox: {settings.sandbox_base_image}\n"
            f"Mode: {self.default_intent.value}\n"
            f"Session: {self.session.path or self.session.session_id}\n"
            f"Workspace: {status or 'clean'}\n"
        )

    def _show_diff(self) -> None:
        status = _run_git(
            self.repo_path,
            "status",
            "--short",
        ).stdout
        patch = diff(self.repo_path)
        self._write(status or "Workspace clean.\n")
        if patch:
            self._write(patch)
            if not patch.endswith("\n"):
                self._write("\n")

    def _run_tests(self) -> None:
        command = self.repo_config.test_command
        self._write(f"Running: {command}\n")
        result = self.sandbox_runner.run_tests(
            self.repo_path,
            command,
        )
        if result.stdout:
            self._write(result.stdout)
        if result.stderr:
            self._write(result.stderr)
        self._write(f"Tests exited with code {result.exit_code}.\n")

    def _undo(self) -> None:
        if not self.undo_stack:
            self._write("Nothing to undo in this terminal session.\n")
            return
        record = self.undo_stack[-1]
        try:
            record.undo()
        except Exception as exc:
            self._write(f"Undo refused: {exc}\n")
            return
        self.undo_stack.pop()
        self._write(f"Undid: {record.objective}\n")

    def _set_mode(self, argument: str) -> None:
        try:
            self.default_intent = TaskIntent(argument)
        except ValueError:
            choices = ", ".join(item.value for item in TaskIntent)
            self._write(f"Mode must be one of: {choices}\n")
            return
        self._write(f"Default mode: {self.default_intent.value}\n")

    def _branch_label(self) -> str:
        try:
            branch = _run_git(
                self.repo_path,
                "branch",
                "--show-current",
            ).stdout.strip()
            if not branch:
                branch = _run_git(
                    self.repo_path,
                    "rev-parse",
                    "--short",
                    "HEAD",
                ).stdout.strip()
            dirty = bool(
                _run_git(
                    self.repo_path,
                    "status",
                    "--porcelain",
                ).stdout
            )
            return f"{branch}{'*' if dirty else ''}"
        except subprocess.CalledProcessError:
            return "unknown"

    @staticmethod
    def _display_path(path: Path | None) -> str:
        if path is None:
            return ""
        resolved = path.expanduser().resolve()
        try:
            relative = resolved.relative_to(Path.home())
        except ValueError:
            return str(resolved)
        return f"~/{relative}"

    def _style(self, text: str, role: str) -> str:
        if not self.color:
            return text
        codes = {
            "bold": "\033[1;97m",
            "accent": "\033[38;5;37m",
            "muted": "\033[38;5;244m",
        }
        return f"{codes.get(role, '')}{text}\033[0m"

    def _write(self, text: str) -> None:
        self.output.write(text)
        self.output.flush()
