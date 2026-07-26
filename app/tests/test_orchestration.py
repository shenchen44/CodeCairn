import shutil
import subprocess
from pathlib import Path

from app.services.openai.agent_loop import AgentRunResult
from app.services.openai.contracts import (
    ExecutionMode,
    SupervisorDecision,
)
from app.services.openai.policy import get_runtime_policy
from app.services.openai.tools import AgentToolbox
from app.services.orchestration import (
    PatchTournament,
    attach_verification,
    build_evidence_ledger,
    compile_agent_graph,
)
from app.services.sandbox.repo_config import load_repo_config


def test_compile_agent_graph_builds_deep_review_dependencies() -> None:
    route = SupervisorDecision(
        variant="full",
        mode=ExecutionMode.deep_review,
        complexity_score=0.8,
        reasons=["integration_task"],
        required_agents=[
            "localization",
            "planner",
            "patch",
            "reviewer",
        ],
    )

    graph = compile_agent_graph(
        {"title": "Integrate API changes", "instance_id": "case-1"},
        route,
        get_runtime_policy("full"),
    )

    assert graph.strategy == "deep_review"
    assert [node.id for node in graph.nodes] == [
        "localization",
        "planning",
        "patch_minimal",
        "patch_challenger",
        "patch_selector",
        "patch_recovery",
        "review",
    ]
    assert graph.nodes[2].depends_on == ["planning"]
    assert graph.nodes[4].depends_on == [
        "patch_minimal",
        "patch_challenger",
    ]
    assert graph.nodes[5].depends_on == ["patch_selector"]
    assert graph.nodes[6].depends_on == ["patch_recovery"]
    assert graph.total_token_budget == 296_001


def test_full_standard_graph_defers_review_without_forcing_planning() -> None:
    route = SupervisorDecision(
        variant="full",
        mode=ExecutionMode.standard,
        complexity_score=0.3,
        reasons=[],
        required_agents=["localization", "patch"],
    )

    graph = compile_agent_graph(
        {"title": "Fix formatting", "instance_id": "case-2"},
        route,
        get_runtime_policy("full"),
    )

    assert [node.id for node in graph.nodes] == [
        "localization",
        "patch",
        "patch_recovery",
        "review",
    ]
    assert "planning" not in {node.id for node in graph.nodes}
    assert graph.nodes[-1].depends_on == ["patch_recovery"]


def test_evidence_ledger_links_requirements_claims_and_hunks() -> None:
    ledger = build_evidence_ledger(
        issue_context={
            "title": "Handle None display names",
            "body": "Expected behavior: None returns an empty string.",
        },
        localization={
            "root_cause_hypothesis": "The function calls strip on None.",
            "evidence": [
                {
                    "path": "app/display.py",
                    "line": 2,
                    "symbol": "format_display_name",
                    "reason": "The nullable value is dereferenced.",
                }
            ],
        },
        summary=None,
        diff_text=(
            "diff --git a/app/display.py b/app/display.py\n"
            "--- a/app/display.py\n"
            "+++ b/app/display.py\n"
            "@@ -1,2 +1,4 @@\n"
            "+    if name is None:\n"
            "+        return \"\"\n"
        ),
        require_grounded_evidence=True,
    )

    assert ledger.gate is not None
    assert ledger.gate.passed is True
    assert ledger.gate.requirement_coverage == 1.0
    assert ledger.gate.patch_claim_coverage == 1.0
    assert ledger.patch_hunks[0].claim_ids == [ledger.claims[0].id]
    assert ledger.claims[0].evidence_ids == [ledger.evidence[0].id]


def test_verification_gate_requires_passing_test_evidence() -> None:
    ledger = build_evidence_ledger(
        issue_context={"title": "Fix value formatting"},
        localization=None,
        summary={"root_cause": "Formatting omits a guard."},
        diff_text=(
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        require_grounded_evidence=False,
    )

    failed = attach_verification(
        ledger.model_dump(mode="json"),
        command="pytest -q",
        exit_code=1,
        stderr="assertion failed",
    )
    passed = attach_verification(
        ledger.model_dump(mode="json"),
        command="pytest -q",
        exit_code=0,
        stdout="1 passed",
    )

    assert failed["gate"]["passed"] is False
    assert failed["gate"]["verification_coverage"] == 0.0
    assert passed["gate"]["passed"] is True
    assert passed["gate"]["verification_coverage"] == 1.0


def test_patch_tournament_selects_smaller_verified_candidate(
    workspace_tmp_dir,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "toy_repo"
    repo_path = workspace_tmp_dir / "tournament_repo"
    shutil.copytree(fixture, repo_path)
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "tests@example.com"],
        ["git", "config", "user.name", "tests"],
        ["git", "add", "."],
        ["git", "commit", "-m", "init"],
    ):
        subprocess.run(
            command,
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    class PassingRunner:
        def run_tests(self, repo_path, test_command):
            return type(
                "Result",
                (),
                {"exit_code": 0, "stdout": "2 passed", "stderr": ""},
            )()

    class CandidateLoop:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, toolbox, **kwargs):
            self.calls += 1
            if self.calls == 1:
                content = (
                    "def format_display_name(name: str | None) -> str:\n"
                    "    if name is None:\n"
                    "        return \"\"\n"
                    "    return name.strip().title()\n"
                )
            else:
                content = (
                    "def format_display_name(name: str | None) -> str:\n"
                    "    if name is None:\n"
                    "        return \"\"\n"
                    "    cleaned = name.strip()\n"
                    "    if not cleaned:\n"
                    "        return \"\"\n"
                    "    return cleaned.title()\n"
                )
            toolbox.write_file("app/display.py", content)
            return AgentRunResult(
                summary={"root_cause": "Missing nullable guard."},
                patch_text="",
                pr_title="fix: handle None",
                pr_body_summary={},
                model_call_count=1,
                tool_call_count=1,
                total_input_tokens=100,
                total_output_tokens=50,
            )

    toolbox = AgentToolbox(
        repo_path=repo_path,
        repo_config=load_repo_config(repo_path),
        issue_context={"title": "Handle None display names"},
        sandbox_runner=PassingRunner(),
        runtime_policy=get_runtime_policy("full"),
    )
    selection = PatchTournament(CandidateLoop()).run(
        toolbox,
        localization_context={
            "root_cause_hypothesis": "Missing nullable guard.",
            "evidence": [
                {
                    "path": "app/display.py",
                    "line": 2,
                    "reason": "strip is called without a guard",
                }
            ],
        },
        planning_context=None,
        retry_context=None,
    )

    assert selection.tournament.selected_candidate_id == "minimal"
    assert len(selection.tournament.candidates) == 2
    assert "if name is None" in (
        repo_path / "app" / "display.py"
    ).read_text(encoding="utf-8")
    assert "cleaned =" not in (
        repo_path / "app" / "display.py"
    ).read_text(encoding="utf-8")
