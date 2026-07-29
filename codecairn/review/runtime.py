from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codecairn.review.models import (
    AgentRun,
    ChangeRequest,
    ReviewMessage,
    ReviewThread,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ReviewEventBroker:
    def __init__(self, *, max_events: int = 1000) -> None:
        self._condition = threading.Condition()
        self._events: list[dict[str, Any]] = []
        self._sequence = 0
        self._max_events = max_events

    def publish(self, event_type: str, payload: dict[str, Any]) -> int:
        with self._condition:
            self._sequence += 1
            event = {
                "sequence": self._sequence,
                "type": event_type,
                "timestamp": _now().isoformat(),
                "payload": payload,
            }
            self._events.append(event)
            self._events = self._events[-self._max_events :]
            self._condition.notify_all()
            return self._sequence

    def events_after(self, sequence: int) -> list[dict[str, Any]]:
        with self._condition:
            return [
                dict(item)
                for item in self._events
                if item["sequence"] > sequence
            ]

    @staticmethod
    def encode(event: dict[str, Any]) -> str:
        sequence = int(event["sequence"])
        return (
            f"id: {sequence}\n"
            f"event: {event['type']}\n"
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        )

    def stream(self, after: int = 0) -> Iterator[str]:
        cursor = after
        while True:
            with self._condition:
                ready = [
                    item
                    for item in self._events
                    if item["sequence"] > cursor
                ]
                if not ready:
                    self._condition.wait(timeout=15)
                    ready = [
                        item
                        for item in self._events
                        if item["sequence"] > cursor
                    ]
            if not ready:
                yield ": keepalive\n\n"
                continue
            for event in ready:
                cursor = int(event["sequence"])
                yield self.encode(event)


class PiAgentManager:
    """Runs one Pi RPC turn at a time and mirrors observable events."""

    def __init__(
        self,
        *,
        repo: Path,
        get_proof: Callable[[], Any],
        persist: Callable[[], None],
        refresh_after_change: Callable[[AgentRun], None],
        broker: ReviewEventBroker,
    ) -> None:
        self.repo = repo.resolve()
        self.get_proof = get_proof
        self.persist = persist
        self.refresh_after_change = refresh_after_change
        self.broker = broker
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def _proof(self) -> Any:
        return self.get_proof()

    def _thread(self, thread_id: str) -> ReviewThread:
        thread = next(
            (
                item
                for item in self._proof().review_threads
                if item.id == thread_id
            ),
            None,
        )
        if thread is None:
            raise ValueError("review_thread_not_found")
        return thread

    def _change_request(self, run_id: str) -> ChangeRequest | None:
        return next(
            (
                item
                for item in self._proof().change_requests
                if item.agent_run_id == run_id
            ),
            None,
        )

    def _finish_change_request(
        self, run_id: str, status: str
    ) -> None:
        request = self._change_request(run_id)
        if request is None:
            return
        request.status = status
        request.completed_at = _now()

    def start(self, run: AgentRun) -> AgentRun:
        with self._lock:
            if any(
                item.status in {"starting", "running"}
                for item in self._proof().agent_runs
            ):
                raise ValueError("agent_run_already_active")
            self._proof().agent_runs.append(run)
            thread = self._thread(run.thread_id)
            thread.messages.append(
                ReviewMessage(
                    id=f"message_{run.id}_user",
                    role="user",
                    content=run.prompt,
                    agent_run_id=run.id,
                )
            )
            thread.updated_at = _now()
            self.persist()
        threading.Thread(
            target=self._execute,
            args=(run.id,),
            daemon=True,
            name=f"codecairn-agent-{run.id[-8:]}",
        ).start()
        return run

    def abort(self, run_id: str) -> bool:
        with self._lock:
            process = self._processes.get(run_id)
            run = next(
                (
                    item
                    for item in self._proof().agent_runs
                    if item.id == run_id
                ),
                None,
            )
            if process is None or run is None:
                return False
            process.terminate()
            run.status = "cancelled"
            run.finished_at = _now()
            self._finish_change_request(run_id, "cancelled")
            self.persist()
            self.broker.publish(
                "agent_status",
                {"run_id": run_id, "status": "cancelled"},
            )
            return True

    def _update(self, run: AgentRun, event_type: str, **payload: Any) -> None:
        run.event_count += 1
        self.persist()
        self.broker.publish(
            event_type,
            {"run_id": run.id, "status": run.status, **payload},
        )

    def _prompt(self, run: AgentRun) -> str:
        proof = self._proof()
        target = "the complete change"
        target_detail = ""
        if run.target_type == "file" and run.target_id:
            file_change = next(
                (
                    item
                    for item in proof.file_changes
                    if item.id == run.target_id
                ),
                None,
            )
            if file_change is not None:
                target = f"file:{file_change.path}"
                target_detail = (
                    f"Change type: {file_change.change_type}\n"
                    f"Summary: {file_change.summary}\n"
                )
        elif run.target_type == "hunk" and run.target_id:
            hunk = next(
                (
                    item
                    for item in proof.patch_hunks
                    if item.id == run.target_id
                ),
                None,
            )
            if hunk is not None:
                target = f"hunk:{hunk.path}:{hunk.header}"
                target_detail = (
                    f"Hunk summary: {hunk.summary}\n"
                    f"Reviewed diff:\n{hunk.diff[:6000]}\n"
                )
        elif run.target_id:
            target = f"{run.target_type}:{run.target_id}"
        context = (
            f"CodeCairn review revision: {proof.revision_id}\n"
            f"Review target: {target}\n"
            f"{target_detail}"
            f"Task: {run.prompt}\n"
        )
        if run.mode == "ask":
            return (
                context
                + "\nThis is a read-only review question. Inspect the repository "
                "and answer with concise file, symbol, and line references. "
                "Do not modify files or run commands that can mutate state."
            )
        return (
            context
            + "\nImplement the requested correction in the repository. Inspect "
            "the relevant source first, record a cairn_decision before every "
            "mutation, run focused verification, and summarize changed files "
            "and remaining risks."
        )

    def _execute(self, run_id: str) -> None:
        run = next(
            item for item in self._proof().agent_runs if item.id == run_id
        )
        thread = self._thread(run.thread_id)
        answer_parts: list[str] = []
        process: subprocess.Popen[str] | None = None
        pi = shutil.which("pi")
        if pi is None:
            run.status = "failed"
            run.error = "pi_command_unavailable"
            run.finished_at = _now()
            self._finish_change_request(run.id, "failed")
            self.persist()
            self.broker.publish(
                "agent_status",
                {"run_id": run.id, "status": "failed", "error": run.error},
            )
            return
        env = os.environ.copy()
        if run.mode == "ask":
            env["CODECAIRN_REVIEW_READ_ONLY"] = "1"
        run.status = "starting"
        run.started_at = _now()
        self.persist()
        self.broker.publish(
            "agent_status", {"run_id": run.id, "status": "starting"}
        )
        try:
            process = subprocess.Popen(
                [
                    pi,
                    "--mode",
                    "rpc",
                    "--no-session",
                    "--approve",
                    "--name",
                    f"CodeCairn {run.mode}",
                ],
                cwd=self.repo,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
            with self._lock:
                self._processes[run.id] = process
            run.process_id = process.pid
            run.status = "running"
            self._update(run, "agent_status")
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(
                json.dumps(
                    {
                        "id": run.id,
                        "type": "prompt",
                        "message": self._prompt(run),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            process.stdin.flush()
            for raw in process.stdout:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                kind = str(event.get("type", "agent_event"))
                if kind == "message_update":
                    delta = event.get("assistantMessageEvent") or {}
                    if delta.get("type") == "text_delta":
                        text = str(delta.get("delta", ""))
                        answer_parts.append(text)
                        run.answer = "".join(answer_parts)[-50000:]
                        self._update(run, "agent_text", delta=text)
                        continue
                if kind.startswith("tool_execution_"):
                    self._update(
                        run,
                        "agent_tool",
                        phase=kind.removeprefix("tool_execution_"),
                        tool=str(
                            event.get("toolName")
                            or event.get("tool_name")
                            or ""
                        ),
                    )
                elif kind == "agent_settled":
                    process.stdin.close()
                    break
                elif kind in {
                    "agent_start",
                    "turn_start",
                    "turn_end",
                    "auto_retry_start",
                    "auto_retry_end",
                    "extension_error",
                }:
                    self._update(run, "agent_event", agent_event_type=kind)
            return_code = process.wait(timeout=15)
            if run.status == "cancelled":
                return
            stderr = process.stderr.read() if process.stderr else ""
            if return_code != 0:
                raise RuntimeError(stderr.strip()[-1000:] or "pi_rpc_failed")
            run.answer = "".join(answer_parts).strip()
            run.status = "completed"
            run.finished_at = _now()
            thread.messages.append(
                ReviewMessage(
                    id=f"message_{run.id}_assistant",
                    role="assistant",
                    content=run.answer or "Agent completed without text output.",
                    agent_run_id=run.id,
                )
            )
            thread.updated_at = _now()
            self.persist()
            if run.mode == "change":
                self.refresh_after_change(run)
            self.broker.publish(
                "agent_status",
                {
                    "run_id": run.id,
                    "status": "completed",
                    "result_revision_id": run.result_revision_id,
                },
            )
        except Exception as exc:
            if run.status != "cancelled":
                run.status = "failed"
                run.error = str(exc)[-1000:]
                run.finished_at = _now()
                self._finish_change_request(run.id, "failed")
                self.persist()
                self.broker.publish(
                    "agent_status",
                    {
                        "run_id": run.id,
                        "status": "failed",
                        "error": run.error,
                    },
                )
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            with self._lock:
                self._processes.pop(run.id, None)
