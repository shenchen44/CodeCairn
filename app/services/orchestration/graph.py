import hashlib
import json

from app.services.agent_runtime import CodingTask, normalize_task
from app.services.openai.contracts import ExecutionMode, SupervisorDecision
from app.services.openai.policy import RuntimePolicy
from app.services.orchestration.contracts import AgentGraph, AgentGraphNode


def _graph_id(
    task_context: dict | CodingTask,
    policy: RuntimePolicy,
) -> str:
    task = normalize_task(task_context)
    payload = json.dumps(
        {
            "task_id": task.id,
            "objective": task.objective,
            "intent": task.intent.value,
            "variant": policy.name,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def compile_agent_graph(
    issue_context: dict | CodingTask,
    route: SupervisorDecision,
    policy: RuntimePolicy,
    *,
    enable_tournament: bool | None = None,
) -> AgentGraph:
    """Compile the selected policy into an auditable execution DAG."""

    task = normalize_task(issue_context)
    if not task.requires_workspace_change:
        node = AgentGraphNode(
            id="task",
            agent=task.intent.value,
            activation_reason=f"{task.intent.value}_task",
            tool_profile="read_only",
            max_turns=15,
            token_budget=120_000,
        )
        return AgentGraph(
            graph_id=_graph_id(task, policy),
            strategy="task_only",
            risk_signals=list(route.reasons),
            nodes=[node],
            total_token_budget=node.token_budget,
        )

    nodes: list[AgentGraphNode] = []
    if policy.enable_staged_localization:
        nodes.append(
            AgentGraphNode(
                id="localization",
                agent="localizer",
                activation_reason="ground_repository_claims_before_mutation",
                tool_profile="read_only",
                max_turns=6,
                token_budget=24_000,
            )
        )

    patch_dependencies: list[str] = []
    if nodes:
        patch_dependencies.append("localization")

    if route.mode == ExecutionMode.deep_review:
        nodes.append(
            AgentGraphNode(
                id="planning",
                agent="planner",
                depends_on=["localization"],
                activation_reason="supervisor_detected_high_change_risk",
                tool_profile="read_only",
                max_turns=1,
                token_budget=8_000,
            )
        )
        patch_dependencies = ["planning"]

    tournament = (
        policy.enable_patch_tournament
        if enable_tournament is None
        else enable_tournament
    ) and route.mode == ExecutionMode.deep_review
    if tournament:
        for candidate_id, reason in (
            ("patch_minimal", "generate_minimal_candidate"),
            ("patch_challenger", "challenge_root_cause_candidate"),
        ):
            nodes.append(
                AgentGraphNode(
                    id=candidate_id,
                    agent="patcher",
                    depends_on=patch_dependencies,
                    activation_reason=reason,
                    tool_profile="workspace",
                    max_turns=15,
                    token_budget=120_000,
                )
            )
        nodes.append(
            AgentGraphNode(
                id="patch_selector",
                agent="deterministic_selector",
                depends_on=["patch_minimal", "patch_challenger"],
                activation_reason="select_best_verified_candidate",
                tool_profile="review",
                max_turns=1,
                token_budget=1,
            )
        )
    else:
        nodes.append(
            AgentGraphNode(
                id="patch",
                agent="patcher",
                depends_on=patch_dependencies,
                activation_reason="task_requires_repository_mutation",
                tool_profile="workspace",
                max_turns=15,
                token_budget=120_000,
            )
        )

    recovery_dependency = "patch_selector" if tournament else "patch"
    if policy.enable_patch_recovery:
        nodes.append(
            AgentGraphNode(
                id="patch_recovery",
                agent="exact_edit_recovery",
                depends_on=[recovery_dependency],
                activation_reason=(
                    "activate_only_when_patch_is_missing_or_invalid"
                ),
                tool_profile="workspace",
                max_turns=max(policy.patch_recovery_attempts, 1),
                token_budget=16_000,
            )
        )

    if (
        route.mode == ExecutionMode.deep_review
        or policy.enable_standard_review
    ):
        nodes.append(
            AgentGraphNode(
                id="review",
                agent="reviewer",
                depends_on=[
                    "patch_recovery"
                    if policy.enable_patch_recovery
                    else recovery_dependency
                ],
                activation_reason=(
                    "supervisor_requires_independent_review"
                    if route.mode == ExecutionMode.deep_review
                    else "activate_when_behavior_hypotheses_need_review"
                ),
                tool_profile="review",
                max_turns=1,
                token_budget=8_000,
            )
        )

    strategy = "single_agent"
    if policy.enable_staged_localization:
        strategy = "evidence_first"
    if route.mode == ExecutionMode.deep_review:
        strategy = "deep_review"
    return AgentGraph(
        graph_id=_graph_id(task, policy),
        strategy=strategy,
        risk_signals=list(route.reasons),
        nodes=nodes,
        total_token_budget=sum(node.token_budget for node in nodes),
    )
