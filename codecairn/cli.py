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
from codecairn.interactive import (
    InteractiveShell,
    default_session_path,
    load_session,
    resolve_repository,
)


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
    repo_path = resolve_repository(Path(args.repo))
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


def _add_interactive_options(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument(
        "--repo",
        default=".",
        help="Repository path (default: current directory)",
    )
    parser.add_argument(
        "--variant",
        choices=["legacy", "retrieval", "memory", "full"],
        default="full",
    )
    parser.add_argument(
        "--session-file",
        help="Persistent JSONL session path",
    )


def _chat(args: argparse.Namespace) -> int:
    repo_path = resolve_repository(Path(args.repo))
    session_path = (
        Path(args.session_file).expanduser().resolve()
        if args.session_file
        else default_session_path(repo_path)
    )
    session = load_session(session_path)
    policy = get_runtime_policy(args.variant)
    return InteractiveShell(
        repo_path=repo_path,
        policy=policy,
        session=session,
    ).run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn",
        description="CodeCairn general-purpose coding agent",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"CodeCairn {__version__}",
    )
    _add_interactive_options(parser)
    commands = parser.add_subparsers(dest="command")
    chat = commands.add_parser(
        "chat",
        help="Start an interactive coding session",
    )
    _add_interactive_options(chat)
    chat.set_defaults(handler=_chat)
    run = commands.add_parser("run", help="Run a coding task on a local repository")
    run.add_argument("--repo", default=".")
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
    handler = getattr(args, "handler", _chat)
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
