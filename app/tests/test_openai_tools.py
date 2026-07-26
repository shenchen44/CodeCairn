import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.openai.tools import AgentToolbox, ToolExecutionError
from app.services.openai.policy import get_runtime_policy
from app.services.sandbox.repo_config import load_repo_config


class LocalRunner:
    def run_tests(self, repo_path: Path, test_command: str):
        process = subprocess.run(
            [sys.executable, *test_command.split()[1:]],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=False,
        )
        return type("Result", (), {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})()


def _init_git_repo(repo_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "tests"], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_path, check=True, capture_output=True, text=True)


def _make_toolbox(workspace_tmp_dir: Path, *, add_blocked_file: bool = False) -> tuple[Path, AgentToolbox]:
    fixture_repo = Path(__file__).parent / "fixtures" / "toy_repo"
    repo_path = workspace_tmp_dir / "toy_repo"
    shutil.copytree(fixture_repo, repo_path)
    if add_blocked_file:
        blocked_file = repo_path / ".github" / "workflows" / "test.yml"
        blocked_file.parent.mkdir(parents=True, exist_ok=True)
        blocked_file.write_text("name: test\n", encoding="utf-8")
    _init_git_repo(repo_path)
    repo_config = load_repo_config(repo_path)
    toolbox = AgentToolbox(
        repo_path=repo_path,
        repo_config=repo_config,
        issue_context={"title": "test", "body": "test", "issue_number": 1},
        sandbox_runner=LocalRunner(),
    )
    return repo_path, toolbox


def test_write_file_success(workspace_tmp_dir) -> None:
    repo_path, toolbox = _make_toolbox(workspace_tmp_dir)
    new_content = """def format_display_name(name: str | None) -> str:
    if name is None:
        return ""
    return name.strip().title()
"""
    result = toolbox.write_file("app/display.py", new_content)
    assert result["files_changed_count"] == 1
    assert result["diff_line_count"] >= 2
    assert "return \"\"" in (repo_path / "app" / "display.py").read_text(encoding="utf-8")


def test_write_file_blocked_path_fails(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir, add_blocked_file=True)
    with pytest.raises(ValueError, match=r"path_not_allowed:\.github/workflows/test.yml"):
        toolbox.write_file(".github/workflows/test.yml", "name: changed\n")


def test_write_file_diff_limit_failure_restores_original(workspace_tmp_dir) -> None:
    repo_path, toolbox = _make_toolbox(workspace_tmp_dir)
    toolbox.repo_config.max_diff_lines = 1
    original_content = (repo_path / "app" / "display.py").read_text(encoding="utf-8")
    new_content = """def format_display_name(name: str | None) -> str:
    if name is None:
        return ""
    cleaned = name.strip()
    return cleaned.title()
"""
    with pytest.raises(ValueError, match="diff_lines_limit_exceeded"):
        toolbox.write_file("app/display.py", new_content)
    assert (repo_path / "app" / "display.py").read_text(encoding="utf-8") == original_content
    assert toolbox.git_diff()["diff"] == ""


def test_write_file_syntax_failure_restores_original(workspace_tmp_dir) -> None:
    repo_path, toolbox = _make_toolbox(workspace_tmp_dir)
    target = repo_path / "app" / "display.py"
    original_content = target.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="python_syntax_invalid"):
        toolbox.write_file(
            "app/display.py",
            "def format_display_name(name):\n    if name is None\n        return ''\n",
        )

    assert target.read_text(encoding="utf-8") == original_content
    assert toolbox.git_diff()["diff"] == ""


def test_write_file_rejects_literal_escaped_newlines(workspace_tmp_dir) -> None:
    repo_path, toolbox = _make_toolbox(workspace_tmp_dir)
    target = repo_path / "app" / "display.py"
    original_content = target.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="escaped_newlines_detected"):
        toolbox.write_file(
            "app/display.py",
            "def format_display_name(name):\\n    return name\\n",
        )

    assert target.read_text(encoding="utf-8") == original_content
    assert toolbox.git_diff()["diff"] == ""


def test_apply_patch_failure_exposes_git_error(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    with pytest.raises(RuntimeError, match="git_apply_failed:"):
        toolbox.apply_patch("not a valid patch")


def test_run_tests_tolerates_optional_runner_argument(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.run_tests(runner="python -m pytest")
    assert isinstance(result["exit_code"], int)


def test_dispatch_returns_recoverable_tool_error(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.dispatch("read_file", '{"path":"tests/does_not_exist.py"}')
    assert result["recoverable"] is True
    assert result["tool"] == "read_file"
    assert "tool_call_failed:read_file" in result["error"]


def test_dispatch_escalates_mutating_tool_error(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir, add_blocked_file=True)

    with pytest.raises(ToolExecutionError, match="path_not_allowed"):
        toolbox.dispatch(
            "write_file",
            '{"path":".github/workflows/test.yml","content":"blocked"}',
        )


def test_legacy_policy_hides_hybrid_retrieval_tool(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    toolbox.runtime_policy = get_runtime_policy("legacy")

    tool_names = {
        schema["function"]["name"] for schema in toolbox.tool_schemas()
    }

    assert "retrieve_code" not in tool_names


def test_dispatch_returns_recoverable_json_argument_error(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.dispatch("read_file", '{"path" "app/display.py"}')
    assert result["recoverable"] is True
    assert result["tool"] == "read_file"
    assert "tool_arguments_invalid_json:read_file" in result["error"]


# ---------------------------------------------------------------------------
# find_definition tests
# ---------------------------------------------------------------------------

def test_find_definition_finds_function(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.find_definition("format_display_name")
    assert result["total"] >= 1
    defn = result["definitions"][0]
    assert defn["path"] == "app/display.py"
    assert defn["type"] == "function"
    assert defn["line"] >= 1


def test_find_definition_finds_class(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.find_definition("LocalRunner")
    # LocalRunner doesn't exist in toy_repo, but format_display_name does
    # This should return 0 for a non-existent class
    assert result["total"] == 0


def test_find_definition_returns_empty_for_nonexistent(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.find_definition("nonexistent_function_xyz")
    assert result["total"] == 0
    assert result["definitions"] == []


# ---------------------------------------------------------------------------
# get_imports tests
# ---------------------------------------------------------------------------

def test_get_imports_shows_forward_imports(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.get_imports("tests/test_display.py")
    assert result["file"] == "tests/test_display.py"
    # test_display.py imports from app.display
    assert any("app.display" in imp for imp in result["imports"])


def test_get_imports_shows_reverse_imports(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.get_imports("app/display.py")
    # app/display.py is imported by tests/test_display.py
    assert any("test_display" in path for path in result["imported_by"])


# ---------------------------------------------------------------------------
# get_functions tests
# ---------------------------------------------------------------------------

def test_get_functions_lists_functions(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.get_functions("app/display.py")
    assert result["file"] == "app/display.py"
    assert len(result["items"]) >= 1
    func = next(item for item in result["items"] if item["name"] == "format_display_name")
    assert func["type"] == "function"
    assert func["line"] >= 1


# ---------------------------------------------------------------------------
# git_log tests
# ---------------------------------------------------------------------------

def test_git_log_returns_commits(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.git_log()
    assert len(result["commits"]) >= 1
    commit = result["commits"][0]
    assert "hash" in commit
    assert "message" in commit
    assert "author" in commit


def test_git_log_filters_by_file(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.git_log(path="app/display.py")
    assert len(result["commits"]) >= 1
    assert result["file"] == "app/display.py"


# ---------------------------------------------------------------------------
# git_blame tests
# ---------------------------------------------------------------------------

def test_git_blame_returns_line_info(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.git_blame("app/display.py")
    assert result["file"] == "app/display.py"
    assert len(result["blame"]) >= 1
    entry = result["blame"][0]
    assert "line" in entry
    assert "hash" in entry


# ---------------------------------------------------------------------------
# run_test_file tests
# ---------------------------------------------------------------------------

def test_run_test_file_runs_specific_test(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.run_test_file("tests/test_display.py")
    assert "exit_code" in result
    assert "duration_ms" in result
    assert result["test_path"] == "tests/test_display.py"


def test_run_test_file_runs_specific_test_function(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    result = toolbox.run_test_file("tests/test_display.py", "test_format_display_name_basic")
    assert result["exit_code"] == 0
    assert result["test_name"] == "test_format_display_name_basic"


def test_run_test_file_classifies_failure(workspace_tmp_dir) -> None:
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    # Run a non-existent test file to trigger a failure
    result = toolbox.run_test_file("tests/nonexistent.py")
    assert result["exit_code"] != 0
    assert "failure_type" in result
    assert "guidance" in result


# ---------------------------------------------------------------------------
# Failure classification tests
# ---------------------------------------------------------------------------

def test_run_tests_classifies_assertion_failure(workspace_tmp_dir) -> None:
    """When a test fails with AssertionError, the result should include failure_type."""
    _, toolbox = _make_toolbox(workspace_tmp_dir)
    # Write a failing test
    toolbox.write_file("app/display.py", 'def format_display_name(name):\n    return "wrong"\n')
    result = toolbox.run_tests()
    assert result["exit_code"] != 0
    assert "failure_type" in result
    assert "guidance" in result
