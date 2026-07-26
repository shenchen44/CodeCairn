import io
import subprocess
from pathlib import Path

import pytest

from app.services.agent_runtime import AgentSession, TaskIntent
from app.services.openai.agent_loop import AgentRunResult
from app.services.openai.policy import get_runtime_policy
from codecairn import __version__
from codecairn.cli import build_parser, main
from codecairn.interactive import (
    InteractiveShell,
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

    shell = InteractiveShell(
        repo_path=tmp_path,
        policy=get_runtime_policy("full"),
        session=AgentSession(path=tmp_path / "session.jsonl"),
        input_fn=lambda _: next(commands),
        output=output,
        task_executor=execute,
    )

    assert shell.run() == 0
    assert observed_intents == [TaskIntent.change, TaskIntent.review]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert "Workspace changed. Use /diff or /undo." in output.getvalue()
    assert "Undid: change the value" in output.getvalue()


def test_repository_resolution_and_session_path_are_stable(tmp_path):
    _init_repo(tmp_path)
    nested = tmp_path / "src"
    nested.mkdir()

    root = resolve_repository(nested)

    assert root == tmp_path.resolve()
    assert default_session_path(root) == default_session_path(root)
    assert default_session_path(root).suffix == ".jsonl"
