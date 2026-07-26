import io
import subprocess
from pathlib import Path

import pytest

from app.services.agent_runtime import (
    AgentSession,
    CodingTask,
    ExtensionEvent,
    TaskIntent,
)
from app.services.openai.agent_loop import AgentRunResult
from app.services.openai.policy import get_runtime_policy
from app.services.orchestration.contracts import RuntimeEvent
from codecairn import __version__
from codecairn.cli import build_parser, main
from codecairn.interactive import (
    InteractiveShell,
    TerminalTaskDisplay,
    UndoRecord,
    WorkspaceTree,
    default_session_path,
    resolve_repository,
)


def test_run_command_parses_general_coding_task_options():
    args = build_parser().parse_args(
        [
            "run",
            "--repo",
            "/tmp/project",
            "--intent",
            "review",
            "--objective",
            "Review cache invalidation",
        ]
    )

    assert args.command == "run"
    assert args.intent == "review"
    assert args.variant == "full"


def test_bare_command_defaults_to_interactive_current_directory():
    args = build_parser().parse_args([])

    assert args.command is None
    assert args.repo == "."
    assert args.variant == "full"


def test_chat_command_accepts_repository_and_session_options():
    args = build_parser().parse_args(
        [
            "chat",
            "--repo",
            "/tmp/project",
            "--session-file",
            "/tmp/session.jsonl",
        ]
    )

    assert args.command == "chat"
    assert args.repo == "/tmp/project"
    assert args.session_file == "/tmp/session.jsonl"


def test_version_uses_codecairn_brand(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"CodeCairn {__version__}"


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
    )
    (path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_workspace_checkpoint_restores_dirty_and_new_files(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("value = 2\n", encoding="utf-8")
    before = WorkspaceTree.capture(tmp_path)

    (tmp_path / "app.py").write_text("value = 3\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("created = True\n", encoding="utf-8")
    after = WorkspaceTree.capture(tmp_path)

    UndoRecord(
        repo_path=tmp_path,
        before=before,
        after=after,
        objective="change values",
    ).undo()

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not (tmp_path / "new.py").exists()


def test_undo_refuses_when_workspace_changed_after_task(tmp_path):
    _init_repo(tmp_path)
    before = WorkspaceTree.capture(tmp_path)
    (tmp_path / "app.py").write_text("value = 2\n", encoding="utf-8")
    after = WorkspaceTree.capture(tmp_path)
    (tmp_path / "app.py").write_text("value = 3\n", encoding="utf-8")

    record = UndoRecord(
        repo_path=tmp_path,
        before=before,
        after=after,
        objective="change value",
    )

    with pytest.raises(RuntimeError, match="workspace_changed_since_task"):
        record.undo()


def test_interactive_shell_runs_multiturn_command_and_undo(tmp_path):
    _init_repo(tmp_path)
    output = io.StringIO()
    commands = iter(
        [
            "change the value",
            "/review inspect the value",
            "/undo",
            "/exit",
        ]
    )
    observed_intents: list[TaskIntent] = []

    def execute(task, session):
        observed_intents.append(task.intent)
        if task.intent == TaskIntent.change:
            (tmp_path / "app.py").write_text(
                "value = 2\n",
                encoding="utf-8",
            )
        return AgentRunResult(
            summary={
                "status": "completed",
                "changes": [task.objective],
            },
            patch_text="",
            pr_title="",
            pr_body_summary={},
        )

    prompts: list[str] = []

    def read_input(prompt):
        prompts.append(prompt)
        return next(commands)

    shell = InteractiveShell(
        repo_path=tmp_path,
        policy=get_runtime_policy("full"),
        session=AgentSession(path=tmp_path / "session.jsonl"),
        input_fn=read_input,
        output=output,
        task_executor=execute,
    )

    assert shell.run() == 0
    assert observed_intents == [TaskIntent.change, TaskIntent.review]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    rendered = output.getvalue()
    assert f"CodeCairn v{__version__}" in rendered
    assert str(tmp_path) in rendered
    assert "Natural-language tasks are ready" not in rendered
    assert "Sandbox:" not in rendered
    assert "Session:" not in rendered
    assert "\033[" not in rendered
    assert "Workspace changed. Use /diff or /undo." in rendered
    assert "Undid: change the value" in rendered
    assert prompts == ["> ", "> ", "> ", "> "]


def test_repository_resolution_and_session_path_are_stable(tmp_path):
    _init_repo(tmp_path)
    nested = tmp_path / "src"
    nested.mkdir()

    root = resolve_repository(nested)

    assert root == tmp_path.resolve()
    assert default_session_path(root) == default_session_path(root)
    assert default_session_path(root).suffix == ".jsonl"


def test_terminal_task_display_formats_runtime_and_tool_progress():
    output = io.StringIO()
    display = TerminalTaskDisplay(output, lambda text, _: text)
    task = CodingTask(objective="Fix the cache")

    display.on_runtime_event(
        RuntimeEvent(
            sequence=1,
            event_type="runtime_start",
            payload={},
            elapsed_ms=0,
        )
    )
    display.on_runtime_event(
        RuntimeEvent(
            sequence=2,
            event_type="phase_start",
            phase="localization",
            payload={},
            elapsed_ms=1,
        )
    )
    display.on_tool_event(
        ExtensionEvent(
            name="tool_call",
            task=task,
            turn=1,
            payload={
                "name": "read_file",
                "arguments": '{"path":"app/cache.py"}',
            },
        )
    )

    rendered = output.getvalue()
    assert "* Understanding the task..." in rendered
    assert "* Inspecting the repository..." in rendered
    assert "└─ Reading app/cache.py" in rendered


def test_interactive_shell_formats_errors_as_terminal_block(tmp_path):
    _init_repo(tmp_path)
    output = io.StringIO()
    commands = iter(["trigger an API error", "/exit"])

    def fail_task(task, session):
        raise RuntimeError("API Error: 402 Insufficient Balance")

    shell = InteractiveShell(
        repo_path=tmp_path,
        policy=get_runtime_policy("full"),
        session=AgentSession(path=tmp_path / "session.jsonl"),
        input_fn=lambda _: next(commands),
        output=output,
        task_executor=fail_task,
    )

    assert shell.run() == 0
    rendered = output.getvalue()
    assert "└─ Error: API Error: 402 Insufficient Balance" in rendered
    assert "Task failed:" not in rendered
