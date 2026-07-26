from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile

from app.services.openai.agent_loop import AgentLoop, AgentRunResult
from app.services.openai.tools import AgentToolbox
from app.services.orchestration.contracts import (
    PatchCandidate,
    PatchTournamentResult,
)
from app.services.orchestration.evidence import build_evidence_ledger
from app.services.sandbox.git_ops import diff
from app.services.sandbox.limits import parse_diff_stats


TOURNAMENT_STRATEGIES = (
    (
        "minimal",
        "Produce the smallest direct fix supported by the localization evidence.",
    ),
    (
        "challenger",
        "Challenge the initial root-cause hypothesis, inspect edge cases, then "
        "produce an alternative minimal fix if the evidence supports it.",
    ),
)


@dataclass(slots=True)
class TournamentSelection:
    result: AgentRunResult
    patch: str
    tournament: PatchTournamentResult


def score_patch_candidate(
    *,
    tests_passed: bool,
    evidence_passed: bool,
    files_changed: int,
    diff_lines: int,
) -> float:
    if not tests_passed:
        return -100.0 - files_changed - diff_lines * 0.01
    return (
        100.0
        + (20.0 if evidence_passed else 0.0)
        - files_changed * 2.0
        - diff_lines * 0.05
    )


class PatchTournament:
    def __init__(self, agent_loop: AgentLoop) -> None:
        self.agent_loop = agent_loop

    def run(
        self,
        toolbox: AgentToolbox,
        *,
        localization_context: dict,
        planning_context: dict | None,
        retry_context: str | None,
    ) -> TournamentSelection:
        if diff(toolbox.repo_path).strip():
            raise RuntimeError(
                "patch_tournament_requires_clean_workspace"
            )

        candidates: list[tuple[PatchCandidate, AgentRunResult | None]] = []
        for candidate_id, strategy_prompt in TOURNAMENT_STRATEGIES:
            candidates.append(
                self._run_candidate(
                    toolbox,
                    candidate_id=candidate_id,
                    strategy_prompt=strategy_prompt,
                    localization_context=localization_context,
                    planning_context=planning_context,
                    retry_context=retry_context,
                )
            )

        valid = [
            item
            for item in candidates
            if item[0].status == "valid" and item[1] is not None
        ]
        if not valid:
            errors = "; ".join(
                f"{candidate.id}:{candidate.error}"
                for candidate, _ in candidates
            )
            raise RuntimeError(
                f"patch_tournament_no_valid_candidate:{errors[:1000]}"
            )

        selected_candidate, selected_result = max(
            valid,
            key=lambda item: (item[0].score, -item[0].diff_lines),
        )
        toolbox.apply_patch(selected_candidate.patch)

        total_model_calls = sum(item.model_calls for item, _ in candidates)
        total_tool_calls = sum(item.tool_calls for item, _ in candidates)
        total_input_tokens = sum(item.input_tokens for item, _ in candidates)
        total_output_tokens = sum(item.output_tokens for item, _ in candidates)
        selected_result.model_call_count = total_model_calls
        selected_result.tool_call_count = total_tool_calls
        selected_result.total_input_tokens = total_input_tokens
        selected_result.total_output_tokens = total_output_tokens

        tournament = PatchTournamentResult(
            trigger_reason="deep_review_route",
            selected_candidate_id=selected_candidate.id,
            candidates=[item for item, _ in candidates],
        )
        selected_result.tournament = tournament.model_dump(mode="json")
        return TournamentSelection(
            result=selected_result,
            patch=selected_candidate.patch,
            tournament=tournament,
        )

    def _run_candidate(
        self,
        toolbox: AgentToolbox,
        *,
        candidate_id: str,
        strategy_prompt: str,
        localization_context: dict,
        planning_context: dict | None,
        retry_context: str | None,
    ) -> tuple[PatchCandidate, AgentRunResult | None]:
        temporary_root = Path(
            tempfile.mkdtemp(
                prefix=f"micro-swe-{candidate_id}-",
                dir=toolbox.repo_path.parent,
            )
        )
        candidate_repo = temporary_root / "workspace"
        result: AgentRunResult | None = None
        try:
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    str(candidate_repo),
                    "HEAD",
                ],
                cwd=toolbox.repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
            candidate_toolbox = AgentToolbox(
                repo_path=candidate_repo,
                repo_config=toolbox.repo_config,
                issue_context=toolbox.issue_context,
                task_context=toolbox.task,
                sandbox_runner=toolbox.sandbox_runner,
                runtime_policy=toolbox.runtime_policy,
            )
            candidate_context = "\n\n".join(
                item
                for item in (retry_context, strategy_prompt)
                if item
            )
            result = self.agent_loop.run(
                candidate_toolbox,
                retry_context=candidate_context,
                localization_context=localization_context,
                planning_context=planning_context,
            )
            patch = diff(candidate_repo)
            if result.patch_text and not patch.strip():
                candidate_toolbox.apply_patch(result.patch_text)
                patch = diff(candidate_repo)
            if not patch.strip():
                raise RuntimeError("candidate_produced_no_diff")

            ledger = build_evidence_ledger(
                issue_context=toolbox.issue_context,
                localization=localization_context,
                summary=result.summary,
                diff_text=patch,
                require_grounded_evidence=True,
            )
            test_result = candidate_toolbox.run_tests()
            stats = parse_diff_stats(patch)
            tests_passed = test_result["exit_code"] == 0
            evidence_passed = bool(
                ledger.gate and ledger.gate.passed
            )
            candidate = PatchCandidate(
                id=candidate_id,
                strategy=strategy_prompt,
                status="valid" if tests_passed else "failed",
                score=score_patch_candidate(
                    tests_passed=tests_passed,
                    evidence_passed=evidence_passed,
                    files_changed=stats.files_changed_count,
                    diff_lines=stats.diff_line_count,
                ),
                patch=patch,
                tests_passed=tests_passed,
                evidence_passed=evidence_passed,
                files_changed=stats.files_changed_count,
                diff_lines=stats.diff_line_count,
                model_calls=result.model_call_count,
                tool_calls=result.tool_call_count,
                input_tokens=result.total_input_tokens,
                output_tokens=result.total_output_tokens,
            )
            return candidate, result
        except Exception as exc:
            return (
                PatchCandidate(
                    id=candidate_id,
                    strategy=strategy_prompt,
                    status="failed",
                    score=-1000.0,
                    model_calls=(
                        result.model_call_count if result else 0
                    ),
                    tool_calls=(
                        result.tool_call_count if result else 0
                    ),
                    input_tokens=(
                        result.total_input_tokens if result else 0
                    ),
                    output_tokens=(
                        result.total_output_tokens if result else 0
                    ),
                    error=str(exc),
                    files_changed=0,
                    diff_lines=0,
                ),
                result,
            )
        finally:
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "remove",
                    "--force",
                    str(candidate_repo),
                ],
                cwd=toolbox.repo_path,
                check=False,
                capture_output=True,
                text=True,
            )
            shutil.rmtree(temporary_root, ignore_errors=True)
