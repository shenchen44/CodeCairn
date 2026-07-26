import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import openai
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.services.adapters import github_issue_task
from app.core.config import get_settings
from app.db.models.task import Task, TaskArtifact, TaskArtifactType, TaskAttempt, TaskResultStatus, TaskStatus
from app.db.models.memory import MemoryKind, MemoryScope
from app.db.session import SessionLocal
from app.services.comments.formatter import format_issue_failure_comment, format_issue_success_comment, format_pr_body
from app.services.github.auth import GitHubAuthService
from app.services.github.issues import GitHubIssueService
from app.services.github.pulls import GitHubPullRequestService
from app.services.github.repos import build_clone_url
from app.services.openai.agent_loop import AgentResponseParseError
from app.services.openai.staged_runtime import LocalizationGateError
from app.services.openai.staged_runtime import EvidenceGateError
from app.services.openai.staged_runtime import PhaseGateError
from app.services.openai.staged_runtime import StagedAgentRuntime as AgentLoop
from app.services.openai.tools import AgentToolbox, ToolExecutionError
from app.services.memory import recall, remember, snapshot_evidence
from app.services.orchestration import attach_verification
from app.services.openai.policy import RuntimePolicy, get_runtime_policy
from app.services.sandbox.git_ops import checkout_new_branch, clone_repo, commit_all, diff, push_branch, set_remote_url
from app.services.sandbox.limits import enforce_patch_limits, parse_diff_stats
from app.services.sandbox.repo_config import load_repo_config
from app.services.sandbox.runner import SandboxRunner
from app.services.task_runner.orchestrator import build_branch_name, ensure_workspace_root, get_artifact_content, mark_task_failed, transition_task


logger = logging.getLogger(__name__)

MAX_CONCURRENT_TASKS = 3
MAX_API_RETRIES = 3
API_RETRY_BASE_DELAY = 10  # seconds


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

class RetryableError(Exception):
    """Transient errors that should be retried (API rate limits, network issues)."""
    pass


class FatalError(Exception):
    """Permanent errors that should immediately fail the task."""
    pass


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def ensure_mapping(value: object) -> dict:
    if isinstance(value, dict):
        return value
    return {}


def _get_raw_webhook(task: Task) -> dict:
    raw_webhook = get_artifact_content(task, TaskArtifactType.raw_webhook)
    if isinstance(raw_webhook, dict):
        return raw_webhook
    raise FatalError("raw_webhook_artifact_missing")


def _build_issue_context(
    task: Task,
    db=None,
    *,
    repo_path: Path | None = None,
    policy: RuntimePolicy | None = None,
) -> dict:
    issue_context = {
        "mode": "integration" if task.issue.github_issue_number <= 0 else "issue_fix",
        "title": task.issue.title,
        "body": task.issue.body,
        "issue_number": task.issue.github_issue_number,
        "repository": f"{task.repository.owner}/{task.repository.name}",
        "default_branch": task.repository.default_branch,
    }
    integration_request = get_artifact_content(task, TaskArtifactType.integration_request)
    if isinstance(integration_request, dict):
        issue_context["integration_request"] = integration_request
    policy = policy or get_runtime_policy()
    if db is not None and policy.enable_memory:
        memory_query = f"{task.issue.title}\n{task.issue.body}"
        issue_context["memory_context"] = recall(
            db,
            repository_id=task.repository_id,
            task_id=task.id,
            query=memory_query,
            limit=6,
            repo_path=repo_path,
        )
    return issue_context


def _remember_attempt_failure(
    db,
    task: Task,
    failure: dict,
    *,
    localization: dict | None = None,
) -> None:
    remember(
        db,
        repository_id=task.repository_id,
        task_id=task.id,
        scope=MemoryScope.task,
        kind=MemoryKind.failure,
        content={
            "issue": task.issue.title,
            "failure_type": failure.get("failure_type", "unknown"),
            "test_exit_code": failure.get("test_exit_code"),
            "error_summary": failure.get("error_summary", "")[:800],
            "guidance": failure.get("guidance", ""),
            "localization_hypothesis": (
                (localization or {}).get("root_cause_hypothesis")
            ),
        },
        evidence=(localization or {}).get("evidence", []),
        confidence=0.7,
        source_commit=task.base_commit,
    )


def _remember_success(
    db,
    task: Task,
    *,
    summary: object,
    localization: dict | None,
    test_command: str,
    repo_path: Path,
) -> None:
    summary_mapping = ensure_mapping(summary)
    remember(
        db,
        repository_id=task.repository_id,
        scope=MemoryScope.repository,
        kind=MemoryKind.solution,
        content={
            "issue": task.issue.title,
            "root_cause": summary_mapping.get("root_cause"),
            "patch_plan": summary_mapping.get("patch_plan"),
            "files": (localization or {}).get("candidate_files", []),
            "test_command": test_command,
        },
        evidence=snapshot_evidence(
            repo_path,
            (localization or {}).get("evidence", []),
        ),
        confidence=float((localization or {}).get("confidence", 0.7)),
        source_commit=task.base_commit,
    )


def _is_conflict_resolution_task(task: Task) -> bool:
    integration_request = get_artifact_content(task, TaskArtifactType.integration_request)
    return isinstance(integration_request, dict) and integration_request.get("mode") == "conflict_resolution"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_ms(start_time: float) -> int:
    return int((time.perf_counter() - start_time) * 1000)


# ---------------------------------------------------------------------------
# API retry helper for async calls
# ---------------------------------------------------------------------------

async def _retry_async_api_call(fn, max_retries: int = MAX_API_RETRIES):
    """Retry an async API call with exponential backoff for transient errors."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError) as exc:
            last_exc = exc
            delay = API_RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(f"api_retry attempt={attempt+1} delay={delay}s error={exc}")
            await asyncio.sleep(delay)
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                last_exc = exc
                delay = API_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(f"api_retry attempt={attempt+1} delay={delay}s status={exc.status_code}")
                await asyncio.sleep(delay)
            else:
                raise
    raise RetryableError(f"api_exhausted_retries: {last_exc}")


# ---------------------------------------------------------------------------
# Attempt recording
# ---------------------------------------------------------------------------

def _record_attempt(
    db,
    task: Task,
    attempt_index: int,
    result_status: TaskResultStatus,
    diff_text: str,
    *,
    model_summary: dict | None = None,
    patch_text: str | None = None,
    test_command: str | None = None,
    test_exit_code: int | None = None,
    test_stdout: str | None = None,
    test_stderr: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    duration_ms: int | None = None,
    model_duration_ms: int | None = None,
    tool_call_count: int = 0,
    error_text: str | None = None,
    tool_name: str | None = None,
    tool_arguments: dict | None = None,
    raw_response: str | None = None,
    total_input_tokens: int = 0,
    total_output_tokens: int = 0,
    turn_durations_ms: list[int] | None = None,
    localization: dict | None = None,
    route_decision: dict | None = None,
    plan: dict | None = None,
    review: dict | None = None,
    agent_graph: dict | None = None,
    evidence_ledger: dict | None = None,
    runtime_events: list[dict] | None = None,
    tournament: dict | None = None,
    recovery: dict | None = None,
) -> None:
    diff_stats = parse_diff_stats(diff_text)
    attempt = TaskAttempt(
        task_id=task.id,
        attempt_index=attempt_index,
        model_summary=model_summary,
        patch_text=patch_text,
        files_changed_count=diff_stats.files_changed_count,
        diff_line_count=diff_stats.diff_line_count,
        test_command=test_command,
        test_exit_code=test_exit_code,
        test_stdout=test_stdout,
        test_stderr=test_stderr,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        model_duration_ms=model_duration_ms,
        tool_call_count=tool_call_count,
        result_status=result_status,
    )
    db.add(attempt)
    db.add(
        TaskArtifact(
            task_id=task.id,
            artifact_type=TaskArtifactType.diff,
            content={"attempt": attempt_index, "diff": diff_text, "error": error_text},
        )
    )
    db.add(
        TaskArtifact(
            task_id=task.id,
            artifact_type=TaskArtifactType.model_response,
            content={
                "attempt": attempt_index,
                "summary": model_summary,
                "patch_text": patch_text,
                "tool_name": tool_name,
                "tool_arguments": tool_arguments,
                "error": error_text,
                "raw_response": raw_response,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "turn_durations_ms": turn_durations_ms or [],
            },
        )
    )
    if test_command is not None or test_stdout is not None or test_stderr is not None or test_exit_code is not None:
        db.add(
            TaskArtifact(
                task_id=task.id,
                artifact_type=TaskArtifactType.test_log,
                content={"stdout": test_stdout or "", "stderr": test_stderr or "", "exit_code": test_exit_code},
            )
        )
    phases = {
        "supervisor": route_decision,
        "agent_graph": agent_graph,
        "localization": localization,
        "planning": plan,
        "review": review,
        "evidence_ledger": evidence_ledger,
        "patch_tournament": tournament,
        "patch_recovery": recovery,
    }
    for phase, phase_result in phases.items():
        if phase_result is None:
            continue
        db.add(
            TaskArtifact(
                task_id=task.id,
                artifact_type=TaskArtifactType.agent_phase,
                content={
                    "attempt": attempt_index,
                    "phase": phase,
                    "contract_version": phase_result.get("contract_version", "1"),
                    "result": phase_result,
                },
            )
        )
    if runtime_events:
        db.add(
            TaskArtifact(
                task_id=task.id,
                artifact_type=TaskArtifactType.agent_phase,
                content={
                    "attempt": attempt_index,
                    "phase": "runtime_events",
                    "contract_version": "1",
                    "result": {"events": runtime_events},
                },
            )
        )


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def process_task(task_id: str) -> None:
    """Process a single task through the full agent pipeline.

    Phases:
    1. Clone & setup workspace
    2. Run agent loop (up to max_attempts rounds)
    3. Create PR on success, or comment failure on issue

    Error handling:
    - RetryableError (API rate limits, timeouts) => logged, task stays triaged for next poll
    - FatalError (install failed, webhook missing) => task marked failed immediately
    """
    settings = get_settings()
    runtime_policy = get_runtime_policy(
        getattr(settings, "agent_runtime_variant", "full")
    )
    db = SessionLocal()
    task_started_clock = time.perf_counter()

    try:
        task = db.scalar(
            select(Task)
            .where(Task.id == task_id)
            .options(selectinload(Task.issue), selectinload(Task.repository), selectinload(Task.artifacts), selectinload(Task.attempts))
        )
        if task is None:
            return
        if task.started_at is None:
            task.started_at = _utcnow()
        task.finished_at = None
        db.add(task)
        db.commit()

        raw_webhook = _get_raw_webhook(task)
        installation_id = raw_webhook["installation"]["id"]
        auth_service = GitHubAuthService()
        installation_token = await _retry_async_api_call(
            lambda: auth_service.get_installation_token(installation_id)
        )
        workspace_root = ensure_workspace_root()

        transition_task(db, task, TaskStatus.sandbox_ready)
        db.commit()

        temp_dir_obj = tempfile.TemporaryDirectory(dir=workspace_root, ignore_cleanup_errors=True)
        try:
            temp_dir = temp_dir_obj.name
            repo_path = Path(temp_dir) / task.repository.name
            authenticated_clone_url = build_clone_url(task.repository.owner, task.repository.name, installation_token)
            clone_repo(authenticated_clone_url, repo_path)
            set_remote_url(repo_path, "origin", authenticated_clone_url)
            branch_name = build_branch_name(task.issue, task.id)
            base_commit = checkout_new_branch(repo_path, branch_name, task.repository.default_branch)
            task.branch_name = branch_name
            task.base_commit = base_commit
            db.add(task)
            db.commit()

            repo_config = load_repo_config(repo_path)
            sandbox = SandboxRunner()
            install_started = time.perf_counter()
            install_result = sandbox.install_dependencies(repo_path, repo_config.install_command)
            task.install_duration_ms = _elapsed_ms(install_started)
            db.add(
                TaskArtifact(
                    task_id=task.id,
                    artifact_type=TaskArtifactType.install_log,
                    content={
                        "command": repo_config.install_command,
                        "exit_code": install_result.exit_code,
                        "stdout": install_result.stdout,
                        "stderr": install_result.stderr,
                    },
                )
            )
            db.commit()
            if install_result.exit_code != 0:
                raise FatalError(
                    f"install_failed: stdout={install_result.stdout.strip()} stderr={install_result.stderr.strip()}"
                )

            agent = AgentLoop()
            transition_task(db, task, TaskStatus.patching)
            db.commit()

            successful_result = None
            successful_attempt_index = None
            successful_diff_text = ""
            previous_failure = None  # Track failure for reflective retry

            for attempt_index in range(1, settings.max_attempts + 1):
                attempt_started_at = _utcnow()
                attempt_started_clock = time.perf_counter()
                task.attempt_count = attempt_index
                db.add(task)
                db.commit()

                legacy_context = _build_issue_context(
                    task,
                    db,
                    repo_path=repo_path,
                    policy=runtime_policy,
                )
                toolbox = AgentToolbox(
                    repo_path=repo_path,
                    repo_config=repo_config,
                    issue_context=legacy_context,
                    task_context=github_issue_task(legacy_context),
                    sandbox_runner=sandbox,
                    runtime_policy=runtime_policy,
                )
                result = None
                tool_name = None
                tool_arguments = None
                error_text = None
                raw_response = None
                model_started = time.perf_counter()

                # Build reflective retry context from previous failure
                retry_context = None
                if previous_failure and attempt_index > 1:
                    retry_context = (
                        f"=== RETRY ATTEMPT {attempt_index} ===\n"
                        f"Your previous patch FAILED.\n\n"
                        f"Failure type: {previous_failure.get('failure_type', 'unknown')}\n"
                        f"Test exit code: {previous_failure.get('test_exit_code', '?')}\n"
                        f"Error summary:\n{previous_failure.get('error_summary', 'N/A')}\n\n"
                        f"Guidance: {previous_failure.get('guidance', 'Analyze the failure carefully.')}\n\n"
                        f"What to do now:\n"
                        f"1. Analyze WHY your previous fix failed (see error output above)\n"
                        f"2. If the failure_type is 'assertion_failure', your root cause analysis was likely wrong — re-read the issue\n"
                        f"3. If the failure_type is 'import_error' or 'name_error', you broke a dependency — use get_imports to check\n"
                        f"4. Try a DIFFERENT approach — do not repeat the same fix\n"
                        f"5. Use find_definition to verify you are modifying the correct function\n"
                    )

                try:
                    result = agent.run(toolbox, retry_context=retry_context)
                    model_duration_ms = _elapsed_ms(model_started)
                    task.model_call_count += result.model_call_count
                    task.tool_call_count += result.tool_call_count
                    diff_before_patch_text = diff(repo_path)
                    if result.patch_text and not diff_before_patch_text.strip():
                        toolbox.apply_patch(result.patch_text)

                    diff_text = diff(repo_path)
                    enforce_patch_limits(
                        diff_text,
                        repo_config.allowed_paths,
                        repo_config.blocked_paths,
                        repo_config.max_changed_files,
                        repo_config.max_diff_lines,
                    )

                    transition_task(db, task, TaskStatus.testing)
                    test_started = time.perf_counter()
                    test_result = sandbox.run_tests(repo_path, repo_config.test_command)
                    result.evidence_ledger = attach_verification(
                        result.evidence_ledger,
                        command=repo_config.test_command,
                        exit_code=test_result.exit_code,
                        stdout=test_result.stdout,
                        stderr=test_result.stderr,
                    )
                    task.patch_duration_ms = (task.patch_duration_ms or 0) + model_duration_ms
                    test_duration_ms = _elapsed_ms(test_started)
                    task.test_duration_ms = (task.test_duration_ms or 0) + test_duration_ms
                    _record_attempt(
                        db,
                        task,
                        attempt_index,
                        TaskResultStatus.success if test_result.exit_code == 0 else TaskResultStatus.failed,
                        diff_text,
                        model_summary=result.summary,
                        patch_text=result.patch_text,
                        test_command=repo_config.test_command,
                        test_exit_code=test_result.exit_code,
                        test_stdout=test_result.stdout,
                        test_stderr=test_result.stderr,
                        started_at=attempt_started_at,
                        finished_at=_utcnow(),
                        duration_ms=_elapsed_ms(attempt_started_clock),
                        model_duration_ms=model_duration_ms,
                        tool_call_count=result.tool_call_count,
                        total_input_tokens=result.total_input_tokens,
                        total_output_tokens=result.total_output_tokens,
                        turn_durations_ms=result.turn_durations_ms,
                        localization=result.localization,
                        route_decision=result.route_decision,
                        plan=result.plan,
                        review=result.review,
                        agent_graph=result.agent_graph,
                        evidence_ledger=result.evidence_ledger,
                        runtime_events=result.runtime_events,
                        tournament=result.tournament,
                        recovery=result.recovery,
                    )
                    db.commit()

                    if test_result.exit_code == 0:
                        if runtime_policy.enable_memory:
                            _remember_success(
                                db,
                                task,
                                summary=result.summary,
                                localization=result.localization,
                                test_command=repo_config.test_command,
                                repo_path=repo_path,
                            )
                        transition_task(db, task, TaskStatus.ready_for_pr)
                        successful_result = result
                        successful_attempt_index = attempt_index
                        successful_diff_text = diff_text
                        previous_failure = None
                        break

                    # Track failure for reflective retry
                    from app.services.openai.tools import _classify_test_failure, FAILURE_GUIDANCE
                    failure_type = _classify_test_failure(test_result.stdout, test_result.stderr)
                    previous_failure = {
                        "failure_type": failure_type,
                        "test_exit_code": test_result.exit_code,
                        "error_summary": (test_result.stderr or test_result.stdout or "")[-800:],
                        "guidance": FAILURE_GUIDANCE.get(failure_type, FAILURE_GUIDANCE["unknown"]),
                    }
                    if runtime_policy.enable_memory:
                        _remember_attempt_failure(
                            db,
                            task,
                            previous_failure,
                            localization=result.localization,
                        )

                    if attempt_index < settings.max_attempts:
                        transition_task(db, task, TaskStatus.retrying)
                        db.commit()
                        transition_task(db, task, TaskStatus.patching)
                        db.commit()
                    else:
                        mark_task_failed(db, task, "tests_failed", {"attempts": attempt_index})
                        db.commit()
                except RetryableError:
                    raise  # Let outer handler deal with it
                except Exception as exc:
                    diff_text = diff(repo_path)
                    error_text = str(exc)
                    model_duration_ms = _elapsed_ms(model_started)
                    task.patch_duration_ms = (task.patch_duration_ms or 0) + model_duration_ms
                    test_command = None
                    test_exit_code = None
                    test_stdout = None
                    test_stderr = None
                    if isinstance(exc, ToolExecutionError):
                        tool_name = exc.tool_name
                        tool_arguments = exc.arguments
                        diff_text = exc.diff_text
                    if isinstance(exc, AgentResponseParseError):
                        raw_response = exc.raw_response
                        tool_name = "final_response"
                    localization = result.localization if result is not None else None
                    route_decision = result.route_decision if result is not None else None
                    plan = result.plan if result is not None else None
                    review = result.review if result is not None else None
                    agent_graph = (
                        result.agent_graph if result is not None else None
                    )
                    evidence_ledger = (
                        result.evidence_ledger if result is not None else None
                    )
                    runtime_events = (
                        result.runtime_events if result is not None else None
                    )
                    tournament = (
                        result.tournament if result is not None else None
                    )
                    recovery = (
                        result.recovery if result is not None else None
                    )
                    if isinstance(exc, LocalizationGateError):
                        localization = {
                            **exc.localization,
                            "gate": {
                                "passed": False,
                                "reasons": exc.reasons,
                            },
                        }
                    if isinstance(exc, PhaseGateError):
                        route_decision = exc.context.get("route")
                        localization = exc.context.get("localization")
                        plan = exc.context.get("plan")
                        review = exc.context.get("review")
                        rejected_phase = (
                            review if exc.phase == "review" else plan
                        )
                        if rejected_phase is not None:
                            rejected_phase = {
                                **rejected_phase,
                                "gate": {
                                    "passed": False,
                                    "reasons": exc.reasons,
                                },
                            }
                            if exc.phase == "review":
                                review = rejected_phase
                            else:
                                plan = rejected_phase
                    if isinstance(exc, EvidenceGateError):
                        agent_graph = exc.graph
                        evidence_ledger = exc.ledger
                        runtime_events = exc.events
                    if diff_text.strip():
                        test_started = time.perf_counter()
                        test_result = sandbox.run_tests(repo_path, repo_config.test_command)
                        test_command = repo_config.test_command
                        test_exit_code = test_result.exit_code
                        test_stdout = test_result.stdout
                        test_stderr = test_result.stderr
                        test_duration_ms = _elapsed_ms(test_started)
                        task.test_duration_ms = (task.test_duration_ms or 0) + test_duration_ms
                        evidence_ledger = attach_verification(
                            evidence_ledger,
                            command=repo_config.test_command,
                            exit_code=test_result.exit_code,
                            stdout=test_result.stdout,
                            stderr=test_result.stderr,
                        )
                    _record_attempt(
                        db,
                        task,
                        attempt_index,
                        TaskResultStatus.failed,
                        diff_text,
                        model_summary=result.summary if result is not None else None,
                        patch_text=result.patch_text if result is not None else None,
                        test_command=test_command,
                        test_exit_code=test_exit_code,
                        test_stdout=test_stdout,
                        test_stderr=test_stderr,
                        started_at=attempt_started_at,
                        finished_at=_utcnow(),
                        duration_ms=_elapsed_ms(attempt_started_clock),
                        model_duration_ms=model_duration_ms,
                        tool_call_count=result.tool_call_count if result is not None else 0,
                        error_text=error_text,
                        tool_name=tool_name,
                        tool_arguments=tool_arguments,
                        raw_response=raw_response,
                        total_input_tokens=result.total_input_tokens if result is not None else 0,
                        total_output_tokens=result.total_output_tokens if result is not None else 0,
                        turn_durations_ms=result.turn_durations_ms if result is not None else None,
                        localization=localization,
                        route_decision=route_decision,
                        plan=plan,
                        review=review,
                        agent_graph=agent_graph,
                        evidence_ledger=evidence_ledger,
                        runtime_events=runtime_events,
                        tournament=tournament,
                        recovery=recovery,
                    )
                    db.commit()
                    # Track exception failure for reflective retry
                    previous_failure = {
                        "failure_type": "tool_error" if isinstance(exc, ToolExecutionError) else "parse_error" if isinstance(exc, AgentResponseParseError) else "unknown",
                        "test_exit_code": test_exit_code,
                        "error_summary": error_text[:800] if error_text else "N/A",
                        "guidance": f"Your tool call failed: {error_text[:200]}. Read the error carefully and adjust your approach.",
                    }
                    if runtime_policy.enable_memory:
                        _remember_attempt_failure(
                            db,
                            task,
                            previous_failure,
                            localization=localization,
                        )
                    if attempt_index < settings.max_attempts:
                        transition_task(db, task, TaskStatus.retrying)
                        db.commit()
                        transition_task(db, task, TaskStatus.patching)
                        db.commit()
                    else:
                        mark_task_failed(
                            db,
                            task,
                            "patch_failed",
                            {
                                "attempts": attempt_index,
                                "error": error_text,
                                "tool_name": tool_name,
                                "tool_arguments": tool_arguments,
                                "raw_response": raw_response,
                            },
                        )
                        db.commit()

            should_comment_on_issue = task.issue.github_issue_number > 0

            if successful_result is None:
                if should_comment_on_issue:
                    issue_service = GitHubIssueService(installation_token)
                    await issue_service.create_comment(
                        task.repository.owner,
                        task.repository.name,
                        task.issue.github_issue_number,
                        format_issue_failure_comment(task.failure_reason["reason"], task.attempt_count),
                    )
                task.finished_at = _utcnow()
                task.total_duration_ms = _elapsed_ms(task_started_clock)
                db.commit()
                return

            try:
                changed_files = parse_diff_stats(successful_diff_text).changed_files
                commit_message = f"fix: resolve issue #{task.issue.github_issue_number}"
                head_commit = commit_all(repo_path, commit_message, include_paths=changed_files)
                task.head_commit = head_commit
                db.add(task)
                db.commit()

                set_remote_url(repo_path, "origin", authenticated_clone_url)
                push_branch(repo_path, branch_name)

                summary_map = ensure_mapping(successful_result.summary)
                pr_body_summary_map = ensure_mapping(successful_result.pr_body_summary)
                root_cause = pr_body_summary_map.get("root_cause") or summary_map.get("root_cause") or "Issue-specific bug"
                changes = pr_body_summary_map.get("changes") or summary_map.get("patch_plan") or ["Minimal targeted patch"]
                pr_body = format_pr_body(
                    issue_number=task.issue.github_issue_number,
                    root_cause=root_cause,
                    changes=changes,
                    validation_summary="pytest passed",
                )
                db.add(TaskArtifact(task_id=task.id, artifact_type=TaskArtifactType.pr_body, content={"body": pr_body}))
                db.commit()

                pr_service = GitHubPullRequestService(installation_token)
                pr = await pr_service.create_pull_request(
                    owner=task.repository.owner,
                    repo=task.repository.name,
                    title=successful_result.pr_title,
                    body=pr_body,
                    head=branch_name,
                    base=task.repository.default_branch,
                )
                task.pr_number = pr["number"]
                transition_task(db, task, TaskStatus.pr_opened)
                db.commit()

                if _is_conflict_resolution_task(task):
                    integration_request = ensure_mapping(get_artifact_content(task, TaskArtifactType.integration_request))
                    source_task_ids = integration_request.get("source_task_ids") or []
                    source_pr_numbers = integration_request.get("source_pr_numbers") or []
                    if source_task_ids:
                        source_task = db.get(Task, source_task_ids[0])
                        if source_task is not None:
                            db.add(
                                TaskArtifact(
                                    task_id=source_task.id,
                                    artifact_type=TaskArtifactType.resolution_link,
                                    content={
                                        "resolved_task_id": task.id,
                                        "resolved_pr_number": pr["number"],
                                        "resolved_pr_url": pr["html_url"],
                                    },
                                )
                            )
                            db.add(
                                TaskArtifact(
                                    task_id=task.id,
                                    artifact_type=TaskArtifactType.resolution_link,
                                    content={
                                        "source_task_id": source_task.id,
                                        "source_pr_number": source_pr_numbers[0] if source_pr_numbers else source_task.pr_number,
                                    },
                                )
                            )
                            db.commit()
                            issue_service = GitHubIssueService(installation_token)
                            source_pr_number = source_pr_numbers[0] if source_pr_numbers else source_task.pr_number
                            if source_pr_number:
                                await issue_service.create_comment(
                                    task.repository.owner,
                                    task.repository.name,
                                    source_pr_number,
                                    (
                                        "CodeCairn generated a conflict-resolved follow-up PR.\n\n"
                                        f"- Original PR: #{source_pr_number}\n"
                                        f"- Replacement PR: {pr['html_url']}\n"
                                        "- Please review and merge the replacement PR instead of this conflicted one.\n"
                                    ),
                                )

                if should_comment_on_issue:
                    issue_service = GitHubIssueService(installation_token)
                    await issue_service.create_comment(
                        task.repository.owner,
                        task.repository.name,
                        task.issue.github_issue_number,
                        format_issue_success_comment(pr["html_url"], successful_attempt_index or task.attempt_count, True),
                    )
                    await issue_service.add_labels(task.repository.owner, task.repository.name, pr["number"], [settings.pr_review_label])
                transition_task(db, task, TaskStatus.done)
                task.finished_at = _utcnow()
                task.total_duration_ms = _elapsed_ms(task_started_clock)
                db.commit()
                return
            except Exception as exc:
                mark_task_failed(
                    db,
                    task,
                    "pr_failed",
                    {
                        "attempts": successful_attempt_index,
                        "error": str(exc),
                        "branch_name": task.branch_name,
                        "head_commit": task.head_commit,
                    },
                )
                task.finished_at = _utcnow()
                task.total_duration_ms = _elapsed_ms(task_started_clock)
                db.commit()
        finally:
            temp_dir_obj.cleanup()
    except RetryableError as exc:
        logger.warning(f"task_retryable_error task_id={task_id} error={exc}")
        # Task stays in triaged state, will be picked up by next poll
    except FatalError as exc:
        logger.error(f"task_fatal_error task_id={task_id} error={exc}")
        task = db.get(Task, task_id)
        if task is not None:
            mark_task_failed(db, task, "fatal_error", {"error": str(exc)})
            task.finished_at = _utcnow()
            task.total_duration_ms = _elapsed_ms(task_started_clock)
            db.commit()
    except Exception as exc:
        logger.exception("task processing failed", extra={"task_id": task_id})
        task = db.get(Task, task_id)
        if task is not None:
            mark_task_failed(db, task, "worker_exception", {"error": str(exc)})
            task.finished_at = _utcnow()
            task.total_duration_ms = _elapsed_ms(task_started_clock)
            db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Worker loop with concurrency
# ---------------------------------------------------------------------------

async def poll_forever() -> None:
    """Poll for triaged tasks and process them with bounded concurrency."""
    settings = get_settings()
    running_tasks: set[asyncio.Task] = set()

    while True:
        # Clean up completed tasks
        done_tasks = {t for t in running_tasks if t.done()}
        for t in done_tasks:
            if t.exception():
                logger.error(f"background_task_failed error={t.exception()}")
        running_tasks -= done_tasks

        # Start new tasks if under concurrency limit
        if len(running_tasks) < MAX_CONCURRENT_TASKS:
            db = SessionLocal()
            try:
                task = db.scalar(
                    select(Task)
                    .where(Task.status == TaskStatus.triaged)
                    .order_by(Task.created_at.asc())
                    .options(selectinload(Task.issue), selectinload(Task.repository), selectinload(Task.artifacts))
                )
                if task is not None:
                    logger.info(f"dispatching_task task_id={task.id}")
                    running_tasks.add(asyncio.create_task(process_task(task.id)))
            finally:
                db.close()

        await asyncio.sleep(settings.worker_poll_interval)


def main() -> None:
    asyncio.run(poll_forever())


if __name__ == "__main__":
    main()
