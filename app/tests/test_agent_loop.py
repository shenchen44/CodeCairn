import json
from types import SimpleNamespace

import pytest

from app.services.openai.agent_loop import (
    AgentLoop,
    AgentResponseParseError,
    AgentRunResult,
    MAX_AGENT_TURNS,
    MAX_TOTAL_INPUT_TOKENS,
    extract_json_object,
)
from app.services.openai.tool_calls import extract_dsml_tool_calls
from app.services.openai.tools import ToolExecutionError


# ---------------------------------------------------------------------------
# JSON extraction tests
# ---------------------------------------------------------------------------

def test_extract_json_object_from_plain_json() -> None:
    parsed = extract_json_object('{"summary": {}, "patch_text": "", "pr_title": "x", "pr_body_summary": {}}')
    assert parsed["pr_title"] == "x"


def test_extract_json_object_from_fenced_json() -> None:
    parsed = extract_json_object('```json\n{"summary": {}, "patch_text": "", "pr_title": "x", "pr_body_summary": {}}\n```')
    assert parsed["pr_title"] == "x"


def test_extract_json_object_from_wrapped_text() -> None:
    parsed = extract_json_object('Here is the result:\n{"summary": {}, "patch_text": "", "pr_title": "x", "pr_body_summary": {}}\nThanks')
    assert parsed["pr_title"] == "x"


def test_extract_json_object_empty_response_has_clear_error() -> None:
    with pytest.raises(AgentResponseParseError, match="empty_model_response"):
        extract_json_object("")


def test_extract_json_object_invalid_response_includes_snippet() -> None:
    with pytest.raises(AgentResponseParseError, match="invalid_model_json: definitely not json"):
        extract_json_object("definitely not json")


def test_extract_deepseek_dsml_tool_call() -> None:
    content = """<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="glob_file_search">
<｜｜DSML｜｜parameter name="pattern" string="true">**/std_L060*</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>"""

    calls = extract_dsml_tool_calls(content)

    assert len(calls) == 1
    assert calls[0].function.name == "glob_file_search"
    assert json.loads(calls[0].function.arguments) == {
        "pattern": "**/std_L060*"
    }
    assert calls[0].synthetic is True


def test_extract_deepseek_dsml_restores_non_string_parameters() -> None:
    content = """<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="search_code">
<｜｜DSML｜｜parameter name="limit" string="false">20</｜｜DSML｜｜parameter>
<｜｜DSML｜｜parameter name="query" string="true">LintResult</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>"""

    calls = extract_dsml_tool_calls(content)

    assert json.loads(calls[0].function.arguments) == {
        "limit": 20,
        "query": "LintResult",
    }


def test_agent_loop_executes_dsml_tool_call() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_completion(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return _make_final_response(
                    '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="list_files">'
                    '<｜｜DSML｜｜parameter name="path" string="true">.</｜｜DSML｜｜parameter>'
                    '</｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>'
                )
            assert messages[-1]["role"] == "user"
            assert '"tool": "list_files"' in messages[-1]["content"]
            return _make_final_response(FINAL_JSON)

    result = AgentLoop(client=FakeClient()).run(FakeToolbox())

    assert result.model_call_count == 2
    assert result.tool_call_count == 1


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_usage(input_tokens: int = 100, output_tokens: int = 50):
    return SimpleNamespace(prompt_tokens=input_tokens, completion_tokens=output_tokens)


def _make_tool_call_response(tool_name: str, arguments: str = "{}", input_tokens: int = 100, output_tokens: int = 50):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(name=tool_name, arguments=arguments),
                        )
                    ],
                )
            )
        ],
        usage=_make_usage(input_tokens, output_tokens),
    )


def _make_final_response(json_str: str, input_tokens: int = 100, output_tokens: int = 50):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json_str, tool_calls=None)
            )
        ],
        usage=_make_usage(input_tokens, output_tokens),
    )


def _make_empty_tool_calls_response(input_tokens: int = 100, output_tokens: int = 50):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[])
            )
        ],
        usage=_make_usage(input_tokens, output_tokens),
    )


FINAL_JSON = '{"summary": {"root_cause": "test"}, "patch_text": "", "pr_title": "fix: test", "pr_body_summary": {"root_cause": "test", "changes": ["test"]}}'


class FakeToolbox:
    @staticmethod
    def tool_schemas():
        return [{"type": "function", "function": {"name": "list_files", "parameters": {}}}]

    @staticmethod
    def get_issue_context():
        return {"title": "test issue", "body": "test body"}

    @staticmethod
    def dispatch(name: str, arguments_json: str):
        return {"files": []}


# ---------------------------------------------------------------------------
# Basic agent loop behavior
# ---------------------------------------------------------------------------

def test_agent_loop_reports_model_and_tool_call_counts() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_completion(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return _make_tool_call_response("get_issue_context")
            return _make_final_response(FINAL_JSON)

    result = AgentLoop(client=FakeClient()).run(FakeToolbox())

    assert result.model_call_count == 2
    assert result.tool_call_count == 1


def test_agent_loop_forces_mutation_tools_after_deadline() -> None:
    class DeadlineToolbox(FakeToolbox):
        runtime_policy = SimpleNamespace(mutation_deadline_turn=3)

        @staticmethod
        def tool_schemas():
            return [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "parameters": {},
                    },
                }
                for name in ("list_files", "replace_in_file")
            ]

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_completion(self, messages, tools):
            self.calls += 1
            if self.calls <= 2:
                return _make_tool_call_response("list_files")
            names = {
                tool["function"]["name"] for tool in tools
            }
            assert names == {"replace_in_file"}
            assert "exploration budget is exhausted" in (
                messages[-1]["content"].lower()
            )
            return _make_final_response(FINAL_JSON)

    result = AgentLoop(client=FakeClient()).run(DeadlineToolbox())

    assert result.mutation_pressure_applied is True
    assert result.model_call_count == 3
    assert result.tool_call_count == 2


def test_agent_loop_recovers_from_rejected_mutation_tool() -> None:
    class MutationToolbox(FakeToolbox):
        calls = 0

        @staticmethod
        def tool_schemas():
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "parameters": {},
                    },
                }
            ]

        @classmethod
        def dispatch(cls, name: str, arguments_json: str):
            cls.calls += 1
            if cls.calls == 1:
                raise ToolExecutionError(
                    "apply_patch",
                    {"unified_diff": "bad"},
                    "git_apply_failed: corrupt patch",
                    "",
                )
            return {"diff": "fixed"}

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_completion(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return _make_tool_call_response(
                    "apply_patch",
                    '{"unified_diff":"bad"}',
                )
            assert "recoverable" in messages[-1]["content"]
            return _make_final_response(FINAL_JSON)

    result = AgentLoop(client=FakeClient()).run(MutationToolbox())

    assert result.model_call_count == 2
    assert result.tool_call_count == 1


def test_agent_loop_direct_answer_no_tools() -> None:
    """When the model returns JSON directly without tool calls."""
    class FakeClient:
        def create_completion(self, messages, tools):
            return _make_final_response(FINAL_JSON)

    result = AgentLoop(client=FakeClient()).run(FakeToolbox())

    assert result.model_call_count == 1
    assert result.tool_call_count == 0
    assert result.pr_title == "fix: test"


# ---------------------------------------------------------------------------
# Token tracking
# ---------------------------------------------------------------------------

def test_agent_loop_tracks_token_usage() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_completion(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return _make_tool_call_response("list_files", input_tokens=500, output_tokens=200)
            return _make_final_response(FINAL_JSON, input_tokens=300, output_tokens=100)

    result = AgentLoop(client=FakeClient()).run(FakeToolbox())

    assert result.total_input_tokens == 800
    assert result.total_output_tokens == 300


def test_agent_loop_tracks_turn_durations() -> None:
    class FakeClient:
        def create_completion(self, messages, tools):
            return _make_final_response(FINAL_JSON)

    result = AgentLoop(client=FakeClient()).run(FakeToolbox())

    assert result.turn_durations_ms is not None
    assert len(result.turn_durations_ms) == 1
    assert result.turn_durations_ms[0] >= 0


# ---------------------------------------------------------------------------
# Turn limit
# ---------------------------------------------------------------------------

def test_agent_loop_respects_max_turns() -> None:
    """When the agent keeps requesting tool calls, it should hit the turn limit and force a final answer."""
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_completion(self, messages, tools):
            self.calls += 1
            if tools:  # Normal turns with tool calls
                return _make_tool_call_response("list_files", input_tokens=1000, output_tokens=100)
            else:  # Forced final answer turn (no tools)
                return _make_final_response(FINAL_JSON, input_tokens=200, output_tokens=50)

    result = AgentLoop(client=FakeClient()).run(FakeToolbox())

    # Should have hit the turn limit
    assert result.truncated_by_limit is True
    # Should have MAX_AGENT_TURNS tool-calling turns + 1 forced final answer turn
    assert result.model_call_count == MAX_AGENT_TURNS + 1
    assert result.tool_call_count == MAX_AGENT_TURNS


# ---------------------------------------------------------------------------
# Token budget limit
# ---------------------------------------------------------------------------

def test_agent_loop_respects_token_budget() -> None:
    """When token budget is exhausted, agent should stop using tools and force final answer."""
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_completion(self, messages, tools):
            self.calls += 1
            if self.calls <= 2:
                # First two turns use large token budgets
                return _make_tool_call_response("list_files", input_tokens=60_000, output_tokens=100)
            if tools:
                # If still called with tools (shouldn't happen after budget exceeded), return tool call
                return _make_tool_call_response("list_files", input_tokens=100, output_tokens=100)
            # Final answer turn
            return _make_final_response(FINAL_JSON, input_tokens=200, output_tokens=50)

    result = AgentLoop(client=FakeClient()).run(FakeToolbox())

    assert result.truncated_by_limit is True
    assert result.total_input_tokens >= MAX_TOTAL_INPUT_TOKENS


# ---------------------------------------------------------------------------
# Multi-tool turn
# ---------------------------------------------------------------------------

def test_agent_loop_handles_multiple_tool_calls_per_turn() -> None:
    """When the model returns multiple tool calls in a single turn."""
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_completion(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="",
                                tool_calls=[
                                    SimpleNamespace(id="call-1", function=SimpleNamespace(name="list_files", arguments='{"path": "."}')),
                                    SimpleNamespace(id="call-2", function=SimpleNamespace(name="get_issue_context", arguments="{}")),
                                ],
                            )
                        )
                    ],
                    usage=_make_usage(200, 100),
                )
            return _make_final_response(FINAL_JSON)

    result = AgentLoop(client=FakeClient()).run(FakeToolbox())

    assert result.model_call_count == 2
    assert result.tool_call_count == 2
    assert len(result.turn_durations_ms) == 2
