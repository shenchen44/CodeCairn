from dataclasses import dataclass
import json
import logging
import time

from app.services.agent_runtime import (
    AgentSession,
    ExtensionEvent,
    ExtensionManager,
    normalize_task,
)
from app.services.openai.client import OpenAIChatClient
from app.services.openai.prompts import SYSTEM_PROMPT
from app.services.openai.tool_calls import (
    append_tool_exchange,
    normalized_tool_calls,
)
from app.services.openai.tools import AgentToolbox, ToolExecutionError

logger = logging.getLogger(__name__)

# --- Safety limits ---
MAX_AGENT_TURNS = 15
MAX_TOTAL_INPUT_TOKENS = 120_000
FINAL_ANSWER_MAX_TOKENS = 4096


@dataclass(slots=True)
class AgentRunResult:
    summary: dict
    patch_text: str
    pr_title: str
    pr_body_summary: dict
    delivery: dict | None = None
    raw_response: str = ""
    model_call_count: int = 0
    tool_call_count: int = 0
    # --- new observability fields ---
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    turn_durations_ms: list[int] | None = None
    truncated_by_limit: bool = False
    localization: dict | None = None
    route_decision: dict | None = None
    plan: dict | None = None
    review: dict | None = None
    agent_graph: dict | None = None
    evidence_ledger: dict | None = None
    runtime_events: list[dict] | None = None
    tournament: dict | None = None
    recovery: dict | None = None
    mutation_pressure_applied: bool = False
    session_id: str | None = None


class AgentResponseParseError(ValueError):
    def __init__(self, message: str, raw_response: str) -> None:
        super().__init__(message)
        self.raw_response = raw_response


class AgentTurnLimitError(AgentResponseParseError):
    """Raised when the agent loop hits the turn limit without producing valid JSON."""
    pass


def _extract_fenced_json(text: str) -> str | None:
    marker = "```"
    start = text.find(marker)
    while start != -1:
        end = text.find(marker, start + len(marker))
        if end == -1:
            break
        block = text[start + len(marker):end].strip()
        if block.startswith("json"):
            block = block[4:].lstrip()
        if block.startswith("{") and block.endswith("}"):
            return block
        start = text.find(marker, end + len(marker))
    return None


def _find_json_object_span(text: str) -> tuple[int, int] | None:
    in_string = False
    escape = False
    depth = 0
    start = -1
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start != -1:
                return start, index + 1
    return None


def extract_json_object(text: str) -> dict:
    cleaned = text.strip()
    if not cleaned:
        raise AgentResponseParseError("empty_model_response", text)

    candidates = [cleaned]
    fenced = _extract_fenced_json(cleaned)
    if fenced is not None:
        candidates.insert(0, fenced)

    span = _find_json_object_span(cleaned)
    if span is not None:
        candidates.append(cleaned[span[0]:span[1]])

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise AgentResponseParseError(f"invalid_model_json: {cleaned[:500]}", text)


def _extract_usage(response) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from an OpenAI response."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0


class AgentLoop:
    """OpenAI-compatible chat-completions loop with explicit tool dispatch.

    Safety features:
    - MAX_AGENT_TURNS hard limit prevents infinite loops
    - MAX_TOTAL_INPUT_TOKENS budget prevents runaway context growth
    - Forced final-answer turn when limits are hit
    - Per-turn timing for observability
    """

    def __init__(
        self,
        client: OpenAIChatClient | None = None,
        *,
        extensions: ExtensionManager | None = None,
    ) -> None:
        self.client = client or OpenAIChatClient()
        self.extensions = extensions or ExtensionManager()

    @staticmethod
    def _dispatch_with_recovery(
        toolbox: AgentToolbox,
        name: str,
        arguments: str,
    ) -> dict:
        try:
            return toolbox.dispatch(name, arguments)
        except ToolExecutionError as exc:
            return {
                "error": str(exc),
                "recoverable": True,
                "tool": exc.tool_name,
                "arguments": exc.arguments,
                "current_diff": exc.diff_text[-6000:],
                "guidance": (
                    "The edit was rejected and rolled back. Read the exact "
                    "target excerpt and retry with replace_in_file, or repair "
                    "the unified diff before calling apply_patch again."
                ),
            }

    def run(
        self,
        toolbox: AgentToolbox,
        retry_context: str | None = None,
        localization_context: dict | None = None,
        planning_context: dict | None = None,
        session: AgentSession | None = None,
    ) -> AgentRunResult:
        model_call_count = 0
        tool_call_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        turn_durations: list[int] = []
        truncated_by_limit = False
        mutation_pressure_applied = False
        task = getattr(toolbox, "task", None)
        if task is None:
            context_getter = getattr(
                toolbox,
                "get_task_context",
                toolbox.get_issue_context,
            )
            task = normalize_task(context_getter())
        policy = getattr(toolbox, "runtime_policy", None)
        adaptive_mutation = policy is not None
        mutation_deadline_turn = getattr(
            policy,
            "mutation_deadline_turn",
            None,
        )
        baseline_diff = ""
        if task.requires_workspace_change and adaptive_mutation:
            try:
                baseline_diff = str(
                    toolbox.dispatch("git_diff", "{}").get("diff", "")
                ).strip()
            except Exception:
                baseline_diff = ""
        stall_turn_limit = max(
            int(getattr(policy, "stall_turn_limit", 3)),
            1,
        )
        mutation_reserve_turns = max(
            int(getattr(policy, "mutation_reserve_turns", 3)),
            1,
        )
        last_progress_turn = 0
        seen_tool_calls: set[str] = set()
        current_turn = 0

        def dispatch_tool(name: str, arguments: str) -> dict:
            nonlocal last_progress_turn
            before = self.extensions.emit(
                ExtensionEvent(
                    name="tool_call",
                    task=task,
                    turn=current_turn,
                    payload={
                        "name": name,
                        "arguments": arguments,
                    },
                )
            )
            if before.block:
                return {
                    "error": f"tool_blocked_by_extension:{name}",
                    "reason": before.reason,
                    "recoverable": False,
                }
            payload = before.payload or {}
            name = str(payload.get("name") or name)
            arguments = str(
                payload.get("arguments") or arguments
            )
            result = self._dispatch_with_recovery(
                toolbox,
                name,
                arguments,
            )
            signature = f"{name}:{arguments}"
            successful = not result.get("error")
            if successful and signature not in seen_tool_calls:
                seen_tool_calls.add(signature)
                last_progress_turn = current_turn
            self.extensions.emit(
                ExtensionEvent(
                    name="tool_result",
                    task=task,
                    turn=current_turn,
                    payload={
                        "name": name,
                        "arguments": arguments,
                        "result": result,
                    },
                )
            )
            return result

        session_messages = (
            session.message_context() if session is not None else []
        )
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *session_messages,
            {
                "role": "user",
                "content": (
                    "Work on this repository task using the active tools. "
                    "Match your workflow to its intent and return the required "
                    "source-independent JSON result.\n\n"
                    f"task_context:\n"
                    f"{json.dumps(task.prompt_payload(), ensure_ascii=False, indent=2)}"
                ),
            },
        ]
        transcript_start = len(messages) - 1

        # On retry: inject reflection context so the agent learns from failure
        if retry_context:
            messages.append({"role": "user", "content": retry_context})
        if localization_context:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "A read-only localization phase passed its evidence gate. "
                        "Treat this structured hand-off as the starting point for your patch plan. "
                        "Verify it against the repository before editing if later evidence conflicts.\n\n"
                        f"localization_result:\n"
                        f"{json.dumps(localization_context, ensure_ascii=False, indent=2)}"
                    ),
                }
            )
        if planning_context:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "A planning agent produced the following approved execution plan. "
                        "Follow it unless repository evidence proves a step unsafe or incorrect.\n\n"
                        f"patch_plan:\n"
                        f"{json.dumps(planning_context, ensure_ascii=False, indent=2)}"
                    ),
                }
            )

        last_response_content = ""

        for turn_index in range(MAX_AGENT_TURNS):
            current_turn = turn_index + 1
            turn_start = time.perf_counter()

            tool_schemas = toolbox.tool_schemas()
            deadline_reached = (
                mutation_deadline_turn is not None
                and current_turn >= mutation_deadline_turn
            )
            stalled = (
                current_turn - last_progress_turn > stall_turn_limit
            )
            reserve_reached = (
                current_turn
                > MAX_AGENT_TURNS - mutation_reserve_turns
            )
            if task.requires_workspace_change and adaptive_mutation and (
                deadline_reached or stalled or reserve_reached
            ):
                try:
                    current_diff = str(
                        toolbox.dispatch("git_diff", "{}").get(
                            "diff", ""
                        )
                    ).strip()
                except Exception:
                    current_diff = baseline_diff
                if current_diff == baseline_diff:
                    if not mutation_pressure_applied:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "The exploration budget is exhausted. "
                                    "Use the repository evidence already gathered "
                                    "to make a concrete, minimal workspace change. "
                                    "If evidence is still insufficient, finish as "
                                    "blocked and explain what is missing instead "
                                    "of guessing."
                                ),
                            }
                        )
                        mutation_pressure_applied = True
                    allowed = {
                        "read_file",
                        "replace_in_file",
                        "replace_in_files",
                        "write_file",
                        "apply_patch",
                        "git_diff",
                        "run_test_file",
                        "run_tests",
                        "run_command",
                    }
                    tool_schemas = [
                        schema
                        for schema in tool_schemas
                        if schema["function"]["name"] in allowed
                    ]

            before_model = self.extensions.emit(
                ExtensionEvent(
                    name="before_model",
                    task=task,
                    turn=current_turn,
                    payload={
                        "messages": messages,
                        "tools": tool_schemas,
                    },
                )
            )
            if before_model.block:
                raise AgentResponseParseError(
                    "model_call_blocked_by_extension:"
                    f"{before_model.reason}",
                    "",
                )
            model_payload = before_model.payload or {}
            messages = list(
                model_payload.get("messages") or messages
            )
            tool_schemas = list(
                model_payload.get("tools") or tool_schemas
            )
            response = self.client.create_completion(
                messages=messages,
                tools=tool_schemas,
            )
            model_call_count += 1

            turn_ms = int((time.perf_counter() - turn_start) * 1000)
            turn_durations.append(turn_ms)

            # Track token usage
            inp, out = _extract_usage(response)
            total_input_tokens += inp
            total_output_tokens += out

            response_message = response.choices[0].message
            last_response_content = response_message.content or ""
            tool_calls = normalized_tool_calls(response_message)
            tool_call_count += len(tool_calls)

            logger.info(
                "agent_turn",
                extra={
                    "turn": turn_index + 1,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "tool_calls": len(tool_calls),
                    "duration_ms": turn_ms,
                },
            )
            self.extensions.emit(
                ExtensionEvent(
                    name="model_response",
                    task=task,
                    turn=current_turn,
                    payload={
                        "content": last_response_content,
                        "tool_call_count": len(tool_calls),
                        "input_tokens": inp,
                        "output_tokens": out,
                    },
                )
            )

            # No tool calls => model wants to give final answer
            if not tool_calls:
                break

            # Budget check: if we're running low on tokens, force final answer next turn
            if total_input_tokens >= MAX_TOTAL_INPUT_TOKENS:
                logger.warning(
                    "agent_token_budget_exhausted",
                    extra={"total_input_tokens": total_input_tokens, "limit": MAX_TOTAL_INPUT_TOKENS},
                )
                truncated_by_limit = True
                append_tool_exchange(
                    messages,
                    response_content=response_message.content or "",
                    calls=tool_calls,
                    dispatch=dispatch_tool,
                )
                # Force final answer — strip tools
                messages.append({
                    "role": "user",
                    "content": (
                        "You have reached the token budget limit. "
                        "Stop using tools immediately and return your final JSON answer now. "
                        "The JSON must have keys summary, patch_text, and delivery. "
                        "GitHub compatibility fields are optional."
                    ),
                })
                response = self.client.create_completion(messages=messages, tools=[])
                model_call_count += 1
                inp2, out2 = _extract_usage(response)
                total_input_tokens += inp2
                total_output_tokens += out2
                last_response_content = response.choices[0].message.content or ""
                break

            # Normal tool dispatch
            append_tool_exchange(
                messages,
                response_content=response_message.content or "",
                calls=tool_calls,
                dispatch=dispatch_tool,
            )
        else:
            # Exhausted all turns without a break — force one final answer
            logger.warning("agent_turn_limit_reached", extra={"max_turns": MAX_AGENT_TURNS})
            truncated_by_limit = True
            messages.append({
                "role": "user",
                "content": (
                    f"You have used all {MAX_AGENT_TURNS} allowed turns. "
                    "Stop using tools immediately and return your final JSON answer now. "
                    "The JSON must have keys summary, patch_text, and delivery."
                ),
            })
            response = self.client.create_completion(messages=messages, tools=[])
            model_call_count += 1
            inp, out = _extract_usage(response)
            total_input_tokens += inp
            total_output_tokens += out
            last_response_content = response.choices[0].message.content or ""

        payload = last_response_content
        try:
            parsed = extract_json_object(payload)
        except AgentResponseParseError as first_error:
            logger.warning("agent_final_json_parse_failed", extra={"error": str(first_error)[:500]})
            repair_messages = [
                {
                    "role": "system",
                    "content": (
                        "You are formatting the final answer for a coding agent. "
                        "Return only a strict JSON object. Do not call tools. Do not use markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "The previous final answer was not valid JSON. Convert it into this exact schema: "
                        '{"summary": {"status": "completed|partial|blocked", '
                        '"findings": [], "changes": [], "verification": [], '
                        '"remaining_risks": []}, "patch_text": "", '
                        '"delivery": {"title": "", "description": ""}, '
                        '"pr_title": "", "pr_body_summary": {}}.\n\n'
                        f"Previous answer:\n{payload[:4000]}"
                    ),
                },
            ]
            try:
                repair_response = self.client.create_completion(messages=repair_messages, tools=[])
                model_call_count += 1
                inp, out = _extract_usage(repair_response)
                total_input_tokens += inp
                total_output_tokens += out
                payload = repair_response.choices[0].message.content or ""
                parsed = extract_json_object(payload)
            except Exception as repair_error:
                logger.warning("agent_final_json_repair_failed", extra={"error": str(repair_error)[:500]})
                parsed = {
                    "summary": {
                        "status": "failed",
                        "notes": [
                            "The model did not return valid final JSON.",
                            str(first_error)[:500],
                        ],
                    },
                    "patch_text": "",
                    "delivery": {
                        "title": "Task result unavailable",
                        "description": (
                            "No final JSON could be parsed from the model response."
                        ),
                    },
                    "pr_title": "chore: report coding task result",
                    "pr_body_summary": {
                        "summary": "No final JSON could be parsed from the model response.",
                        "tests": "Not run.",
                    },
                }
        result = AgentRunResult(
            summary=parsed.get("summary") or {},
            patch_text=parsed.get("patch_text") or "",
            pr_title=(
                parsed.get("pr_title")
                or (parsed.get("delivery") or {}).get("title")
                or "chore: complete coding task"
            ),
            pr_body_summary=parsed.get("pr_body_summary") or {},
            delivery=parsed.get("delivery") or {},
            raw_response=payload,
            model_call_count=model_call_count,
            tool_call_count=tool_call_count,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            turn_durations_ms=turn_durations,
            truncated_by_limit=truncated_by_limit,
            mutation_pressure_applied=mutation_pressure_applied,
            session_id=session.session_id if session is not None else None,
        )
        if session is not None:
            transcript = list(messages[transcript_start:])
            if (
                last_response_content
                and (
                    not transcript
                    or transcript[-1].get("role") != "assistant"
                    or transcript[-1].get("content")
                    != last_response_content
                )
            ):
                transcript.append(
                    {
                        "role": "assistant",
                        "content": last_response_content,
                    }
                )
            for message in transcript:
                session.append_message(message)
        self.extensions.emit(
            ExtensionEvent(
                name="run_end",
                task=task,
                turn=current_turn,
                payload={
                    "summary": result.summary,
                    "delivery": result.delivery,
                    "model_call_count": result.model_call_count,
                    "tool_call_count": result.tool_call_count,
                },
            )
        )
        return result
