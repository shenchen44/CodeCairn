from dataclasses import dataclass, field
import json
import logging
import time

from pydantic import BaseModel, ValidationError

from app.services.agent_runtime import (
    AgentSession,
    CodingTask,
    normalize_task,
)
from app.core.config import get_settings
from app.services.openai.agent_loop import (
    AgentLoop,
    AgentResponseParseError,
    AgentRunResult,
    _extract_usage,
    extract_json_object,
)
from app.services.openai.client import OpenAIChatClient
from app.services.openai.contracts import (
    ExecutionMode,
    LocalizationResult,
    PatchPlan,
    PatchRecoveryResult,
    ReviewResult,
    SupervisorDecision,
    evaluate_localization_gate,
    evaluate_plan_gate,
    evaluate_review_gate,
)
from app.services.openai.tools import AgentToolbox
from app.services.openai.policy import RuntimePolicy, get_runtime_policy
from app.services.openai.tool_calls import (
    append_tool_exchange,
    normalized_tool_calls,
)
from app.services.orchestration import (
    PatchTournament,
    RuntimeEventRecorder,
    build_evidence_ledger,
    compile_agent_graph,
)
from app.services.orchestration.contracts import AgentGraph


logger = logging.getLogger(__name__)

MAX_LOCALIZATION_TURNS = 6
MAX_LOCALIZATION_FINALIZATION_TURNS = 3
READ_ONLY_TOOLS = {
    "list_files",
    "glob_file_search",
    "search_code",
    "read_file",
    "retrieve_code",
    "find_definition",
    "get_imports",
    "get_functions",
    "git_log",
    "git_blame",
    "get_task_context",
    "get_issue_context",
    "get_repo_config",
}

LOCALIZATION_PROMPT = """You are the localization stage of a software-engineering agent.

Your only job is to investigate the task and identify the smallest plausible code surface.
You have read-only tools. Gather concrete evidence from repository files; do not propose edits
until you can cite paths and explain why they are relevant.
Choose retrieval, search, symbol navigation, history, and file reads according to the task.
Treat prior context as a hint, never as proof.

Return only strict JSON with this schema:
{
  "contract_version": "1",
  "status": "ready",
  "issue_summary": "requested behavior or outcome",
  "candidate_files": ["path/to/file.py"],
  "suspected_symbols": ["function_or_class"],
  "evidence": [
    {"path": "path/to/file.py", "line": 10, "symbol": "name", "reason": "why this proves relevance"}
  ],
  "root_cause_hypothesis": "evidence-backed hypothesis",
  "behavioral_contracts": [
    "observable return value, exception, log, warning, stdout/stderr, type, or shape that must hold"
  ],
  "alternative_hypotheses": [
    {
      "hypothesis": "another plausible explanation",
      "evidence_for": ["supporting repository observation"],
      "evidence_against": ["contradicting repository observation"],
      "falsification_test": "focused check that distinguishes this hypothesis"
    }
  ],
  "confidence": 0.0,
  "missing_information": []
}

Use status=insufficient when repository evidence does not support a safe patch. Evidence paths
must also appear in candidate_files. Do not use markdown or include text outside the JSON object.
"""

PLANNING_PROMPT = """You are the planning agent in a software-engineering runtime.
Turn grounded localization evidence into a minimal, ordered implementation plan.
Do not invent files outside candidate_files. Include a focused test strategy and rollback.

Return only strict JSON:
{
  "contract_version": "1",
  "objective": "what the patch must achieve",
  "steps": [
    {"order": 1, "description": "change", "files": ["path.py"], "rationale": "why"}
  ],
  "test_strategy": ["targeted test", "full regression suite"],
  "risk_level": "low",
  "rollback_strategy": "how to revert safely"
}
"""

REVIEW_PROMPT = """You are an independent code-review agent. Inspect the task, grounded
localization, approved plan, and resulting diff. Reject behavioral gaps, unsafe scope,
missing edge cases, or changes that contradict the plan. Do not approve merely because
the diff is small. Check return values, exceptions, warnings, logging, stdout/stderr,
type preservation, and shape separately when relevant. Confirm the patch distinguishes
the selected root cause from plausible alternatives.

Return only strict JSON:
{
  "contract_version": "1",
  "verdict": "approved",
  "summary": "review conclusion",
  "findings": [
    {"severity": "high", "path": "path.py", "line": 1, "message": "problem"}
  ],
  "behavior_contracts_covered": true,
  "hypotheses_considered": ["selected and rejected behavior hypotheses"],
  "test_gaps": [],
  "confidence": 0.0
}
"""

PATCH_RECOVERY_PROMPT = """You are the mutation recovery stage of a coding agent.
The normal patch loop failed to create an applicable repository diff. You receive exact
numbered file excerpts from grounded candidate files. Select the most likely behavioral
hypothesis and return up to five coherent exact text replacements. old_text must be copied verbatim from the
excerpt but WITHOUT line-number prefixes. Keep each replacement minimal. Do not return a
unified diff, prose, markdown, or speculative test-only changes.

Return only strict JSON:
{
  "contract_version": "1",
  "selected_hypothesis": "the behavior-level root cause this edit addresses",
  "rejected_hypotheses": ["plausible alternative rejected by evidence"],
  "behavior_contracts": ["observable behavior preserved or fixed"],
  "operations": [
    {
      "path": "relative/path.py",
      "old_text": "exact current source text",
      "new_text": "replacement source text",
      "rationale": "why this exact replacement fixes the selected hypothesis"
    }
  ],
  "test_expectation": "focused behavior that must change while regressions stay green"
}
"""


class LocalizationGateError(AgentResponseParseError):
    def __init__(
        self,
        reasons: list[str],
        raw_response: str,
        localization: dict,
    ) -> None:
        self.reasons = reasons
        self.localization = localization
        super().__init__(f"localization_gate_failed: {', '.join(reasons)}", raw_response)


class PhaseGateError(AgentResponseParseError):
    def __init__(
        self,
        *,
        phase: str,
        reasons: list[str],
        raw_response: str,
        phase_result: dict,
        context: dict[str, dict | None],
    ) -> None:
        self.phase = phase
        self.reasons = reasons
        self.phase_result = phase_result
        self.context = context
        super().__init__(
            f"{phase}_gate_failed: {', '.join(reasons)}",
            raw_response,
        )


class ExecutionClosureError(AgentResponseParseError):
    def __init__(
        self,
        *,
        reason: str,
        raw_response: str,
        context: dict[str, dict | None],
        run_result: AgentRunResult,
    ) -> None:
        self.reason = reason
        self.context = context
        self.run_result = run_result
        super().__init__(f"execution_closure_failed:{reason}", raw_response)


class EvidenceGateError(AgentResponseParseError):
    def __init__(
        self,
        *,
        ledger: dict,
        graph: dict,
        events: list[dict],
        run_result: AgentRunResult,
    ) -> None:
        self.ledger = ledger
        self.graph = graph
        self.events = events
        self.run_result = run_result
        reasons = ledger.get("gate", {}).get("reasons", [])
        super().__init__(
            f"evidence_gate_failed:{','.join(reasons)}",
            run_result.raw_response,
        )


class ReadOnlyToolbox:
    """Expose the exploration surface while making mutation impossible by construction."""

    def __init__(self, toolbox: AgentToolbox) -> None:
        self._toolbox = toolbox

    def tool_schemas(self) -> list[dict]:
        return [
            schema
            for schema in self._toolbox.tool_schemas()
            if schema["function"]["name"] in READ_ONLY_TOOLS
        ]

    def dispatch(self, name: str, arguments_json: str) -> dict:
        if name not in READ_ONLY_TOOLS:
            return {
                "error": f"tool_not_allowed_in_localization:{name}",
                "recoverable": False,
            }
        return self._toolbox.dispatch(name, arguments_json)


@dataclass(slots=True)
class LocalizationRun:
    result: LocalizationResult
    raw_response: str
    model_call_count: int = 0
    tool_call_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    turn_durations_ms: list[int] = field(default_factory=list)


@dataclass(slots=True)
class StructuredPhaseRun:
    result: BaseModel
    raw_response: str
    model_call_count: int
    total_input_tokens: int
    total_output_tokens: int
    duration_ms: int


def route_task(
    issue_context: dict | CodingTask,
    retry_context: str | None = None,
    policy: RuntimePolicy | None = None,
) -> SupervisorDecision:
    policy = policy or get_runtime_policy()
    task = normalize_task(issue_context)
    text = (
        f"{task.objective}\n"
        f"{task.description}\n"
        f"{' '.join(task.requirements)}"
    ).lower()
    score = 0.0
    reasons: list[str] = []
    if not task.requires_workspace_change:
        return SupervisorDecision(
            variant=policy.name,
            mode=ExecutionMode.standard,
            complexity_score=0.0,
            reasons=[f"{task.intent.value}_task"],
            required_agents=[task.intent.value],
        )
    mode_hint = str(task.metadata.get("mode") or "")
    if mode_hint == "integration":
        score += 0.55
        reasons.append("integration_task")
    if retry_context:
        score += 0.35
        reasons.append("retry_after_failure")
    if len(text) > 1200:
        score += 0.15
        reasons.append("long_task_context")
    complexity_terms = {
        "concurrency",
        "race condition",
        "migration",
        "security",
        "breaking change",
        "multiple modules",
        "cross-module",
        "integration",
    }
    matched_terms = sorted(term for term in complexity_terms if term in text)
    if matched_terms:
        score += min(0.3, len(matched_terms) * 0.1)
        reasons.append(f"complexity_terms:{','.join(matched_terms)}")
    memory_context = task.prior_context
    if any(item.get("kind") == "failure" for item in memory_context):
        score += 0.15
        reasons.append("recalled_failure_memory")

    score = min(score, 1.0)
    requested_mode = (
        ExecutionMode.deep_review
        if score >= 0.45
        else ExecutionMode.standard
    )
    mode = requested_mode
    if not policy.enable_deep_review:
        mode = ExecutionMode.standard
        if requested_mode == ExecutionMode.deep_review:
            reasons.append("deep_review_disabled_by_policy")
    agents = ["patch"]
    if policy.enable_staged_localization:
        agents.insert(0, "localization")
    if mode == ExecutionMode.deep_review:
        agents = ["localization", "planner", "patch", "reviewer"]
    if not reasons:
        reasons.append("low_complexity_task")
    return SupervisorDecision(
        variant=policy.name,
        mode=mode,
        complexity_score=score,
        reasons=reasons,
        required_agents=agents,
    )


def _run_structured_phase(
    client: OpenAIChatClient,
    *,
    system_prompt: str,
    payload: dict,
    result_type: type[BaseModel],
) -> StructuredPhaseRun:
    started = time.perf_counter()
    response = client.create_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, indent=2),
            },
        ],
        tools=[],
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    raw_response = response.choices[0].message.content or ""
    input_tokens, output_tokens = _extract_usage(response)
    model_call_count = 1
    try:
        parsed = extract_json_object(raw_response)
        result = result_type.model_validate(parsed)
    except (AgentResponseParseError, ValidationError) as first_exc:
        repair_started = time.perf_counter()
        repair_response = client.create_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Repair a structured agent hand-off. Return only one "
                        "strict JSON object matching the supplied JSON Schema. "
                        "Do not call tools or include markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "json_schema": result_type.model_json_schema(),
                            "invalid_response": raw_response[:12000],
                            "validation_error": str(first_exc)[:2000],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            tools=[],
        )
        duration_ms += int(
            (time.perf_counter() - repair_started) * 1000
        )
        model_call_count += 1
        repair_input, repair_output = _extract_usage(repair_response)
        input_tokens += repair_input
        output_tokens += repair_output
        raw_response = repair_response.choices[0].message.content or ""
        try:
            parsed = extract_json_object(raw_response)
            result = result_type.model_validate(parsed)
        except (AgentResponseParseError, ValidationError) as exc:
            raise AgentResponseParseError(
                f"invalid_{result_type.__name__}: {str(exc)[:500]}",
                raw_response,
            ) from exc
    return StructuredPhaseRun(
        result=result,
        raw_response=raw_response,
        model_call_count=model_call_count,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        duration_ms=duration_ms,
    )


class LocalizationLoop:
    def __init__(self, client: OpenAIChatClient) -> None:
        self.client = client

    def run(
        self,
        toolbox: AgentToolbox,
        retry_context: str | None = None,
        session: AgentSession | None = None,
        minimum_confidence: float = 0.55,
    ) -> LocalizationRun:
        readonly = ReadOnlyToolbox(toolbox)
        context_getter = getattr(
            toolbox,
            "get_task_context",
            toolbox.get_issue_context,
        )
        task_context = normalize_task(context_getter()).prompt_payload()
        messages: list[dict] = [
            {"role": "system", "content": LOCALIZATION_PROMPT},
            {
                "role": "user",
                "content": (
                    "Localize this task using repository evidence.\n\n"
                    f"task_context:\n{json.dumps(task_context, ensure_ascii=False, indent=2)}"
                ),
            },
        ]
        if retry_context:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "A previous execution attempt failed. Use this only as additional localization evidence:\n"
                        f"{retry_context}"
                    ),
                }
            )

        model_calls = 0
        tool_calls_count = 0
        input_tokens = 0
        output_tokens = 0
        durations: list[int] = []
        raw_response = ""

        for _ in range(MAX_LOCALIZATION_TURNS):
            started = time.perf_counter()
            response = self.client.create_completion(
                messages=messages,
                tools=readonly.tool_schemas(),
            )
            durations.append(int((time.perf_counter() - started) * 1000))
            model_calls += 1
            turn_input, turn_output = _extract_usage(response)
            input_tokens += turn_input
            output_tokens += turn_output

            response_message = response.choices[0].message
            raw_response = response_message.content or ""
            tool_calls = normalized_tool_calls(response_message)
            tool_calls_count += len(tool_calls)
            if not tool_calls:
                break

            append_tool_exchange(
                messages,
                response_content=response_message.content or "",
                calls=tool_calls,
                dispatch=readonly.dispatch,
            )
        else:
            for _ in range(MAX_LOCALIZATION_FINALIZATION_TURNS):
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Stop exploring and return the localization JSON now. "
                            "Only request another read-only tool when the JSON "
                            "would otherwise be ungrounded."
                        ),
                    }
                )
                started = time.perf_counter()
                response = self.client.create_completion(
                    messages=messages,
                    tools=[],
                )
                durations.append(
                    int((time.perf_counter() - started) * 1000)
                )
                model_calls += 1
                turn_input, turn_output = _extract_usage(response)
                input_tokens += turn_input
                output_tokens += turn_output
                response_message = response.choices[0].message
                raw_response = response_message.content or ""
                tool_calls = normalized_tool_calls(response_message)
                tool_calls_count += len(tool_calls)
                if not tool_calls:
                    break
                append_tool_exchange(
                    messages,
                    response_content=raw_response,
                    calls=tool_calls,
                    dispatch=readonly.dispatch,
                )
            if normalized_tool_calls(response.choices[0].message):
                evidence_transcript = "\n".join(
                    message.get("content", "")
                    for message in messages
                    if message.get("role") in {"tool", "user"}
                    and (
                        message.get("role") == "tool"
                        or "Results for the requested" in message.get(
                            "content", ""
                        )
                    )
                )
                compression_messages = [
                    {
                        "role": "system",
                        "content": (
                            f"{LOCALIZATION_PROMPT}\n"
                            "No tools are available in this compression step. "
                            "Synthesize the collected repository evidence into "
                            "the required JSON now."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "task_context:\n"
                            f"{json.dumps(task_context, ensure_ascii=False)}\n\n"
                            "collected_repository_evidence:\n"
                            f"{evidence_transcript[-30000:]}"
                        ),
                    },
                ]
                started = time.perf_counter()
                response = self.client.create_completion(
                    messages=compression_messages,
                    tools=[],
                )
                durations.append(
                    int((time.perf_counter() - started) * 1000)
                )
                model_calls += 1
                turn_input, turn_output = _extract_usage(response)
                input_tokens += turn_input
                output_tokens += turn_output
                raw_response = (
                    response.choices[0].message.content or ""
                )

        try:
            payload = extract_json_object(raw_response)
            result = LocalizationResult.model_validate(payload)
        except (AgentResponseParseError, ValidationError) as first_exc:
            repair_started = time.perf_counter()
            repair_response = self.client.create_completion(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Repair a localization hand-off. Return only one "
                            "strict JSON object matching the supplied JSON "
                            "Schema. Preserve repository evidence, use null "
                            "for unknown line numbers, and include no markdown."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "json_schema": (
                                    LocalizationResult.model_json_schema()
                                ),
                                "invalid_response": raw_response[:16000],
                                "validation_error": str(first_exc)[:2000],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                tools=[],
            )
            durations.append(
                int((time.perf_counter() - repair_started) * 1000)
            )
            model_calls += 1
            repair_input, repair_output = _extract_usage(repair_response)
            input_tokens += repair_input
            output_tokens += repair_output
            raw_response = repair_response.choices[0].message.content or ""
            try:
                payload = extract_json_object(raw_response)
                result = LocalizationResult.model_validate(payload)
            except (AgentResponseParseError, ValidationError) as exc:
                error = AgentResponseParseError(
                    f"invalid_localization_result: {str(exc)[:500]}",
                    raw_response,
                )
                error.model_call_count = model_calls
                error.tool_call_count = tool_calls_count
                error.total_input_tokens = input_tokens
                error.total_output_tokens = output_tokens
                raise error from exc

        gate = evaluate_localization_gate(
            result,
            minimum_confidence=minimum_confidence,
        )
        if not gate.passed:
            error = LocalizationGateError(
                gate.reasons,
                raw_response,
                result.model_dump(mode="json"),
            )
            error.model_call_count = model_calls
            error.tool_call_count = tool_calls_count
            error.total_input_tokens = input_tokens
            error.total_output_tokens = output_tokens
            raise error

        return LocalizationRun(
            result=result,
            raw_response=raw_response,
            model_call_count=model_calls,
            tool_call_count=tool_calls_count,
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            turn_durations_ms=durations,
        )


class StagedAgentRuntime:
    """Supervisor for staged execution; later phases can become independent agents."""

    def __init__(
        self,
        client: OpenAIChatClient | None = None,
        policy: RuntimePolicy | None = None,
    ) -> None:
        self.client = client or OpenAIChatClient()
        if policy is None:
            policy = get_runtime_policy(
                get_settings().agent_runtime_variant
            )
        self.policy = policy
        self.localization_loop = LocalizationLoop(self.client)
        self.patch_loop = AgentLoop(self.client)

    def run(
        self,
        toolbox: AgentToolbox,
        retry_context: str | None = None,
        session: AgentSession | None = None,
    ) -> AgentRunResult:
        task = getattr(toolbox, "task", None)
        if task is None:
            task = normalize_task(toolbox.get_issue_context())
        issue_context = task.prompt_payload()
        events = RuntimeEventRecorder()
        baseline_diff = ""
        if task.requires_workspace_change:
            baseline_diff = str(
                toolbox.dispatch("git_diff", "{}").get("diff", "")
            ).strip()
        route = route_task(issue_context, retry_context, self.policy)
        tournament_enabled = (
            self.policy.enable_patch_tournament
            and route.mode == ExecutionMode.deep_review
            and isinstance(toolbox, AgentToolbox)
            and not baseline_diff
        )
        graph = compile_agent_graph(
            task,
            route,
            self.policy,
            enable_tournament=tournament_enabled,
        )
        events.emit(
            "runtime_start",
            payload={
                "graph_id": graph.graph_id,
                "strategy": graph.strategy,
                "variant": self.policy.name,
            },
        )
        if not task.requires_workspace_change:
            self._start_node(graph, events, "task")
            result = self.patch_loop.run(
                toolbox,
                retry_context=retry_context,
                session=session,
            )
            self._complete_node(graph, events, "task")
            events.emit(
                "runtime_end",
                payload={"status": "completed"},
            )
            result.route_decision = route.model_dump(mode="json")
            result.agent_graph = graph.model_dump(mode="json")
            result.runtime_events = events.dump()
            return result
        if not self.policy.enable_staged_localization:
            self._start_node(graph, events, "patch")
            result = self.patch_loop.run(
                toolbox,
                retry_context=retry_context,
                session=session,
            )
            diff_text = self._ensure_repository_diff(
                toolbox,
                result,
                issue_context=issue_context,
                route=route.model_dump(mode="json"),
                localization=None,
                plan=None,
                baseline_diff=baseline_diff,
                graph=graph,
                events=events,
            )
            self._complete_node(graph, events, "patch")
            ledger = self._build_and_gate_evidence(
                issue_context=issue_context,
                localization=None,
                result=result,
                diff_text=diff_text,
                graph=graph,
                events=events,
            )
            result.route_decision = route.model_dump(mode="json")
            self._finish_result(result, graph, ledger, events)
            return result
        self._start_node(graph, events, "localization")
        localization = self.localization_loop.run(
            toolbox,
            retry_context=retry_context,
            minimum_confidence=self.policy.localization_min_confidence,
        )
        self._complete_node(graph, events, "localization")
        localization_context = localization.result.model_dump(mode="json")
        plan_run: StructuredPhaseRun | None = None
        planning_context: dict | None = None
        if route.mode == ExecutionMode.deep_review:
            self._start_node(graph, events, "planning")
            plan_run = _run_structured_phase(
                self.client,
                system_prompt=PLANNING_PROMPT,
                payload={
                    "issue_context": issue_context,
                    "localization_result": localization_context,
                },
                result_type=PatchPlan,
            )
            plan = PatchPlan.model_validate(plan_run.result)
            plan_gate = evaluate_plan_gate(plan, localization.result)
            planning_context = plan.model_dump(mode="json")
            if not plan_gate.passed:
                raise PhaseGateError(
                    phase="planning",
                    reasons=plan_gate.reasons,
                    raw_response=plan_run.raw_response,
                    phase_result=planning_context,
                    context={
                        "route": route.model_dump(mode="json"),
                        "localization": localization_context,
                        "plan": planning_context,
                        "review": None,
                    },
                )
            self._complete_node(graph, events, "planning")
        if tournament_enabled:
            self._start_node(graph, events, "patch_minimal")
            self._start_node(graph, events, "patch_challenger")
            selection = PatchTournament(self.patch_loop).run(
                toolbox,
                localization_context=localization_context,
                planning_context=planning_context,
                retry_context=retry_context,
            )
            result = selection.result
            self._complete_node(graph, events, "patch_minimal")
            self._complete_node(graph, events, "patch_challenger")
            self._start_node(graph, events, "patch_selector")
            self._complete_node(graph, events, "patch_selector")
        else:
            self._start_node(graph, events, "patch")
            result = self.patch_loop.run(
                toolbox,
                retry_context=retry_context,
                localization_context=localization_context,
                planning_context=planning_context,
                session=session,
            )
        diff_text = self._ensure_repository_diff(
            toolbox,
            result,
            issue_context=issue_context,
            route=route.model_dump(mode="json"),
            localization=localization_context,
            plan=planning_context,
            baseline_diff=baseline_diff,
            graph=graph,
            events=events,
        )
        if not tournament_enabled:
            self._complete_node(graph, events, "patch")
        ledger = self._build_and_gate_evidence(
            issue_context=issue_context,
            localization=localization_context,
            result=result,
            diff_text=diff_text,
            graph=graph,
            events=events,
        )
        review_run: StructuredPhaseRun | None = None
        review_runs: list[StructuredPhaseRun] = []
        review_context: dict | None = None
        should_review = (
            route.mode == ExecutionMode.deep_review
            or (
                self.policy.enable_standard_review
                and bool(
                    localization_context.get(
                        "alternative_hypotheses"
                    )
                )
            )
        )
        if should_review:
            self._start_node(graph, events, "review")
            diff_result = toolbox.dispatch("git_diff", "{}")
            review_run = _run_structured_phase(
                self.client,
                system_prompt=REVIEW_PROMPT,
                payload={
                    "issue_context": issue_context,
                    "localization_result": localization_context,
                    "patch_plan": planning_context,
                    "diff": str(diff_result.get("diff", ""))[:12000],
                    "evidence_ledger": ledger,
                },
                result_type=ReviewResult,
            )
            review_runs.append(review_run)
            review = ReviewResult.model_validate(review_run.result)
            review_context = review.model_dump(mode="json")
            review_gate = evaluate_review_gate(review)
            if (
                not review_gate.passed
                and self.policy.enable_patch_recovery
            ):
                before_revision = str(
                    diff_result.get("diff", "")
                ).strip()
                revised_diff = self._recover_repository_diff(
                    toolbox,
                    result,
                    issue_context=issue_context,
                    localization=localization_context,
                    plan=planning_context,
                    baseline_diff=before_revision,
                    initial_error=json.dumps(
                        {
                            "review_reasons": review_gate.reasons,
                            "review_findings": review_context.get(
                                "findings", []
                            ),
                            "test_gaps": review_context.get(
                                "test_gaps", []
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    graph=graph,
                    events=events,
                )
                if revised_diff and revised_diff != before_revision:
                    ledger = self._build_and_gate_evidence(
                        issue_context=issue_context,
                        localization=localization_context,
                        result=result,
                        diff_text=revised_diff,
                        graph=graph,
                        events=events,
                    )
                    review_run = _run_structured_phase(
                        self.client,
                        system_prompt=REVIEW_PROMPT,
                        payload={
                            "issue_context": issue_context,
                            "localization_result": (
                                localization_context
                            ),
                            "patch_plan": planning_context,
                            "diff": revised_diff[:12000],
                            "evidence_ledger": ledger,
                            "previous_review": review_context,
                        },
                        result_type=ReviewResult,
                    )
                    review_runs.append(review_run)
                    review = ReviewResult.model_validate(
                        review_run.result
                    )
                    review_context = review.model_dump(mode="json")
                    review_gate = evaluate_review_gate(review)
            if not review_gate.passed:
                raise PhaseGateError(
                    phase="review",
                    reasons=review_gate.reasons,
                    raw_response=review_run.raw_response,
                    phase_result=review_context,
                    context={
                        "route": route.model_dump(mode="json"),
                        "localization": localization_context,
                        "plan": planning_context,
                        "review": review_context,
                    },
                )
            self._complete_node(graph, events, "review")
        else:
            self._skip_node(graph, "review")
        self._skip_node(graph, "patch_recovery")
        result.localization = localization_context
        result.route_decision = route.model_dump(mode="json")
        result.plan = planning_context
        result.review = review_context
        result.model_call_count += localization.model_call_count
        result.tool_call_count += localization.tool_call_count
        result.total_input_tokens += localization.total_input_tokens
        result.total_output_tokens += localization.total_output_tokens
        patch_durations = result.turn_durations_ms or []
        result.turn_durations_ms = list(localization.turn_durations_ms)
        if plan_run is not None:
            result.turn_durations_ms.append(plan_run.duration_ms)
        result.turn_durations_ms.extend(patch_durations)
        for item in review_runs:
            result.turn_durations_ms.append(item.duration_ms)
        for phase_run in [plan_run, *review_runs]:
            if phase_run is None:
                continue
            result.model_call_count += phase_run.model_call_count
            result.total_input_tokens += phase_run.total_input_tokens
            result.total_output_tokens += phase_run.total_output_tokens
        result.tool_call_count += len(review_runs)
        self._finish_result(result, graph, ledger, events)
        return result

    def _build_and_gate_evidence(
        self,
        *,
        issue_context: dict,
        localization: dict | None,
        result: AgentRunResult,
        diff_text: str,
        graph: AgentGraph,
        events: RuntimeEventRecorder,
    ) -> dict:
        ledger = build_evidence_ledger(
            issue_context=issue_context,
            localization=localization,
            summary=result.summary,
            diff_text=diff_text,
            require_grounded_evidence=(
                self.policy.enable_staged_localization
            ),
        ).model_dump(mode="json")
        gate = ledger["gate"]
        events.emit(
            "gate_passed" if gate["passed"] else "gate_failed",
            phase="evidence",
            payload=gate,
        )
        if not gate["passed"]:
            raise EvidenceGateError(
                ledger=ledger,
                graph=graph.model_dump(mode="json"),
                events=events.dump(),
                run_result=result,
            )
        return ledger

    @staticmethod
    def _start_node(
        graph: AgentGraph,
        events: RuntimeEventRecorder,
        node_id: str,
    ) -> None:
        node = next(item for item in graph.nodes if item.id == node_id)
        node.status = "running"
        events.emit(
            "phase_start",
            phase=node_id,
            payload={"agent": node.agent},
        )

    @staticmethod
    def _complete_node(
        graph: AgentGraph,
        events: RuntimeEventRecorder,
        node_id: str,
    ) -> None:
        node = next(item for item in graph.nodes if item.id == node_id)
        node.status = "completed"
        events.emit(
            "phase_end",
            phase=node_id,
            payload={"agent": node.agent, "status": "completed"},
        )

    @staticmethod
    def _finish_result(
        result: AgentRunResult,
        graph: AgentGraph,
        ledger: dict,
        events: RuntimeEventRecorder,
    ) -> None:
        events.emit(
            "runtime_end",
            payload={"status": "completed"},
        )
        result.agent_graph = graph.model_dump(mode="json")
        result.evidence_ledger = ledger
        result.runtime_events = events.dump()

    def _ensure_repository_diff(
        self,
        toolbox: AgentToolbox,
        result: AgentRunResult,
        *,
        issue_context: dict,
        route: dict,
        localization: dict | None,
        plan: dict | None,
        baseline_diff: str,
        graph: AgentGraph,
        events: RuntimeEventRecorder,
    ) -> str:
        diff_text = str(toolbox.dispatch("git_diff", "{}").get("diff", "")).strip()
        apply_error = ""
        if diff_text == baseline_diff and result.patch_text.strip():
            try:
                toolbox.dispatch(
                    "apply_patch",
                    json.dumps({"unified_diff": result.patch_text}),
                )
            except Exception as exc:
                apply_error = str(exc)
            diff_text = str(toolbox.dispatch("git_diff", "{}").get("diff", "")).strip()
        if (
            (not diff_text or diff_text == baseline_diff)
            and self.policy.enable_patch_recovery
        ):
            diff_text = self._recover_repository_diff(
                toolbox,
                result,
                issue_context=issue_context,
                localization=localization,
                plan=plan,
                baseline_diff=baseline_diff,
                initial_error=apply_error or "no_repository_diff",
                graph=graph,
                events=events,
            )
        if not diff_text or diff_text == baseline_diff:
            raise ExecutionClosureError(
                reason=(
                    "no_repository_diff"
                    if not diff_text
                    else "no_new_repository_diff"
                ),
                raw_response=result.raw_response,
                context={
                    "route": route,
                    "localization": localization,
                    "plan": plan,
                    "review": None,
                },
                run_result=result,
            )
        return diff_text

    def _recover_repository_diff(
        self,
        toolbox: AgentToolbox,
        result: AgentRunResult,
        *,
        issue_context: dict,
        localization: dict | None,
        plan: dict | None,
        baseline_diff: str,
        initial_error: str,
        graph: AgentGraph,
        events: RuntimeEventRecorder,
    ) -> str:
        self._start_node(graph, events, "patch_recovery")
        candidates = list(
            dict.fromkeys((localization or {}).get("candidate_files", []))
        )[:5]
        allowed_paths = set(candidates)
        evidence_lines = {
            str(item.get("path")): item.get("line")
            for item in (localization or {}).get("evidence", [])
            if isinstance(item, dict) and item.get("path")
        }
        excerpts: list[dict] = []
        for path in candidates:
            line = evidence_lines.get(path)
            arguments = {"path": path}
            if isinstance(line, int):
                arguments.update(
                    {
                        "start_line": max(1, line - 60),
                        "end_line": line + 100,
                    }
                )
            else:
                arguments.update({"start_line": 1, "end_line": 240})
            read_result = toolbox.dispatch(
                "read_file",
                json.dumps(arguments),
            )
            excerpts.append(
                {
                    "path": path,
                    "content": str(read_result.get("content", ""))[
                        :12000
                    ],
                    "error": read_result.get("error"),
                }
            )

        recovery_records: list[dict] = []
        previous_error = initial_error
        for attempt in range(
            1,
            max(self.policy.patch_recovery_attempts, 1) + 1,
        ):
            try:
                phase_run = _run_structured_phase(
                    self.client,
                    system_prompt=PATCH_RECOVERY_PROMPT,
                    payload={
                        "issue_context": issue_context,
                        "localization_result": localization,
                        "patch_plan": plan,
                        "exact_file_excerpts": excerpts,
                        "previous_patch_error": previous_error[:2000],
                        "previous_final_summary": result.summary,
                    },
                    result_type=PatchRecoveryResult,
                )
                recovery = PatchRecoveryResult.model_validate(
                    phase_run.result
                )
                result.model_call_count += phase_run.model_call_count
                result.total_input_tokens += phase_run.total_input_tokens
                result.total_output_tokens += phase_run.total_output_tokens
                if result.turn_durations_ms is None:
                    result.turn_durations_ms = []
                result.turn_durations_ms.append(phase_run.duration_ms)
                for operation in recovery.operations:
                    if (
                        allowed_paths
                        and operation.path not in allowed_paths
                    ):
                        raise ValueError(
                            "recovery_path_not_localized:"
                            f"{operation.path}"
                        )
                operation_payloads = [
                    operation.model_dump(
                        mode="json",
                        exclude={"rationale"},
                    )
                    for operation in recovery.operations
                ]
                if len(operation_payloads) == 1:
                    toolbox.dispatch(
                        "replace_in_file",
                        json.dumps(
                            operation_payloads[0],
                            ensure_ascii=False,
                        ),
                    )
                else:
                    toolbox.dispatch(
                        "replace_in_files",
                        json.dumps(
                            {"operations": operation_payloads},
                            ensure_ascii=False,
                        ),
                    )
                result.tool_call_count += 1
                diff_text = str(
                    toolbox.dispatch("git_diff", "{}").get("diff", "")
                ).strip()
                if not diff_text or diff_text == baseline_diff:
                    raise ValueError("recovery_produced_no_new_diff")
                recovery_record = {
                    "triggered": True,
                    "status": "recovered",
                    "attempt": attempt,
                    "selected_hypothesis": (
                        recovery.selected_hypothesis
                    ),
                    "rejected_hypotheses": (
                        recovery.rejected_hypotheses
                    ),
                    "behavior_contracts": recovery.behavior_contracts,
                    "operations": [
                        operation.model_dump(mode="json")
                        for operation in recovery.operations
                    ],
                    "operation": recovery.operations[0].model_dump(
                        mode="json"
                    ),
                    "test_expectation": recovery.test_expectation,
                    "attempts": recovery_records,
                }
                result.recovery = recovery_record
                result.patch_text = ""
                self._complete_node(graph, events, "patch_recovery")
                return diff_text
            except Exception as exc:
                previous_error = str(exc)
                recovery_records.append(
                    {
                        "attempt": attempt,
                        "status": "failed",
                        "error": previous_error[:1000],
                    }
                )

        result.recovery = {
            "triggered": True,
            "status": "failed",
            "attempts": recovery_records,
        }
        self._fail_node(
            graph,
            events,
            "patch_recovery",
            previous_error,
        )
        return str(
            toolbox.dispatch("git_diff", "{}").get("diff", "")
        ).strip()

    @staticmethod
    def _skip_node(graph: AgentGraph, node_id: str) -> None:
        node = next(
            (item for item in graph.nodes if item.id == node_id),
            None,
        )
        if node is not None and node.status == "planned":
            node.status = "skipped"

    @staticmethod
    def _fail_node(
        graph: AgentGraph,
        events: RuntimeEventRecorder,
        node_id: str,
        error: str,
    ) -> None:
        node = next(item for item in graph.nodes if item.id == node_id)
        node.status = "failed"
        events.emit(
            "phase_end",
            phase=node_id,
            payload={
                "agent": node.agent,
                "status": "failed",
                "error": error[:1000],
            },
        )
