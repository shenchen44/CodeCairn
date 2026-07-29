from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from codecairn.github.publishing import (
    GitHubPublicationService,
    check_conclusion,
)
from codecairn.review.models import (
    ChangeProof,
    DeliveryRun,
    DeliveryStep,
)


class DeliveryError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}{': ' + detail if detail else ''}")
        self.code = code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_branch_fragment(value: str) -> str:
    fragment = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._")
    return fragment[:42].lower() or "change"


class DeliveryOrchestrator:
    """Idempotent local Git and GitHub PR delivery for one Change Proof."""

    def __init__(
        self,
        *,
        repo: Path,
        proof: ChangeProof,
        run: DeliveryRun,
        github: GitHubPublicationService,
        repository_slug: str,
        evidence_markdown: str,
        head_repository_slug: str | None = None,
        on_update: Callable[[DeliveryRun], None] | None = None,
    ) -> None:
        self.repo = repo.resolve()
        self.proof = proof
        self.run = run
        self.github = github
        self.repository_slug = repository_slug
        self.head_repository_slug = (
            head_repository_slug or repository_slug
        )
        self.evidence_markdown = evidence_markdown
        self.on_update = on_update or (lambda _: None)

    def _git(self, *args: str, check: bool = True) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-800:]
            raise DeliveryError("delivery_git_failed", detail)
        return completed.stdout.strip()

    def _step(
        self,
        name: str,
        status: str,
        detail: str = "",
        *,
        run_status: str | None = None,
    ) -> None:
        existing = next(
            (item for item in self.run.steps if item.name == name), None
        )
        if existing is None:
            existing = DeliveryStep(name=name, status=status, detail=detail)
            self.run.steps.append(existing)
        else:
            existing.status = status
            existing.detail = detail
            existing.updated_at = _now()
        if run_status is not None:
            self.run.status = run_status
        self.run.updated_at = _now()
        self.on_update(self.run)

    def _changed_paths(self) -> set[str]:
        paths: set[str] = set()
        for item in self.proof.file_changes:
            paths.add(item.path)
            if item.old_path:
                paths.add(item.old_path)
        return paths

    def _preflight(self) -> None:
        self._step("preflight", "running", run_status="preflight")
        if self._git(
            "rev-parse", "--is-inside-work-tree", check=False
        ).lower() != "true":
            raise DeliveryError("delivery_not_git_repository")
        if not self.proof.file_changes:
            raise DeliveryError("delivery_no_changes")
        if self.proof.gate.status == "blocked":
            raise DeliveryError(
                "delivery_review_blocked",
                ",".join(self.proof.gate.reasons),
            )
        if not self.proof.ledger_integrity:
            raise DeliveryError("delivery_ledger_integrity_failed")
        expected = self._changed_paths()
        selected = set(self.run.selected_paths or expected)
        if selected != expected:
            raise DeliveryError(
                "delivery_partial_change_unsupported",
                "select the complete reviewed change",
            )
        for raw in selected:
            path = (self.repo / raw).resolve()
            try:
                path.relative_to(self.repo)
            except ValueError as exc:
                raise DeliveryError(
                    "delivery_path_outside_repository", raw
                ) from exc
        self.run.selected_paths = sorted(selected)
        if not self.run.commit_sha:
            staged = self._git("diff", "--cached", "--name-only")
            staged_by_this_run = any(
                item.name == "stage"
                and item.status in {"completed", "failed"}
                for item in self.run.steps
            )
            if staged and not staged_by_this_run:
                raise DeliveryError(
                    "delivery_preexisting_staged_changes", staged
                )
            if not self._git("status", "--porcelain"):
                raise DeliveryError("delivery_workspace_clean")
        if not self._git("remote", "get-url", "origin", check=False):
            raise DeliveryError("delivery_origin_missing")
        self._step("preflight", "completed", "review and Git checks passed")

    def _prepare_branch(self) -> None:
        if self.run.branch:
            current = self._git("branch", "--show-current")
            if current != self.run.branch:
                self._git("switch", self.run.branch)
            self._step("branch", "completed", self.run.branch)
            return
        self._step("branch", "running")
        current = self._git("branch", "--show-current")
        if not current:
            raise DeliveryError("delivery_detached_head")
        protected = {
            "main",
            "master",
            self.run.base_branch,
            f"origin/{self.run.base_branch}",
        }
        if current in protected:
            digest = hashlib.sha256(
                self.proof.change_id.encode()
            ).hexdigest()[:7]
            candidate = (
                f"codecairn/{_safe_branch_fragment(self.proof.title)}-{digest}"
            )
            remote = self._git(
                "ls-remote", "--heads", "origin", candidate, check=False
            )
            if remote:
                candidate = f"{candidate}-{self.run.id[-6:]}"
            self._git("switch", "-c", candidate)
            current = candidate
        self.run.branch = current
        self._step("branch", "completed", current)

    def _commit(self) -> None:
        if self.run.commit_sha:
            self._step("stage", "skipped", "commit already recorded")
            self._step("commit", "completed", self.run.commit_sha)
            return
        self._step("stage", "running", run_status="committing")
        self._git("add", "-A", "--", *self.run.selected_paths)
        staged = self._git("diff", "--cached", "--name-only")
        if not staged:
            raise DeliveryError("delivery_nothing_staged")
        staged_paths = set(staged.splitlines())
        if not staged_paths <= set(self.run.selected_paths):
            raise DeliveryError(
                "delivery_unreviewed_path_staged",
                ",".join(sorted(staged_paths - set(self.run.selected_paths))),
            )
        self._step("stage", "completed", staged)
        self._step("commit", "running", run_status="committing")
        self._git("commit", "-m", self.run.commit_message)
        self.run.commit_sha = self._git("rev-parse", "HEAD")
        self._step("commit", "completed", self.run.commit_sha)

    def _push(self) -> None:
        self._step("push", "running", run_status="pushing")
        self._git("push", "--set-upstream", "origin", self.run.branch)
        remote_sha = self._git(
            "rev-parse", f"refs/remotes/origin/{self.run.branch}"
        )
        if remote_sha != self.run.commit_sha:
            raise DeliveryError("delivery_remote_head_mismatch")
        self._step("push", "completed", remote_sha)

    async def _create_pr(self) -> dict:
        owner, name = self.repository_slug.split("/", 1)
        head_owner, _ = self.head_repository_slug.split("/", 1)
        if self.run.pr_number:
            result = await self.github.request(
                "GET",
                f"/repos/{owner}/{name}/pulls/{self.run.pr_number}",
            )
            if not isinstance(result, dict):
                raise DeliveryError("delivery_pr_response_invalid")
            self._step(
                "pull_request",
                "completed",
                str(self.run.pr_number),
            )
            return result
        self._step("pull_request", "running", run_status="creating_pr")
        head = quote(f"{head_owner}:{self.run.branch}", safe=":")
        existing = await self.github.request(
            "GET",
            f"/repos/{owner}/{name}/pulls?state=open&head={head}",
        )
        if isinstance(existing, list) and existing:
            pr = existing[0]
        else:
            pr = await self.github.request(
                "POST",
                f"/repos/{owner}/{name}/pulls",
                {
                    "title": self.proof.title[:240],
                    "head": (
                        self.run.branch
                        if head_owner == owner
                        else f"{head_owner}:{self.run.branch}"
                    ),
                    "base": self.run.base_branch,
                    "body": self.evidence_markdown,
                },
            )
        if not isinstance(pr, dict) or not pr.get("number"):
            raise DeliveryError("delivery_pr_response_invalid")
        self.run.pr_number = int(pr["number"])
        self.run.pr_url = str(pr.get("html_url", ""))
        self._step(
            "pull_request",
            "completed",
            f"#{self.run.pr_number}",
        )
        return pr

    async def _publish_evidence(self, pr: dict) -> None:
        self._step(
            "evidence", "running", run_status="publishing_evidence"
        )
        owner, name = self.repository_slug.split("/", 1)
        await self.github.request(
            "PATCH",
            f"/repos/{owner}/{name}/pulls/{self.run.pr_number}",
            {"body": self.evidence_markdown},
        )
        external_id = (
            f"{self.proof.review_family_id}/{self.proof.change_id}"
        )
        checks = await self.github.request(
            "GET",
            f"/repos/{owner}/{name}/commits/"
            f"{self.run.commit_sha}/check-runs",
        )
        existing = []
        if isinstance(checks, dict):
            existing = [
                item
                for item in checks.get("check_runs", [])
                if item.get("name") == "CodeCairn Change Proof"
                and item.get("external_id") == external_id
            ]
        conclusion = check_conclusion(
            gate=self.proof.gate.status,
            assurance=self.proof.assurance.level,
            reasons=self.proof.gate.reasons,
            verification_failed=any(
                item.effective_status == "failed"
                for item in self.proof.verifications
            ),
        )
        payload = {
            "name": "CodeCairn Change Proof",
            "external_id": external_id,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": f"Assurance {self.proof.assurance.level.upper()}",
                "summary": self.evidence_markdown[:65000],
            },
        }
        if existing:
            await self.github.request(
                "PATCH",
                f"/repos/{owner}/{name}/check-runs/{existing[0]['id']}",
                payload,
            )
        else:
            await self.github.request(
                "POST",
                f"/repos/{owner}/{name}/check-runs",
                {"head_sha": self.run.commit_sha, **payload},
            )
        self._step("evidence", "completed", str(pr.get("html_url", "")))

    async def run_delivery(self) -> DeliveryRun:
        if self.run.status == "completed":
            return self.run
        try:
            self._preflight()
            self._prepare_branch()
            self._commit()
            self._push()
            pr = await self._create_pr()
            await self._publish_evidence(pr)
            self.run.status = "completed"
            self.run.completed_at = _now()
            self.run.updated_at = _now()
            self.run.error = ""
            self.on_update(self.run)
            return self.run
        except Exception as exc:
            code = getattr(exc, "code", "delivery_failed")
            self.run.status = "failed"
            self.run.error = code
            self.run.updated_at = _now()
            running = next(
                (
                    item
                    for item in reversed(self.run.steps)
                    if item.status == "running"
                ),
                None,
            )
            if running is not None:
                running.status = "failed"
                running.detail = str(exc)
                running.updated_at = _now()
            self.on_update(self.run)
            raise
