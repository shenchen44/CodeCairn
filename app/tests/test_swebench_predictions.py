import subprocess
from dataclasses import dataclass
from pathlib import Path

from experiments.swe_alignment.schema import SWEInstance
from scripts import run_swebench_lite_predictions as predictions


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "app.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "tests"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, text=True)


@dataclass
class FakeResult:
    patch_text: str
    model_call_count: int = 1
    tool_call_count: int = 2
    summary: dict | None = None
    pr_body_summary: dict | None = None

    def __post_init__(self) -> None:
        if self.summary is None:
            self.summary = {}
        if self.pr_body_summary is None:
            self.pr_body_summary = {}


class FakeToolbox:
    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path

    def apply_patch(self, patch_text: str) -> None:
        if "bad patch" in patch_text:
            raise RuntimeError("git_apply_failed: corrupt patch")
        (self.repo_path / "app.py").write_text("new\n", encoding="utf-8")


def test_agent_retry_repairs_invalid_patch(monkeypatch, workspace_tmp_dir) -> None:
    repo_path = workspace_tmp_dir / "repo"
    _init_repo(repo_path)
    instance = SWEInstance(instance_id="demo__repo-1", problem_statement="Fix app.py")

    class FakeAgent:
        calls = 0

        def run(self, toolbox, retry_context=None):
            FakeAgent.calls += 1
            if FakeAgent.calls == 1:
                return FakeResult("bad patch")
            assert retry_context is not None
            assert "invalid patch" in retry_context
            return FakeResult("good patch")

    monkeypatch.setattr(predictions, "AgentLoop", FakeAgent)

    result, attempted_patch, error, applied, model_calls, tool_calls, attempts = predictions._run_agent_with_retry(
        instance,
        FakeToolbox(repo_path),
        repo_path,
        use_localization_gate=False,
    )

    assert applied is True
    assert error == ""
    assert attempted_patch == "good patch"
    assert result.patch_text == "good patch"
    assert model_calls == 2
    assert tool_calls == 4
    assert [attempt["kind"] for attempt in attempts] == ["initial", "patch_repair"]
    assert "new" in (repo_path / "app.py").read_text(encoding="utf-8")


def test_agent_retry_handles_empty_initial_patch(monkeypatch, workspace_tmp_dir) -> None:
    repo_path = workspace_tmp_dir / "repo"
    _init_repo(repo_path)
    instance = SWEInstance(instance_id="demo__repo-2", problem_statement="Fix app.py")

    class FakeAgent:
        calls = 0

        def run(self, toolbox, retry_context=None):
            FakeAgent.calls += 1
            if FakeAgent.calls == 1:
                return FakeResult("")
            assert retry_context is not None
            assert "did not produce any patch" in retry_context
            return FakeResult("good patch")

    monkeypatch.setattr(predictions, "AgentLoop", FakeAgent)

    _, attempted_patch, error, applied, _, _, attempts = predictions._run_agent_with_retry(
        instance,
        FakeToolbox(repo_path),
        repo_path,
        use_localization_gate=False,
    )

    assert applied is True
    assert error == ""
    assert attempted_patch == "good patch"
    assert [attempt["kind"] for attempt in attempts] == ["initial", "no_patch_retry"]


def test_agent_repairs_invalid_patch_after_empty_retry(monkeypatch, workspace_tmp_dir) -> None:
    repo_path = workspace_tmp_dir / "repo"
    _init_repo(repo_path)
    instance = SWEInstance(instance_id="demo__repo-3", problem_statement="Fix app.py")

    class FakeAgent:
        calls = 0

        def run(self, toolbox, retry_context=None):
            FakeAgent.calls += 1
            if FakeAgent.calls == 1:
                return FakeResult("")
            if FakeAgent.calls == 2:
                assert retry_context is not None
                assert "did not produce any patch" in retry_context
                return FakeResult("bad patch")
            assert retry_context is not None
            assert "invalid patch" in retry_context
            return FakeResult("good patch")

    monkeypatch.setattr(predictions, "AgentLoop", FakeAgent)

    _, attempted_patch, error, applied, model_calls, tool_calls, attempts = predictions._run_agent_with_retry(
        instance,
        FakeToolbox(repo_path),
        repo_path,
        use_localization_gate=False,
    )

    assert applied is True
    assert error == ""
    assert attempted_patch == "good patch"
    assert model_calls == 3
    assert tool_calls == 6
    assert [attempt["kind"] for attempt in attempts] == ["initial", "no_patch_retry", "patch_repair"]


def test_agent_uses_localization_gate_before_patch(monkeypatch, workspace_tmp_dir) -> None:
    repo_path = workspace_tmp_dir / "repo"
    _init_repo(repo_path)
    instance = SWEInstance(instance_id="demo__repo-4", problem_statement="Fix app.py")

    class FakeAgent:
        calls = 0

        def run(self, toolbox, retry_context=None):
            FakeAgent.calls += 1
            if FakeAgent.calls == 1:
                assert retry_context is not None
                assert "structured localization" in retry_context.lower()
                return FakeResult(
                    "",
                    summary={
                        "suspect_files": ["app.py"],
                        "target_symbols": ["format_value"],
                        "root_cause": "old behavior",
                        "edit_plan": ["change old to new"],
                        "patch_strategy": "single-file edit",
                        "confidence": 0.7,
                    },
                )
            assert retry_context is not None
            assert "structured_localization_plan" in retry_context
            assert "app.py" in retry_context
            return FakeResult("good patch")

    monkeypatch.setattr(predictions, "AgentLoop", FakeAgent)

    _, attempted_patch, error, applied, model_calls, tool_calls, attempts = predictions._run_agent_with_retry(
        instance,
        FakeToolbox(repo_path),
        repo_path,
    )

    assert applied is True
    assert error == ""
    assert attempted_patch == "good patch"
    assert model_calls == 2
    assert tool_calls == 4
    assert attempts[0]["kind"] == "localization_gate"
    assert attempts[0]["actionable"] is True
    assert attempts[0]["plan"]["suspect_files"] == ["app.py"]


def test_agent_forces_patch_from_actionable_plan(monkeypatch, workspace_tmp_dir) -> None:
    repo_path = workspace_tmp_dir / "repo"
    _init_repo(repo_path)
    instance = SWEInstance(instance_id="demo__repo-5", problem_statement="Fix app.py")

    class FakeClient:
        def create_completion(self, messages, tools):
            assert tools == []
            return type(
                "Response",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "message": type(
                                    "Message",
                                    (),
                                    {
                                        "content": '{"summary": {}, "patch_text": "good patch", "pr_title": "fix", "pr_body_summary": {}}',
                                        "tool_calls": None,
                                    },
                                )()
                            },
                        )()
                    ]
                },
            )()

    class FakeAgent:
        calls = 0

        def __init__(self):
            self.client = FakeClient()

        def run(self, toolbox, retry_context=None):
            FakeAgent.calls += 1
            if FakeAgent.calls == 1:
                return FakeResult(
                    "",
                    summary={
                        "suspect_files": ["app.py"],
                        "target_symbols": ["format_value"],
                        "root_cause": "old behavior",
                        "edit_plan": ["change old to new"],
                        "patch_strategy": "single-file edit",
                        "confidence": 0.9,
                    },
                )
            return FakeResult("")

    monkeypatch.setattr(predictions, "AgentLoop", FakeAgent)

    _, attempted_patch, error, applied, model_calls, _, attempts = predictions._run_agent_with_retry(
        instance,
        FakeToolbox(repo_path),
        repo_path,
    )

    assert applied is True
    assert error == ""
    assert attempted_patch == "good patch"
    assert model_calls == 4
    assert [attempt["kind"] for attempt in attempts] == [
        "localization_gate",
        "initial",
        "no_patch_retry",
        "plan_forced_patch",
    ]
