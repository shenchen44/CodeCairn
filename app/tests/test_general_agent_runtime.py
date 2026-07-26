import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.services.adapters import interactive_task
from app.services.agent_runtime import (
    AgentSession,
    ExtensionEvent,
    ExtensionManager,
    ExtensionResult,
    TaskIntent,
    ToolCapability,
    normalize_task,
)
from app.services.openai.policy import get_runtime_policy
from app.services.openai.staged_runtime import StagedAgentRuntime
from app.services.openai.tools import AgentToolbox
from app.services.sandbox.repo_config import load_repo_config


def _response(content: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=None,
                )
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
        ),
    )


def _repo(workspace_tmp_dir: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "toy_repo"
    target = workspace_tmp_dir / "general_runtime_repo"
    shutil.copytree(source, target)
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "tests@example.com"],
        ["git", "config", "user.name", "tests"],
        ["git", "add", "."],
        ["git", "commit", "-m", "init"],
    ):
        subprocess.run(
            command,
            cwd=target,
            check=True,
            capture_output=True,
            text=True,
        )
    return target


def test_legacy_issue_normalizes_to_source_independent_task() -> None:
    task = normalize_task(
        {
            "title": "Improve parser diagnostics",
            "body": "Return the exact source location.",
            "issue_number": 42,
        }
    )

    assert task.objective == "Improve parser diagnostics"
    assert task.description == "Return the exact source location."
    assert task.requires_workspace_change is True
    assert task.metadata["issue_number"] == 42


def test_review_task_hides_and_rejects_mutating_tools(
    workspace_tmp_dir,
) -> None:
    repo_path = _repo(workspace_tmp_dir)
    toolbox = AgentToolbox(
        repo_path=repo_path,
        repo_config=load_repo_config(repo_path),
        task_context=interactive_task(
            "Review display formatting",
            intent=TaskIntent.review,
        ),
    )

    names = {
        item["function"]["name"]
        for item in toolbox.tool_schemas()
    }
    rejected = toolbox.dispatch(
        "write_file",
        json.dumps(
            {
                "path": "app/display.py",
                "content": "changed",
            }
        ),
    )

    assert "get_task_context" in names
    assert "write_file" not in names
    assert rejected["error"] == "tool_not_allowed_for_task:write_file"


def test_dynamic_tool_registration_and_activation(workspace_tmp_dir) -> None:
    repo_path = _repo(workspace_tmp_dir)
    toolbox = AgentToolbox(
        repo_path=repo_path,
        repo_config=load_repo_config(repo_path),
        task_context=interactive_task("Inspect the repository"),
    )
    toolbox.register_tool(
        name="echo_context",
        schema={
            "type": "function",
            "function": {
                "name": "echo_context",
                "description": "Echo a value",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            },
        },
        handler=lambda value: {"value": value},
        capability=ToolCapability.context,
    )
    toolbox.set_active_tools(["echo_context"])

    assert toolbox.get_active_tools() == ["echo_context"]
    assert toolbox.dispatch(
        "echo_context",
        '{"value":"ok"}',
    ) == {"value": "ok"}


def test_non_python_tool_profile_uses_search_and_sandbox_commands(
    workspace_tmp_dir,
) -> None:
    repo_path = workspace_tmp_dir / "javascript_repo"
    repo_path.mkdir()
    (repo_path / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}',
        encoding="utf-8",
    )
    toolbox = AgentToolbox(
        repo_path=repo_path,
        repo_config=load_repo_config(repo_path),
        task_context=interactive_task("Update the parser"),
    )

    names = {
        item["function"]["name"]
        for item in toolbox.tool_schemas()
    }

    assert "search_code" in names
    assert "run_command" in names
    assert "find_definition" not in names
    assert "run_test_file" not in names


def test_multi_file_exact_edit_rolls_back_as_one_transaction(
    workspace_tmp_dir,
) -> None:
    repo_path = _repo(workspace_tmp_dir)
    toolbox = AgentToolbox(
        repo_path=repo_path,
        repo_config=load_repo_config(repo_path),
        task_context=interactive_task("Update formatting"),
    )
    path = repo_path / "app" / "display.py"
    original = path.read_text(encoding="utf-8")

    try:
        toolbox.replace_in_files(
            [
                {
                    "path": "app/display.py",
                    "old_text": "return name.strip().title()",
                    "new_text": "return name.strip()",
                },
                {
                    "path": "app/display.py",
                    "old_text": "missing exact text",
                    "new_text": "never applied",
                },
            ]
        )
    except Exception:
        pass

    assert path.read_text(encoding="utf-8") == original


def test_extension_can_modify_and_block_lifecycle_events() -> None:
    seen: list[str] = []

    class Extension:
        def on_event(self, event):
            seen.append(event.name)
            if event.name == "tool_call":
                return ExtensionResult(
                    block=True,
                    reason="read-only policy",
                )
            return ExtensionResult(
                payload={**event.payload, "tag": "observed"}
            )

    manager = ExtensionManager([Extension()])
    task = interactive_task(
        "Review formatting",
        intent=TaskIntent.review,
    )

    before = manager.emit(
        ExtensionEvent(
            name="before_model",
            task=task,
            payload={"tools": []},
        )
    )
    tool = manager.emit(
        ExtensionEvent(
            name="tool_call",
            task=task,
            payload={"name": "write_file"},
        )
    )

    assert before.payload["tag"] == "observed"
    assert tool.block is True
    assert seen == ["before_model", "tool_call"]


def test_session_is_persistent_and_forkable(workspace_tmp_dir) -> None:
    path = workspace_tmp_dir / "sessions" / "session.jsonl"
    session = AgentSession(path=path)
    first = session.append_message(
        {"role": "user", "content": "Inspect parser.py"}
    )
    session.append_message(
        {"role": "assistant", "content": "The parser has two entry points."}
    )

    loaded = AgentSession.load(path)
    fork = loaded.fork(first.id)
    fork.append_message(
        {"role": "user", "content": "Review only the first entry point."}
    )

    assert len(loaded.message_context()) == 2
    assert [item["content"] for item in fork.message_context()] == [
        "Inspect parser.py",
        "Review only the first entry point.",
    ]


def test_non_change_task_bypasses_patch_pipeline() -> None:
    class Toolbox:
        task = interactive_task(
            "Explain the parser architecture",
            intent=TaskIntent.explain,
        )

        def __init__(self) -> None:
            self.dispatched: list[str] = []

        @staticmethod
        def tool_schemas():
            return []

        def dispatch(self, name, arguments):
            self.dispatched.append(name)
            return {}

    class Client:
        def create_completion(self, messages, tools):
            return _response(
                json.dumps(
                    {
                        "summary": {
                            "status": "completed",
                            "findings": ["Parser uses two phases"],
                        },
                        "patch_text": "",
                        "delivery": {
                            "title": "Parser architecture",
                            "description": "Read-only explanation",
                        },
                    }
                )
            )

    toolbox = Toolbox()
    result = StagedAgentRuntime(
        client=Client(),
        policy=get_runtime_policy("full"),
    ).run(toolbox)

    assert toolbox.dispatched == []
    assert result.agent_graph["strategy"] == "task_only"
    assert result.delivery["title"] == "Parser architecture"
    assert result.localization is None
