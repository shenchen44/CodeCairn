import json
from types import SimpleNamespace

import pytest

from app.db.models.task import TaskArtifactType, TaskResultStatus
from app.services.openai.contracts import (
    CodeEvidence,
    LocalizationResult,
    LocalizationStatus,
    evaluate_localization_gate,
)
from app.services.openai.staged_runtime import (
    ExecutionClosureError,
    LocalizationGateError,
    PhaseGateError,
    ReadOnlyToolbox,
    StagedAgentRuntime,
    route_task,
)
from app.services.openai.policy import get_runtime_policy
from app.services.task_runner.orchestrator import create_task_from_webhook
from app.workers.poller import _record_attempt


def _usage(input_tokens: int = 100, output_tokens: int = 50):
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
    )


def _final_response(content: str, input_tokens: int = 100, output_tokens: int = 50):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None)
            )
        ],
        usage=_usage(input_tokens, output_tokens),
    )


def _tool_response(name: str, arguments: str = "{}"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            id="localize-1",
                            function=SimpleNamespace(
                                name=name,
                                arguments=arguments,
                            ),
                        )
                    ],
                )
            )
        ],
        usage=_usage(),
    )


LOCALIZATION_JSON = """
{
  "contract_version": "1",
  "status": "ready",
  "issue_summary": "None input crashes display formatting",
  "candidate_files": ["app/display.py"],
  "suspected_symbols": ["format_display_name"],
  "evidence": [
    {
      "path": "app/display.py",
      "line": 2,
      "symbol": "format_display_name",
      "reason": "The function calls strip without guarding None"
    }
  ],
  "root_cause_hypothesis": "The input contract permits None but the implementation assumes str",
  "confidence": 0.92,
  "missing_information": []
}
"""

PATCH_JSON = """
{
  "summary": {"root_cause": "missing None guard"},
  "patch_text": "diff --git a/app/display.py b/app/display.py\\n--- a/app/display.py\\n+++ b/app/display.py\\n@@ -1 +1 @@\\n-old\\n+new",
  "pr_title": "fix: handle None display names",
  "pr_body_summary": {"changes": ["add guard"]}
}
"""

_empty_patch_payload = json.loads(PATCH_JSON)
_empty_patch_payload["patch_text"] = ""
EMPTY_PATCH_JSON = json.dumps(_empty_patch_payload)

RECOVERY_JSON = """
{
  "contract_version": "1",
  "selected_hypothesis": "The nullable input is dereferenced",
  "rejected_hypotheses": ["The caller always normalizes input"],
  "behavior_contracts": ["None returns an empty string"],
  "operations": [
    {
      "path": "app/display.py",
      "old_text": "return name.strip().title()",
      "new_text": "if name is None:\\n        return \\"\\"\\n    return name.strip().title()",
      "rationale": "Guard the nullable value before string operations"
    }
  ],
  "test_expectation": "None returns empty and strings keep title formatting"
}
"""

PLAN_JSON = """
{
  "contract_version": "1",
  "objective": "Handle nullable display names without changing normal formatting",
  "steps": [
    {
      "order": 1,
      "description": "Add a None guard",
      "files": ["app/display.py"],
      "rationale": "The localized function dereferences a nullable value"
    }
  ],
  "test_strategy": ["Run the display tests", "Run the full suite"],
  "risk_level": "low",
  "rollback_strategy": "Revert the guard"
}
"""

REVIEW_JSON = """
{
  "contract_version": "1",
  "verdict": "approved",
  "summary": "The patch is scoped to the localized function",
  "findings": [],
  "confidence": 0.91
}
"""


class FakeToolbox:
    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.diff = ""

    @staticmethod
    def get_issue_context() -> dict:
        return {"title": "display crash", "body": "None crashes"}

    @staticmethod
    def tool_schemas() -> list[dict]:
        names = ["search_code", "read_file", "write_file", "run_tests"]
        return [
            {
                "type": "function",
                "function": {"name": name, "parameters": {"type": "object"}},
            }
            for name in names
        ]

    def dispatch(self, name: str, arguments_json: str) -> dict:
        self.dispatched.append(name)
        if name == "git_diff":
            return {"diff": self.diff}
        if name == "apply_patch":
            self.diff = json.loads(arguments_json)["unified_diff"]
            return {"diff": self.diff}
        return {"matches": [{"path": "app/display.py", "line": 2}]}


def test_localization_gate_accepts_grounded_result() -> None:
    result = LocalizationResult(
        status=LocalizationStatus.ready,
        issue_summary="Crash on None",
        candidate_files=["app/display.py"],
        evidence=[
            CodeEvidence(
                path="app/display.py",
                line=2,
                reason="Calls strip on the nullable value",
            )
        ],
        root_cause_hypothesis="Missing None guard",
        confidence=0.9,
    )

    decision = evaluate_localization_gate(result)

    assert decision.passed is True
    assert decision.reasons == []


def test_localization_gate_reports_all_grounding_failures() -> None:
    result = LocalizationResult(
        status=LocalizationStatus.insufficient,
        issue_summary="Unclear failure",
        candidate_files=["app/display.py"],
        evidence=[
            CodeEvidence(
                path="app/other.py",
                reason="Weak textual match",
            )
        ],
        confidence=0.2,
    )

    decision = evaluate_localization_gate(result)

    assert decision.passed is False
    assert set(decision.reasons) == {
        "localization_status_not_ready",
        "root_cause_hypothesis_missing",
        "localization_confidence_below_threshold",
        "candidate_files_not_grounded_by_evidence",
    }


def test_read_only_toolbox_hides_and_rejects_mutating_tools() -> None:
    toolbox = FakeToolbox()
    readonly = ReadOnlyToolbox(toolbox)

    exposed_names = {
        schema["function"]["name"] for schema in readonly.tool_schemas()
    }
    rejected = readonly.dispatch("write_file", '{"path":"app/display.py"}')

    assert exposed_names == {"search_code", "read_file"}
    assert rejected["error"] == "tool_not_allowed_in_localization:write_file"
    assert toolbox.dispatched == []


def test_staged_runtime_localizes_before_patching_and_merges_metrics() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[list[dict], list[dict]]] = []

        def create_completion(self, messages, tools):
            self.calls.append((messages, tools))
            if len(self.calls) == 1:
                return _tool_response(
                    "search_code",
                    '{"query":"format_display_name"}',
                )
            if len(self.calls) == 2:
                return _final_response(LOCALIZATION_JSON, 200, 80)
            return _final_response(PATCH_JSON, 300, 100)

    client = FakeClient()
    toolbox = FakeToolbox()

    result = StagedAgentRuntime(client=client).run(toolbox)

    assert toolbox.dispatched == [
        "git_diff",
        "search_code",
        "git_diff",
        "apply_patch",
        "git_diff",
    ]
    assert result.localization is not None
    assert result.localization["candidate_files"] == ["app/display.py"]
    assert result.model_call_count == 3
    assert result.tool_call_count == 1
    assert result.total_input_tokens == 600
    assert result.total_output_tokens == 230
    assert len(result.turn_durations_ms or []) == 3
    assert result.route_decision["mode"] == "standard"
    assert result.plan is None
    assert result.review is None
    assert result.agent_graph["strategy"] == "evidence_first"
    assert result.evidence_ledger["gate"]["passed"] is True
    assert [
        event["event_type"] for event in result.runtime_events
    ] == [
        "runtime_start",
        "phase_start",
        "phase_end",
        "phase_start",
        "phase_end",
        "gate_passed",
        "runtime_end",
    ]
    patch_messages = client.calls[2][0]
    assert any(
        "localization_result" in message.get("content", "")
        for message in patch_messages
    )


def test_staged_runtime_does_not_patch_when_localization_gate_fails() -> None:
    insufficient = LOCALIZATION_JSON.replace(
        '"confidence": 0.92',
        '"confidence": 0.2',
    )

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_completion(self, messages, tools):
            self.calls += 1
            return _final_response(insufficient)

    client = FakeClient()

    with pytest.raises(
        LocalizationGateError,
        match="localization_confidence_below_threshold",
    ) as error:
        StagedAgentRuntime(client=client).run(FakeToolbox())

    assert client.calls == 1
    assert error.value.localization["confidence"] == 0.2
    assert error.value.reasons == ["localization_confidence_below_threshold"]


def test_localization_schema_error_gets_one_repair_call() -> None:
    invalid_payload = json.loads(LOCALIZATION_JSON)
    invalid_payload["evidence"][0]["line"] = 0
    repaired_payload = json.loads(LOCALIZATION_JSON)
    repaired_payload["evidence"][0]["line"] = None

    class FakeClient:
        def __init__(self) -> None:
            self.responses = [
                _final_response(json.dumps(invalid_payload)),
                _final_response(json.dumps(repaired_payload)),
                _final_response(PATCH_JSON),
            ]

        def create_completion(self, messages, tools):
            return self.responses.pop(0)

    result = StagedAgentRuntime(client=FakeClient()).run(FakeToolbox())

    assert result.localization["evidence"][0]["line"] is None
    assert result.model_call_count == 3
    assert len(result.turn_durations_ms or []) == 3


def test_supervisor_routes_integration_tasks_to_deep_review() -> None:
    decision = route_task(
        {
            "mode": "integration",
            "title": "Integrate two fixes",
            "body": "Combine behavior safely",
        }
    )

    assert decision.mode.value == "deep_review"
    assert decision.required_agents == [
        "localization",
        "planner",
        "patch",
        "reviewer",
    ]


def test_legacy_policy_skips_localization_and_deep_review() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_completion(self, messages, tools):
            self.calls += 1
            return _final_response(PATCH_JSON)

    client = FakeClient()
    result = StagedAgentRuntime(
        client=client,
        policy=get_runtime_policy("legacy"),
    ).run(FakeToolbox())

    assert client.calls == 1
    assert result.localization is None
    assert result.route_decision["variant"] == "legacy"
    assert result.route_decision["required_agents"] == ["patch"]


def test_staged_runtime_rejects_success_without_repository_diff() -> None:
    class NoDiffToolbox(FakeToolbox):
        def __init__(self) -> None:
            super().__init__()
            self.diff = ""

    class FakeClient:
        def __init__(self) -> None:
            self.responses = [
                _final_response(LOCALIZATION_JSON),
                _final_response(EMPTY_PATCH_JSON),
            ]

        def create_completion(self, messages, tools):
            return self.responses.pop(0)

    with pytest.raises(
        ExecutionClosureError,
        match="no_repository_diff",
    ) as error:
        StagedAgentRuntime(client=FakeClient()).run(NoDiffToolbox())

    assert error.value.context["localization"]["confidence"] == 0.92
    assert error.value.run_result.model_call_count == 1


def test_staged_runtime_recovers_no_diff_with_exact_replacement() -> None:
    class RecoveryToolbox(FakeToolbox):
        runtime_policy = get_runtime_policy("full")

        def dispatch(self, name: str, arguments_json: str) -> dict:
            if name == "read_file":
                self.dispatched.append(name)
                return {
                    "content": (
                        "   1 | def format_display_name(name):\n"
                        "   2 |     return name.strip().title()"
                    )
                }
            if name == "replace_in_file":
                self.dispatched.append(name)
                arguments = json.loads(arguments_json)
                assert arguments["old_text"] == (
                    "return name.strip().title()"
                )
                self.diff = (
                    "diff --git a/app/display.py b/app/display.py\n"
                    "--- a/app/display.py\n"
                    "+++ b/app/display.py\n"
                    "@@ -1,2 +1,4 @@\n"
                    "+    if name is None:\n"
                    "+        return \"\"\n"
                )
                return {"diff": self.diff}
            return super().dispatch(name, arguments_json)

    class FakeClient:
        def __init__(self) -> None:
            self.responses = [
                _final_response(LOCALIZATION_JSON),
                _final_response(EMPTY_PATCH_JSON),
                _final_response(RECOVERY_JSON),
            ]

        def create_completion(self, messages, tools):
            return self.responses.pop(0)

    result = StagedAgentRuntime(
        client=FakeClient(),
        policy=get_runtime_policy("full"),
    ).run(RecoveryToolbox())

    assert result.recovery["status"] == "recovered"
    assert result.recovery["selected_hypothesis"].startswith(
        "The nullable"
    )
    recovery_node = next(
        node
        for node in result.agent_graph["nodes"]
        if node["id"] == "patch_recovery"
    )
    assert recovery_node["status"] == "completed"
    assert "replace_in_file" in result.recovery["operation"]["rationale"] or (
        result.recovery["operation"]["path"] == "app/display.py"
    )


def test_standard_review_revises_patch_for_competing_hypothesis() -> None:
    localization_payload = json.loads(LOCALIZATION_JSON)
    localization_payload["behavioral_contracts"] = [
        "None returns an empty string",
        "String formatting remains unchanged",
    ]
    localization_payload["alternative_hypotheses"] = [
        {
            "hypothesis": "Whitespace-only input is the actual failure",
            "evidence_for": ["The formatter strips input"],
            "evidence_against": ["The issue explicitly mentions None"],
            "falsification_test": "Call the formatter with None and whitespace",
        }
    ]
    localization_json = json.dumps(localization_payload)
    rejected_review = json.dumps(
        {
            "contract_version": "1",
            "verdict": "needs_revision",
            "summary": "The nullable behavior remains uncovered",
            "findings": [
                {
                    "severity": "high",
                    "path": "app/display.py",
                    "line": 2,
                    "message": "Add the localized None guard",
                }
            ],
            "confidence": 0.9,
            "behavior_contracts_covered": False,
            "hypotheses_considered": [
                "nullable input",
                "whitespace-only input",
            ],
            "test_gaps": ["None behavior is not addressed"],
        }
    )

    class RevisionToolbox(FakeToolbox):
        runtime_policy = get_runtime_policy("full")

        def dispatch(self, name: str, arguments_json: str) -> dict:
            if name == "read_file":
                self.dispatched.append(name)
                return {
                    "content": (
                        "   1 | def format_display_name(name):\n"
                        "   2 |     return name.strip().title()"
                    )
                }
            if name == "replace_in_file":
                self.dispatched.append(name)
                self.diff += (
                    "\n@@ -1,2 +1,4 @@\n"
                    "+    if name is None:\n"
                    "+        return \"\"\n"
                )
                return {"diff": self.diff}
            return super().dispatch(name, arguments_json)

    class FakeClient:
        def __init__(self) -> None:
            self.responses = [
                _final_response(localization_json),
                _final_response(PATCH_JSON),
                _final_response(rejected_review),
                _final_response(RECOVERY_JSON),
                _final_response(REVIEW_JSON),
            ]

        def create_completion(self, messages, tools):
            return self.responses.pop(0)

    result = StagedAgentRuntime(
        client=FakeClient(),
        policy=get_runtime_policy("full"),
    ).run(RevisionToolbox())

    assert result.route_decision["mode"] == "standard"
    assert result.plan is None
    assert result.review["verdict"] == "approved"
    assert result.recovery["status"] == "recovered"
    assert result.model_call_count == 5
    review_node = next(
        node
        for node in result.agent_graph["nodes"]
        if node["id"] == "review"
    )
    assert review_node["status"] == "completed"


def test_staged_runtime_applies_returned_patch_before_closing() -> None:
    patch_text = "diff --git a/app/display.py b/app/display.py\n+fixed"
    patch_payload = json.loads(PATCH_JSON)
    patch_payload["patch_text"] = patch_text
    patch_json = json.dumps(patch_payload)

    class NoDiffToolbox(FakeToolbox):
        def __init__(self) -> None:
            super().__init__()
            self.diff = ""

    class FakeClient:
        def __init__(self) -> None:
            self.responses = [
                _final_response(LOCALIZATION_JSON),
                _final_response(patch_json),
            ]

        def create_completion(self, messages, tools):
            return self.responses.pop(0)

    toolbox = NoDiffToolbox()
    result = StagedAgentRuntime(client=FakeClient()).run(toolbox)

    assert result.patch_text == patch_text
    assert "apply_patch" in toolbox.dispatched
    assert toolbox.diff == patch_text


def test_deep_review_route_runs_planner_and_reviewer() -> None:
    class DeepToolbox(FakeToolbox):
        @staticmethod
        def get_issue_context() -> dict:
            return {
                "mode": "integration",
                "title": "Integrate display fixes",
                "body": "Combine both changes",
            }

    class FakeClient:
        def __init__(self) -> None:
            self.responses = [
                _final_response(LOCALIZATION_JSON),
                _final_response(PLAN_JSON),
                _final_response(PATCH_JSON),
                _final_response(REVIEW_JSON),
            ]

        def create_completion(self, messages, tools):
            return self.responses.pop(0)

    result = StagedAgentRuntime(client=FakeClient()).run(DeepToolbox())

    assert result.route_decision["mode"] == "deep_review"
    assert result.plan["risk_level"] == "low"
    assert result.review["verdict"] == "approved"
    assert result.model_call_count == 4
    assert result.tool_call_count == 1
    assert len(result.turn_durations_ms or []) == 4


def test_reviewer_can_reject_patch_before_external_test_phase() -> None:
    rejected_review = REVIEW_JSON.replace(
        '"verdict": "approved"',
        '"verdict": "needs_revision"',
    )

    class DeepToolbox(FakeToolbox):
        @staticmethod
        def get_issue_context() -> dict:
            return {
                "mode": "integration",
                "title": "Integrate display fixes",
                "body": "Combine both changes",
            }

    class FakeClient:
        def __init__(self) -> None:
            self.responses = [
                _final_response(LOCALIZATION_JSON),
                _final_response(PLAN_JSON),
                _final_response(PATCH_JSON),
                _final_response(rejected_review),
            ]

        def create_completion(self, messages, tools):
            return self.responses.pop(0)

    with pytest.raises(PhaseGateError, match="review_needs_revision") as error:
        StagedAgentRuntime(client=FakeClient()).run(DeepToolbox())

    assert error.value.phase == "review"
    assert error.value.context["plan"]["objective"].startswith("Handle")


def test_localization_result_is_persisted_as_agent_phase_artifact(
    db_session,
    sample_issue_payload,
) -> None:
    task = create_task_from_webhook(db_session, sample_issue_payload)
    localization = LocalizationResult.model_validate_json(
        LOCALIZATION_JSON
    ).model_dump(mode="json")

    _record_attempt(
        db_session,
        task,
        attempt_index=1,
        result_status=TaskResultStatus.failed,
        diff_text="",
        localization=localization,
    )
    db_session.commit()
    db_session.refresh(task)

    phase_artifact = next(
        artifact
        for artifact in task.artifacts
        if artifact.artifact_type == TaskArtifactType.agent_phase
    )
    assert phase_artifact.content["phase"] == "localization"
    assert phase_artifact.content["result"]["confidence"] == 0.92
