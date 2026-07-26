# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

```bash
# Install dependencies
python -m pip install -r requirements.txt

# Run API server (development)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Run worker (separate terminal)
python -m app.workers.poller

# Database migration
alembic upgrade head

# Run all tests
pytest app/tests -q -p no:cacheprovider

# Run single test file
pytest app/tests/test_state_machine.py -q

# Docker Compose (full stack)
docker compose up --build
docker compose exec api alembic upgrade head
```

## Architecture

### Process Model
- **API Process**: FastAPI application handling GitHub webhooks, task queries, and dashboard
- **Worker Process**: Async poller that processes `triaged` tasks through the agent loop

### Core Flow
1. GitHub webhook arrives at `POST /webhooks/github`
2. `should_process_issue_event()` filters by labels (`good first issue`, `bug`, `agent-fixable`)
3. Task created with status `triaged`
4. Worker picks up task, clones repo to temp directory
5. `AgentLoop` runs OpenAI agent with tools (`list_files`, `read_file`, `apply_patch`, `run_tests`)
6. Up to `max_attempts` (default 3) patch → test → retry cycles
7. On success: commit, push branch, create PR, comment on issue
8. On failure: mark failed, comment with error

### State Machine
```
received -> triaged -> sandbox_ready -> patching -> testing -> retrying -> patching
testing -> ready_for_pr -> pr_opened -> done
* -> failed
```
Defined in `app/services/task_runner/state_machine.py`.

### Sandbox Security
- All changes happen on a new branch
- `load_repo_config()` reads `.agent.yml` for `allowed_paths`, `blocked_paths`, `max_changed_files`, `max_diff_lines`
- `enforce_patch_limits()` validates diff against limits before running tests
- Commands are checked for dangerous patterns before execution
- Docker container with memory/CPU limits runs in isolation

### Key Components
- `app/workers/poller.py`: Main worker loop and `process_task()` orchestration
- `app/services/openai/agent_loop.py`: OpenAI Responses API loop with tool dispatch
- `app/services/openai/tools.py`: `AgentToolbox` with `list_files`, `read_file`, `apply_patch`, `git_diff`, `run_tests`
- `app/services/sandbox/runner.py`: Docker container execution for install/test commands
- `app/services/sandbox/git_ops.py`: clone, checkout, commit, push operations
- `app/api/routes/github_webhooks.py`: Webhook endpoint with signature verification

### Database Models
- `Task`: Central model with status, branch_name, base_commit, pr_number
- `TaskAttempt`: Records each patch attempt with diff, test results, summary
- `TaskArtifact`: Stores diff, test_log, pr_body, install_log, raw_webhook
- `Issue`, `Repository`: Linked to tasks

### Environment Variables
- `DATABASE_URL`: PostgreSQL connection (default: `postgresql+psycopg://postgres:postgres@localhost:5432/codecairn`)
- `OPENAI_API_KEY`, `OPENAI_MODEL`: OpenAI configuration
- `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_PRIVATE_KEY_PATH`: GitHub App credentials
- `GITHUB_TARGET_LABELS`: Comma-separated labels to process (default: `good first issue,bug,agent-fixable`)
- `WORKSPACE_ROOT`: Temp directory for repo clones (default: `/tmp/codecairn`)
- `DOCKER_BIND_HOST_ROOT`, `DOCKER_BIND_CONTAINER_ROOT`: Path mapping for sandbox Docker mounts
- `max_attempts`: Max patch→test→retry cycles (default: 3)

### Docker Compose
- Exposes Docker socket to containers for sandbox execution
- `DOCKER_BIND_HOST_ROOT: ${PWD}` maps host project dir into container
- Worker calls host Docker daemon to run sandbox containers
