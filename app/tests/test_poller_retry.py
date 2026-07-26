import asyncio
from copy import deepcopy
import json
import shutil
import subprocess
import sys
from pathlib import Path

from app.db.models.task import TaskArtifactType, TaskResultStatus, TaskStatus
from app.db.session import SessionLocal
from app.services.openai.agent_loop import AgentResponseParseError, AgentRunResult
from app.services.task_runner.orchestrator import create_integration_task, create_task_from_webhook
from app.workers import poller


def _init_git_repo(repo_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "tests"], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_path, check=True, capture_output=True, text=True)


def test_process_task_retries_after_tool_failure(sample_issue_payload, workspace_tmp_dir, monkeypatch) -> None:
    db = SessionLocal()
    task = create_task_from_webhook(db, sample_issue_payload)
    db.close()

    class FakeSettings:
        max_attempts = 3
        pr_review_label = "needs-human-review"

    class FakeAuthService:
        async def get_installation_token(self, installation_id: int) -> str:
            return "token"

    class FakeIssueService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_comment(self, owner: str, repo: str, issue_number: int, body: str) -> None:
            return None

        async def add_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> None:
            return None

    class FakePullRequestService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_pull_request(self, **kwargs) -> dict:
            return {"number": 99, "html_url": "https://example.com/pr/99"}

    class FakeSandboxRunner:
        def install_dependencies(self, repo_path: Path, install_command: str):
            return type("Result", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

        def run_tests(self, repo_path: Path, test_command: str):
            process = subprocess.run(
                [sys.executable, *test_command.split()[1:]],
                cwd=repo_path,
                text=True,
                capture_output=True,
                check=False,
            )
            return type("Result", (), {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})()

    class FakeAgentLoop:
        calls = 0

        def run(self, toolbox, retry_context=None):
            type(self).calls += 1
            if type(self).calls == 1:
                toolbox.dispatch(
                    "write_file",
                    json.dumps({"path": ".github/workflows/test.yml", "content": "name: blocked\n"}),
                )
            toolbox.dispatch(
                "read_file",
                json.dumps({"path": "app/display.py"}),
            )
            toolbox.dispatch(
                "write_file",
                json.dumps(
                    {
                        "path": "app/display.py",
                        "content": 'def format_display_name(name: str | None) -> str:\n    if name is None:\n        return ""\n    return name.strip().title()\n',
                    }
                ),
            )
            return AgentRunResult(
                summary={
                    "root_cause": "format_display_name assumed name was always a string.",
                    "files_to_change": ["app/display.py"],
                    "patch_plan": ["Handle None before trimming the input."],
                    "test_expectation": "pytest should pass.",
                },
                patch_text="",
                pr_title="fix: handle None display names",
                pr_body_summary={"root_cause": "None input was not handled.", "changes": ["Return an empty string for None."]},
            )

    def fake_clone_repo(clone_url: str, destination: Path) -> None:
        fixture_repo = Path(__file__).parent / "fixtures" / "toy_repo"
        shutil.copytree(fixture_repo, destination)
        blocked_file = destination / ".github" / "workflows" / "test.yml"
        blocked_file.parent.mkdir(parents=True, exist_ok=True)
        blocked_file.write_text("name: original\n", encoding="utf-8")
        _init_git_repo(destination)

    def fake_checkout_new_branch(repo_path: Path, branch_name: str, base_branch: str) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, text=True, capture_output=True).stdout.strip()

    monkeypatch.setattr(poller, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(poller, "ensure_workspace_root", lambda: workspace_tmp_dir)
    monkeypatch.setattr(poller, "GitHubAuthService", FakeAuthService)
    monkeypatch.setattr(poller, "GitHubIssueService", FakeIssueService)
    monkeypatch.setattr(poller, "GitHubPullRequestService", FakePullRequestService)
    monkeypatch.setattr(poller, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(poller, "AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(poller, "clone_repo", fake_clone_repo)
    monkeypatch.setattr(poller, "checkout_new_branch", fake_checkout_new_branch)
    monkeypatch.setattr(poller, "commit_all", lambda repo_path, message, include_paths=None: "head-sha")
    monkeypatch.setattr(poller, "push_branch", lambda repo_path, branch_name: None)

    asyncio.run(poller.process_task(task.id))

    db = SessionLocal()
    refreshed_task = db.get(type(task), task.id)
    assert refreshed_task is not None
    assert refreshed_task.status == TaskStatus.done
    assert refreshed_task.attempt_count == 2
    assert len(refreshed_task.attempts) == 2
    assert refreshed_task.attempts[0].result_status == TaskResultStatus.failed
    assert refreshed_task.attempts[1].result_status == TaskResultStatus.success

    failure_artifacts = [
        artifact for artifact in refreshed_task.artifacts
        if artifact.artifact_type == TaskArtifactType.model_response and artifact.content.get("attempt") == 1
    ]
    assert failure_artifacts
    assert "tool_call_failed:write_file" in failure_artifacts[0].content["error"]

    diff_artifacts = [
        artifact for artifact in refreshed_task.artifacts
        if artifact.artifact_type == TaskArtifactType.diff and artifact.content.get("attempt") == 1
    ]
    assert diff_artifacts
    assert "error" in diff_artifacts[0].content
    install_artifacts = [
        artifact for artifact in refreshed_task.artifacts
        if artifact.artifact_type == TaskArtifactType.install_log
    ]
    assert install_artifacts
    assert install_artifacts[0].content["exit_code"] == 0
    db.close()


def test_process_task_skips_patch_text_when_tools_already_changed_files(sample_issue_payload, workspace_tmp_dir, monkeypatch) -> None:
    db = SessionLocal()
    task = create_task_from_webhook(db, sample_issue_payload)
    db.close()

    class FakeSettings:
        max_attempts = 3
        pr_review_label = "needs-human-review"

    class FakeAuthService:
        async def get_installation_token(self, installation_id: int) -> str:
            return "token"

    class FakeIssueService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_comment(self, owner: str, repo: str, issue_number: int, body: str) -> None:
            return None

        async def add_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> None:
            return None

    class FakePullRequestService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_pull_request(self, **kwargs) -> dict:
            return {"number": 100, "html_url": "https://example.com/pr/100"}

    class FakeSandboxRunner:
        def install_dependencies(self, repo_path: Path, install_command: str):
            return type("Result", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

        def run_tests(self, repo_path: Path, test_command: str):
            process = subprocess.run(
                [sys.executable, *test_command.split()[1:]],
                cwd=repo_path,
                text=True,
                capture_output=True,
                check=False,
            )
            return type("Result", (), {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})()

    class FakeAgentLoop:
        def run(self, toolbox, retry_context=None):
            toolbox.dispatch(
                "read_file",
                json.dumps({"path": "app/display.py"}),
            )
            toolbox.dispatch(
                "write_file",
                json.dumps(
                    {
                        "path": "app/display.py",
                        "content": 'def format_display_name(name: str | None) -> str:\n    if name is None:\n        return ""\n    return name.strip().title()\n',
                    }
                ),
            )
            return AgentRunResult(
                summary={
                    "root_cause": "format_display_name assumed name was always a string.",
                    "files_to_change": ["app/display.py"],
                    "patch_plan": ["Handle None before trimming the input."],
                    "test_expectation": "pytest should pass.",
                },
                patch_text="diff --git a/app/display.py b/app/display.py\nthis is intentionally invalid\n",
                pr_title="fix: handle None display names",
                pr_body_summary={"root_cause": "None input was not handled.", "changes": ["Return an empty string for None."]},
            )

    def fake_clone_repo(clone_url: str, destination: Path) -> None:
        fixture_repo = Path(__file__).parent / "fixtures" / "toy_repo"
        shutil.copytree(fixture_repo, destination)
        _init_git_repo(destination)

    def fake_checkout_new_branch(repo_path: Path, branch_name: str, base_branch: str) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, text=True, capture_output=True).stdout.strip()

    def fail_if_apply_patch(*args, **kwargs):
        raise AssertionError("apply_patch should be skipped when tool edits already produced a diff")

    monkeypatch.setattr(poller, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(poller, "ensure_workspace_root", lambda: workspace_tmp_dir)
    monkeypatch.setattr(poller, "GitHubAuthService", FakeAuthService)
    monkeypatch.setattr(poller, "GitHubIssueService", FakeIssueService)
    monkeypatch.setattr(poller, "GitHubPullRequestService", FakePullRequestService)
    monkeypatch.setattr(poller, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(poller, "AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(poller, "clone_repo", fake_clone_repo)
    monkeypatch.setattr(poller, "checkout_new_branch", fake_checkout_new_branch)
    monkeypatch.setattr(poller, "commit_all", lambda repo_path, message, include_paths=None: "head-sha")
    monkeypatch.setattr(poller, "push_branch", lambda repo_path, branch_name: None)
    monkeypatch.setattr("app.services.openai.tools.AgentToolbox.apply_patch", fail_if_apply_patch)

    asyncio.run(poller.process_task(task.id))

    db = SessionLocal()
    refreshed_task = db.get(type(task), task.id)
    assert refreshed_task is not None
    assert refreshed_task.status == TaskStatus.done
    assert refreshed_task.attempt_count == 1
    assert refreshed_task.attempts[0].result_status == TaskResultStatus.success
    assert any(artifact.artifact_type == TaskArtifactType.install_log for artifact in refreshed_task.artifacts)
    db.close()


def test_process_task_preserves_diff_and_test_log_on_final_json_parse_failure(sample_issue_payload, workspace_tmp_dir, monkeypatch) -> None:
    db = SessionLocal()
    task = create_task_from_webhook(db, sample_issue_payload)
    db.close()

    class FakeSettings:
        max_attempts = 3
        pr_review_label = "needs-human-review"

    class FakeAuthService:
        async def get_installation_token(self, installation_id: int) -> str:
            return "token"

    class FakeIssueService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_comment(self, owner: str, repo: str, issue_number: int, body: str) -> None:
            return None

        async def add_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> None:
            return None

    class FakePullRequestService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_pull_request(self, **kwargs) -> dict:
            return {"number": 101, "html_url": "https://example.com/pr/101"}

    class FakeSandboxRunner:
        def install_dependencies(self, repo_path: Path, install_command: str):
            return type("Result", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

        def run_tests(self, repo_path: Path, test_command: str):
            process = subprocess.run(
                [sys.executable, *test_command.split()[1:]],
                cwd=repo_path,
                text=True,
                capture_output=True,
                check=False,
            )
            return type("Result", (), {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})()

    class FakeAgentLoop:
        calls = 0

        def run(self, toolbox, retry_context=None):
            type(self).calls += 1
            toolbox.dispatch(
                "write_file",
                json.dumps(
                    {
                        "path": "app/display.py",
                        "content": 'def format_display_name(name: str | None) -> str:\n    if name is None:\n        return ""\n    return name.strip().title()\n',
                    }
                ),
            )
            if type(self).calls == 1:
                raise AgentResponseParseError(
                    "invalid_model_json: almost json",
                    'summary: "almost json"',
                )
            return AgentRunResult(
                summary={
                    "root_cause": "format_display_name assumed name was always a string.",
                    "files_to_change": ["app/display.py"],
                    "patch_plan": ["Handle None before trimming the input."],
                    "test_expectation": "pytest should pass.",
                },
                patch_text="",
                pr_title="fix: handle None display names",
                pr_body_summary={"root_cause": "None input was not handled.", "changes": ["Return an empty string for None."]},
            )

    def fake_clone_repo(clone_url: str, destination: Path) -> None:
        fixture_repo = Path(__file__).parent / "fixtures" / "toy_repo"
        shutil.copytree(fixture_repo, destination)
        _init_git_repo(destination)

    def fake_checkout_new_branch(repo_path: Path, branch_name: str, base_branch: str) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, text=True, capture_output=True).stdout.strip()

    monkeypatch.setattr(poller, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(poller, "ensure_workspace_root", lambda: workspace_tmp_dir)
    monkeypatch.setattr(poller, "GitHubAuthService", FakeAuthService)
    monkeypatch.setattr(poller, "GitHubIssueService", FakeIssueService)
    monkeypatch.setattr(poller, "GitHubPullRequestService", FakePullRequestService)
    monkeypatch.setattr(poller, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(poller, "AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(poller, "clone_repo", fake_clone_repo)
    monkeypatch.setattr(poller, "checkout_new_branch", fake_checkout_new_branch)
    monkeypatch.setattr(poller, "commit_all", lambda repo_path, message, include_paths=None: "head-sha")
    monkeypatch.setattr(poller, "push_branch", lambda repo_path, branch_name: None)

    asyncio.run(poller.process_task(task.id))

    db = SessionLocal()
    refreshed_task = db.get(type(task), task.id)
    assert refreshed_task is not None
    assert refreshed_task.status == TaskStatus.done
    assert refreshed_task.attempt_count == 2
    assert refreshed_task.attempts[0].result_status == TaskResultStatus.failed
    assert refreshed_task.attempts[0].test_command is not None
    assert refreshed_task.attempts[0].test_exit_code == 0

    failure_artifact = next(
        artifact for artifact in refreshed_task.artifacts
        if artifact.artifact_type == TaskArtifactType.model_response and artifact.content.get("attempt") == 1
    )
    assert failure_artifact.content["raw_response"] == 'summary: "almost json"'

    diff_artifact = next(
        artifact for artifact in refreshed_task.artifacts
        if artifact.artifact_type == TaskArtifactType.diff and artifact.content.get("attempt") == 1
    )
    assert "return \"\"" in diff_artifact.content["diff"]

    test_artifact = next(
        artifact for artifact in refreshed_task.artifacts
        if artifact.artifact_type == TaskArtifactType.test_log
    )
    assert test_artifact.content["exit_code"] == 0
    db.close()


def test_process_task_records_install_log_on_install_failure(sample_issue_payload, workspace_tmp_dir, monkeypatch) -> None:
    db = SessionLocal()
    task = create_task_from_webhook(db, sample_issue_payload)
    db.close()

    class FakeSettings:
        max_attempts = 3
        pr_review_label = "needs-human-review"

    class FakeAuthService:
        async def get_installation_token(self, installation_id: int) -> str:
            return "token"

    class FakeSandboxRunner:
        def install_dependencies(self, repo_path: Path, install_command: str):
            return type("Result", (), {"exit_code": 1, "stdout": "pip stdout", "stderr": "pip stderr"})()

    def fake_clone_repo(clone_url: str, destination: Path) -> None:
        fixture_repo = Path(__file__).parent / "fixtures" / "toy_repo"
        shutil.copytree(fixture_repo, destination)
        _init_git_repo(destination)

    def fake_checkout_new_branch(repo_path: Path, branch_name: str, base_branch: str) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, text=True, capture_output=True).stdout.strip()

    monkeypatch.setattr(poller, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(poller, "ensure_workspace_root", lambda: workspace_tmp_dir)
    monkeypatch.setattr(poller, "GitHubAuthService", FakeAuthService)
    monkeypatch.setattr(poller, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(poller, "clone_repo", fake_clone_repo)
    monkeypatch.setattr(poller, "checkout_new_branch", fake_checkout_new_branch)

    asyncio.run(poller.process_task(task.id))

    db = SessionLocal()
    refreshed_task = db.get(type(task), task.id)
    assert refreshed_task is not None
    assert refreshed_task.status == TaskStatus.failed
    assert refreshed_task.failure_reason is not None
    assert "pip stdout" in refreshed_task.failure_reason["details"]["error"]
    assert "pip stderr" in refreshed_task.failure_reason["details"]["error"]
    install_artifact = next(
        artifact for artifact in refreshed_task.artifacts
        if artifact.artifact_type == TaskArtifactType.install_log
    )
    assert install_artifact.content["stdout"] == "pip stdout"
    assert install_artifact.content["stderr"] == "pip stderr"
    assert install_artifact.content["exit_code"] == 1
    db.close()


def test_process_task_accepts_string_summary_and_pr_body_summary(sample_issue_payload, workspace_tmp_dir, monkeypatch) -> None:
    db = SessionLocal()
    task = create_task_from_webhook(db, sample_issue_payload)
    db.close()

    class FakeSettings:
        max_attempts = 3
        pr_review_label = "needs-human-review"

    class FakeAuthService:
        async def get_installation_token(self, installation_id: int) -> str:
            return "token"

    class FakeIssueService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_comment(self, owner: str, repo: str, issue_number: int, body: str) -> None:
            return None

        async def add_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> None:
            return None

    class FakePullRequestService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_pull_request(self, **kwargs) -> dict:
            return {"number": 102, "html_url": "https://example.com/pr/102"}

    class FakeSandboxRunner:
        def install_dependencies(self, repo_path: Path, install_command: str):
            return type("Result", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

        def run_tests(self, repo_path: Path, test_command: str):
            process = subprocess.run(
                [sys.executable, *test_command.split()[1:]],
                cwd=repo_path,
                text=True,
                capture_output=True,
                check=False,
            )
            return type("Result", (), {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})()

    class FakeAgentLoop:
        def run(self, toolbox, retry_context=None):
            toolbox.dispatch(
                "write_file",
                json.dumps(
                    {
                        "path": "app/display.py",
                        "content": 'def format_display_name(name: str | None) -> str:\n    if name is None:\n        return ""\n    return name.strip().title()\n',
                    }
                ),
            )
            return AgentRunResult(
                summary="summary as plain string",
                patch_text="",
                pr_title="fix: handle None display names",
                pr_body_summary="pr body summary as string",
            )

    def fake_clone_repo(clone_url: str, destination: Path) -> None:
        fixture_repo = Path(__file__).parent / "fixtures" / "toy_repo"
        shutil.copytree(fixture_repo, destination)
        _init_git_repo(destination)

    def fake_checkout_new_branch(repo_path: Path, branch_name: str, base_branch: str) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, text=True, capture_output=True).stdout.strip()

    monkeypatch.setattr(poller, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(poller, "ensure_workspace_root", lambda: workspace_tmp_dir)
    monkeypatch.setattr(poller, "GitHubAuthService", FakeAuthService)
    monkeypatch.setattr(poller, "GitHubIssueService", FakeIssueService)
    monkeypatch.setattr(poller, "GitHubPullRequestService", FakePullRequestService)
    monkeypatch.setattr(poller, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(poller, "AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(poller, "clone_repo", fake_clone_repo)
    monkeypatch.setattr(poller, "checkout_new_branch", fake_checkout_new_branch)
    monkeypatch.setattr(poller, "commit_all", lambda repo_path, message, include_paths=None: "head-sha")
    monkeypatch.setattr(poller, "push_branch", lambda repo_path, branch_name: None)

    asyncio.run(poller.process_task(task.id))

    db = SessionLocal()
    refreshed_task = db.get(type(task), task.id)
    assert refreshed_task is not None
    assert refreshed_task.status == TaskStatus.done
    assert refreshed_task.head_commit == "head-sha"
    pr_body_artifact = next(
        artifact for artifact in refreshed_task.artifacts
        if artifact.artifact_type == TaskArtifactType.pr_body
    )
    assert "Issue-specific bug" in pr_body_artifact.content["body"]
    assert "Minimal targeted patch" in pr_body_artifact.content["body"]
    db.close()


def test_process_task_marks_failed_when_pr_stage_errors_after_tests_pass(sample_issue_payload, workspace_tmp_dir, monkeypatch) -> None:
    db = SessionLocal()
    task = create_task_from_webhook(db, sample_issue_payload)
    db.close()

    class FakeSettings:
        max_attempts = 3
        pr_review_label = "needs-human-review"

    class FakeAuthService:
        async def get_installation_token(self, installation_id: int) -> str:
            return "token"

    class FakeSandboxRunner:
        def install_dependencies(self, repo_path: Path, install_command: str):
            return type("Result", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

        def run_tests(self, repo_path: Path, test_command: str):
            process = subprocess.run(
                [sys.executable, *test_command.split()[1:]],
                cwd=repo_path,
                text=True,
                capture_output=True,
                check=False,
            )
            return type("Result", (), {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})()

    class FakeAgentLoop:
        def run(self, toolbox, retry_context=None):
            toolbox.dispatch(
                "write_file",
                json.dumps(
                    {
                        "path": "app/display.py",
                        "content": 'def format_display_name(name: str | None) -> str:\n    if name is None:\n        return ""\n    return name.strip().title()\n',
                    }
                ),
            )
            return AgentRunResult(
                summary={"root_cause": "x", "patch_plan": ["y"]},
                patch_text="",
                pr_title="fix: handle None display names",
                pr_body_summary={"root_cause": "x", "changes": ["y"]},
            )

    class FailingPullRequestService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_pull_request(self, **kwargs) -> dict:
            raise RuntimeError("pr create failed")

    def fake_clone_repo(clone_url: str, destination: Path) -> None:
        fixture_repo = Path(__file__).parent / "fixtures" / "toy_repo"
        shutil.copytree(fixture_repo, destination)
        _init_git_repo(destination)

    def fake_checkout_new_branch(repo_path: Path, branch_name: str, base_branch: str) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, text=True, capture_output=True).stdout.strip()

    monkeypatch.setattr(poller, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(poller, "ensure_workspace_root", lambda: workspace_tmp_dir)
    monkeypatch.setattr(poller, "GitHubAuthService", FakeAuthService)
    monkeypatch.setattr(poller, "GitHubPullRequestService", FailingPullRequestService)
    monkeypatch.setattr(poller, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(poller, "AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(poller, "clone_repo", fake_clone_repo)
    monkeypatch.setattr(poller, "checkout_new_branch", fake_checkout_new_branch)
    monkeypatch.setattr(poller, "commit_all", lambda repo_path, message, include_paths=None: "head-sha")
    monkeypatch.setattr(poller, "push_branch", lambda repo_path, branch_name: None)

    asyncio.run(poller.process_task(task.id))

    db = SessionLocal()
    refreshed_task = db.get(type(task), task.id)
    assert refreshed_task is not None
    assert refreshed_task.status == TaskStatus.failed
    assert refreshed_task.attempt_count == 1
    assert refreshed_task.head_commit == "head-sha"
    assert refreshed_task.branch_name is not None
    assert refreshed_task.failure_reason["reason"] == "pr_failed"
    assert "pr create failed" in refreshed_task.failure_reason["details"]["error"]
    assert len(refreshed_task.attempts) == 1
    assert refreshed_task.attempts[0].result_status == TaskResultStatus.success
    db.close()


def test_process_task_marks_failed_when_push_fails_after_tests_pass(sample_issue_payload, workspace_tmp_dir, monkeypatch) -> None:
    db = SessionLocal()
    task = create_task_from_webhook(db, sample_issue_payload)
    db.close()

    class FakeSettings:
        max_attempts = 3
        pr_review_label = "needs-human-review"

    class FakeAuthService:
        async def get_installation_token(self, installation_id: int) -> str:
            return "token"

    class FakeSandboxRunner:
        def install_dependencies(self, repo_path: Path, install_command: str):
            return type("Result", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

        def run_tests(self, repo_path: Path, test_command: str):
            process = subprocess.run(
                [sys.executable, *test_command.split()[1:]],
                cwd=repo_path,
                text=True,
                capture_output=True,
                check=False,
            )
            return type("Result", (), {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})()

    class FakeAgentLoop:
        def run(self, toolbox, retry_context=None):
            toolbox.dispatch(
                "write_file",
                json.dumps(
                    {
                        "path": "app/display.py",
                        "content": 'def format_display_name(name: str | None) -> str:\n    if name is None:\n        return ""\n    return name.strip().title()\n',
                    }
                ),
            )
            return AgentRunResult(
                summary={"root_cause": "x", "patch_plan": ["y"]},
                patch_text="",
                pr_title="fix: handle None display names",
                pr_body_summary={"root_cause": "x", "changes": ["y"]},
            )

    class FakePullRequestService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_pull_request(self, **kwargs) -> dict:
            return {"number": 103, "html_url": "https://example.com/pr/103"}

    def fake_clone_repo(clone_url: str, destination: Path) -> None:
        fixture_repo = Path(__file__).parent / "fixtures" / "toy_repo"
        shutil.copytree(fixture_repo, destination)
        _init_git_repo(destination)

    def fake_checkout_new_branch(repo_path: Path, branch_name: str, base_branch: str) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, text=True, capture_output=True).stdout.strip()

    monkeypatch.setattr(poller, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(poller, "ensure_workspace_root", lambda: workspace_tmp_dir)
    monkeypatch.setattr(poller, "GitHubAuthService", FakeAuthService)
    monkeypatch.setattr(poller, "GitHubPullRequestService", FakePullRequestService)
    monkeypatch.setattr(poller, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(poller, "AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(poller, "clone_repo", fake_clone_repo)
    monkeypatch.setattr(poller, "checkout_new_branch", fake_checkout_new_branch)
    monkeypatch.setattr(poller, "commit_all", lambda repo_path, message, include_paths=None: "head-sha")
    monkeypatch.setattr(poller, "push_branch", lambda repo_path, branch_name: (_ for _ in ()).throw(RuntimeError("git_push_failed: remote rejected")))

    asyncio.run(poller.process_task(task.id))

    db = SessionLocal()
    refreshed_task = db.get(type(task), task.id)
    assert refreshed_task is not None
    assert refreshed_task.status == TaskStatus.failed
    assert refreshed_task.head_commit == "head-sha"
    assert refreshed_task.failure_reason["reason"] == "pr_failed"
    assert "git_push_failed: remote rejected" in refreshed_task.failure_reason["details"]["error"]
    assert len(refreshed_task.attempts) == 1
    assert refreshed_task.attempts[0].result_status == TaskResultStatus.success
    db.close()


def test_process_task_passes_integration_context_to_agent(sample_issue_payload, workspace_tmp_dir, monkeypatch) -> None:
    db = SessionLocal()
    source_task_a = create_task_from_webhook(db, sample_issue_payload)
    second_payload = deepcopy(sample_issue_payload)
    second_payload["issue"]["id"] = sample_issue_payload["issue"]["id"] + 1
    second_payload["issue"]["number"] = sample_issue_payload["issue"]["number"] + 1
    second_payload["issue"]["title"] = "Add a default return value"
    second_payload["issue"]["body"] = "Add default=None support to safe_divide and keep tests passing."
    second_payload["issue"]["html_url"] = "https://github.com/octo/demo-repo/issues/2"
    source_task_b = create_task_from_webhook(db, second_payload)

    for index, source_task in enumerate((source_task_a, source_task_b), start=1):
        source_task.status = TaskStatus.done
        source_task.pr_number = 200 + index
        source_task.branch_name = f"agent/{index}"
        db.add(source_task)
        db.add(
            poller.TaskArtifact(
                task_id=source_task.id,
                artifact_type=TaskArtifactType.model_response,
                content={"summary": {"root_cause": f"source-{index}", "patch_plan": [f"change-{index}"]}},
            )
        )
        db.add(
            poller.TaskArtifact(
                task_id=source_task.id,
                artifact_type=TaskArtifactType.diff,
                content={"diff": f"diff --git a/app.py b/app.py\n+source {index}\n"},
            )
        )
        db.add(
            poller.TaskArtifact(
                task_id=source_task.id,
                artifact_type=TaskArtifactType.pr_body,
                content={"body": f"## Changes\n- change-{index}\n"},
            )
        )
    db.commit()

    integration_task = create_integration_task(db, [source_task_a, source_task_b], "Prefer source task A API shape.")
    db.close()

    class FakeSettings:
        max_attempts = 3
        pr_review_label = "needs-human-review"

    class FakeAuthService:
        async def get_installation_token(self, installation_id: int) -> str:
            return "token"

    class FakeSandboxRunner:
        def install_dependencies(self, repo_path: Path, install_command: str):
            return type("Result", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

        def run_tests(self, repo_path: Path, test_command: str):
            process = subprocess.run(
                [sys.executable, *test_command.split()[1:]],
                cwd=repo_path,
                text=True,
                capture_output=True,
                check=False,
            )
            return type("Result", (), {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})()

    class FakeAgentLoop:
        def run(self, toolbox, retry_context=None):
            issue_context = toolbox.get_issue_context()
            assert issue_context["mode"] == "integration"
            assert issue_context["default_branch"] == "main"
            assert issue_context["integration_request"]["guidance"] == "Prefer source task A API shape."
            assert len(issue_context["integration_request"]["source_prs"]) == 2
            assert issue_context["integration_request"]["source_prs"][0]["diff"].startswith("diff --git")
            toolbox.dispatch("read_file", json.dumps({"path": "app/display.py"}))
            toolbox.dispatch(
                "write_file",
                json.dumps(
                    {
                        "path": "app/display.py",
                        "content": 'def format_display_name(name: str | None) -> str:\n    if name is None:\n        return ""\n    return name.strip().title()\n',
                    }
                ),
            )
            return AgentRunResult(
                summary={"root_cause": "integration", "patch_plan": ["combine selected behavior"]},
                patch_text="",
                pr_title="feat: integrate selected PRs",
                pr_body_summary={"root_cause": "Integrated overlapping PR behavior.", "changes": ["Combined the selected changes on top of main."]},
            )

    class FakePullRequestService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_pull_request(self, **kwargs) -> dict:
            return {"number": 250, "html_url": "https://example.com/pr/250"}

    def fake_clone_repo(clone_url: str, destination: Path) -> None:
        fixture_repo = Path(__file__).parent / "fixtures" / "toy_repo"
        shutil.copytree(fixture_repo, destination)
        _init_git_repo(destination)

    def fake_checkout_new_branch(repo_path: Path, branch_name: str, base_branch: str) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, text=True, capture_output=True).stdout.strip()

    monkeypatch.setattr(poller, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(poller, "ensure_workspace_root", lambda: workspace_tmp_dir)
    monkeypatch.setattr(poller, "GitHubAuthService", FakeAuthService)
    monkeypatch.setattr(poller, "GitHubPullRequestService", FakePullRequestService)
    monkeypatch.setattr(poller, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(poller, "AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(poller, "clone_repo", fake_clone_repo)
    monkeypatch.setattr(poller, "checkout_new_branch", fake_checkout_new_branch)
    monkeypatch.setattr(poller, "commit_all", lambda repo_path, message, include_paths=None: "head-sha")
    monkeypatch.setattr(poller, "push_branch", lambda repo_path, branch_name: None)

    asyncio.run(poller.process_task(integration_task.id))

    db = SessionLocal()
    refreshed_task = db.get(type(integration_task), integration_task.id)
    assert refreshed_task is not None
    assert refreshed_task.status == TaskStatus.done
    assert refreshed_task.pr_number == 250
    assert refreshed_task.issue.github_issue_number == 0
    db.close()


def test_conflict_resolution_task_marks_original_pr_superseded(sample_issue_payload, workspace_tmp_dir, monkeypatch) -> None:
    db = SessionLocal()
    source_task = create_task_from_webhook(db, sample_issue_payload)
    source_task.status = TaskStatus.done
    source_task.pr_number = 301
    source_task.branch_name = "agent/original"
    source_task_id = source_task.id
    db.add(source_task)
    db.add(
        poller.TaskArtifact(
            task_id=source_task.id,
            artifact_type=TaskArtifactType.model_response,
            content={"summary": {"root_cause": "original", "patch_plan": ["original change"]}},
        )
    )
    db.add(
        poller.TaskArtifact(
            task_id=source_task.id,
            artifact_type=TaskArtifactType.diff,
            content={"diff": "diff --git a/app/display.py b/app/display.py\n+source\n"},
        )
    )
    db.add(
        poller.TaskArtifact(
            task_id=source_task.id,
            artifact_type=TaskArtifactType.pr_body,
            content={"body": "## Changes\n- original change\n"},
        )
    )
    db.commit()
    resolution_task = create_integration_task(db, [source_task], "Keep behavior but resolve conflicts.", mode="conflict_resolution")
    db.close()

    class FakeSettings:
        max_attempts = 3
        pr_review_label = "needs-human-review"

    class FakeAuthService:
        async def get_installation_token(self, installation_id: int) -> str:
            return "token"

    class FakeIssueService:
        comments: list[tuple[int, str]] = []

        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_comment(self, owner: str, repo: str, issue_number: int, body: str) -> None:
            type(self).comments.append((issue_number, body))

        async def add_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> None:
            return None

    class FakeSandboxRunner:
        def install_dependencies(self, repo_path: Path, install_command: str):
            return type("Result", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

        def run_tests(self, repo_path: Path, test_command: str):
            process = subprocess.run(
                [sys.executable, *test_command.split()[1:]],
                cwd=repo_path,
                text=True,
                capture_output=True,
                check=False,
            )
            return type("Result", (), {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})()

    class FakeAgentLoop:
        def run(self, toolbox, retry_context=None):
            toolbox.dispatch("read_file", json.dumps({"path": "app/display.py"}))
            toolbox.dispatch(
                "write_file",
                json.dumps(
                    {
                        "path": "app/display.py",
                        "content": 'def format_display_name(name: str | None) -> str:\n    if name is None:\n        return ""\n    return name.strip().title()\n',
                    }
                ),
            )
            return AgentRunResult(
                summary={"root_cause": "resolved", "patch_plan": ["resolve conflict on top of main"]},
                patch_text="",
                pr_title="fix: resolve conflicted PR",
                pr_body_summary={"root_cause": "Resolved the conflicting change against main.", "changes": ["Re-applied the original behavior on top of the latest base branch."]},
            )

    class FakePullRequestService:
        def __init__(self, installation_token: str) -> None:
            self.installation_token = installation_token

        async def create_pull_request(self, **kwargs) -> dict:
            return {"number": 302, "html_url": "https://example.com/pr/302"}

    def fake_clone_repo(clone_url: str, destination: Path) -> None:
        fixture_repo = Path(__file__).parent / "fixtures" / "toy_repo"
        shutil.copytree(fixture_repo, destination)
        _init_git_repo(destination)

    def fake_checkout_new_branch(repo_path: Path, branch_name: str, base_branch: str) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, check=True, text=True, capture_output=True).stdout.strip()

    monkeypatch.setattr(poller, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(poller, "ensure_workspace_root", lambda: workspace_tmp_dir)
    monkeypatch.setattr(poller, "GitHubAuthService", FakeAuthService)
    monkeypatch.setattr(poller, "GitHubIssueService", FakeIssueService)
    monkeypatch.setattr(poller, "GitHubPullRequestService", FakePullRequestService)
    monkeypatch.setattr(poller, "SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(poller, "AgentLoop", FakeAgentLoop)
    monkeypatch.setattr(poller, "clone_repo", fake_clone_repo)
    monkeypatch.setattr(poller, "checkout_new_branch", fake_checkout_new_branch)
    monkeypatch.setattr(poller, "commit_all", lambda repo_path, message, include_paths=None: "head-sha")
    monkeypatch.setattr(poller, "push_branch", lambda repo_path, branch_name: None)

    asyncio.run(poller.process_task(resolution_task.id))

    db = SessionLocal()
    refreshed_source_task = db.get(type(source_task), source_task_id)
    refreshed_resolution_task = db.get(type(resolution_task), resolution_task.id)
    assert refreshed_source_task is not None
    assert refreshed_resolution_task is not None
    source_link = next(
        artifact for artifact in refreshed_source_task.artifacts if artifact.artifact_type == TaskArtifactType.resolution_link
    )
    assert source_link.content["resolved_pr_number"] == 302
    assert source_link.content["resolved_task_id"] == resolution_task.id
    assert refreshed_resolution_task.pr_number == 302
    assert FakeIssueService.comments
    assert FakeIssueService.comments[0][0] == 301
    assert "Replacement PR: https://example.com/pr/302" in FakeIssueService.comments[0][1]
    db.close()
