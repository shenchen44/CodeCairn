from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.adapters import interactive_task  # noqa: E402
from app.services.agent_runtime import (  # noqa: E402
    AgentSession,
    CodingTask,
    TaskIntent,
)
from app.services.openai.policy import get_runtime_policy  # noqa: E402
from app.services.openai.staged_runtime import StagedAgentRuntime  # noqa: E402
from app.services.openai.tools import AgentToolbox  # noqa: E402
from app.services.sandbox.git_ops import diff  # noqa: E402
from app.services.sandbox.repo_config import load_repo_config  # noqa: E402
from app.services.sandbox.runner import SandboxRunner  # noqa: E402


def _load_task(args: argparse.Namespace) -> CodingTask:
    if args.task_file:
        return CodingTask.model_validate_json(
            Path(args.task_file).read_text(encoding="utf-8")
        )
    if not args.objective:
        raise ValueError(
            "either --objective or --task-file is required"
        )
    return interactive_task(
        args.objective,
        description=args.description or "",
        intent=TaskIntent(args.intent),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the general coding agent on a local repository",
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--objective")
    parser.add_argument("--description")
    parser.add_argument("--task-file")
    parser.add_argument(
        "--intent",
        choices=[item.value for item in TaskIntent],
        default=TaskIntent.change.value,
    )
    parser.add_argument(
        "--variant",
        choices=["legacy", "retrieval", "memory", "full"],
        default="full",
    )
    parser.add_argument(
        "--session-file",
        help="Append-only JSONL session path",
    )
    args = parser.parse_args()

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
    result = StagedAgentRuntime(policy=policy).run(
        toolbox,
        session=session,
    )
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


if __name__ == "__main__":
    main()
