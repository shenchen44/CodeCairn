from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from app.services.adapters import interactive_task
from app.services.agent_runtime import AgentSession, CodingTask, TaskIntent
from app.services.openai.policy import get_runtime_policy
from app.services.openai.staged_runtime import StagedAgentRuntime
from app.services.openai.tools import AgentToolbox
from app.services.sandbox.git_ops import diff
from app.services.sandbox.repo_config import load_repo_config
from app.services.sandbox.runner import SandboxRunner
from codecairn import __version__


def _load_task(args: argparse.Namespace) -> CodingTask:
    if args.task_file:
        return CodingTask.model_validate_json(
            Path(args.task_file).read_text(encoding="utf-8")
        )
    if not args.objective:
        raise ValueError("either --objective or --task-file is required")
    return interactive_task(
        args.objective,
        description=args.description or "",
        intent=TaskIntent(args.intent),
    )


def _run(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).expanduser().resolve()
    task = _load_task(args)
    session_path = (
        Path(args.session_file).expanduser().resolve()
        if args.session_file
        else None
    )
    session = (
        AgentSession.load(session_path)
        if session_path is not None and session_path.exists()
        else AgentSession(path=session_path)
        if session_path is not None
        else None
    )
    policy = get_runtime_policy(args.variant)
    toolbox = AgentToolbox(
        repo_path=repo_path,
        repo_config=load_repo_config(repo_path),
        task_context=task,
        sandbox_runner=SandboxRunner(),
        runtime_policy=policy,
    )
    result = StagedAgentRuntime(policy=policy).run(toolbox, session=session)
    print(
        json.dumps(
            {
                "task": task.prompt_payload(),
                "summary": result.summary,
                "delivery": result.delivery,
                "workspace_diff": diff(repo_path),
                "route": result.route_decision,
                "graph": result.agent_graph,
                "session_id": result.session_id,
                "model_call_count": result.model_call_count,
                "tool_call_count": result.tool_call_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn",
        description="CodeCairn general-purpose coding agent",
    )
    parser.add_argument("--version", action="version", version=f"CodeCairn {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run a coding task on a local repository")
    run.add_argument("--repo", required=True)
    run.add_argument("--objective")
    run.add_argument("--description")
    run.add_argument("--task-file")
    run.add_argument(
        "--intent",
        choices=[item.value for item in TaskIntent],
        default=TaskIntent.change.value,
    )
    run.add_argument(
        "--variant",
        choices=["legacy", "retrieval", "memory", "full"],
        default="full",
    )
    run.add_argument("--session-file", help="Append-only JSONL session path")
    run.set_defaults(handler=_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
