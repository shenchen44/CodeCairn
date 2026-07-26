# micro-swe-agent

The project is an embeddable, extensible general-purpose coding agent runtime.
Its core understands source-independent coding tasks, sessions, tools, models,
and lifecycle events. GitHub Issues, SWE-bench, interactive clients, and
automations connect through adapters. See
[`docs/general_coding_agent_runtime.md`](docs/general_coding_agent_runtime.md).

The included GitHub App is one product surface. The runtime also supports a
SWE-bench adapter, read-only review, investigation, explanation, persistent
multi-turn sessions, dynamic tools, and non-GitHub callers.

## Features

- **Tool-calling agent loop**: supports `list_files`, `search_code`, `read_file`, `write_file`, `apply_patch`, `run_tests`
- **General task contract**: change, review, investigate, and explain tasks share one core; only change tasks can mutate the workspace
- **Pi-style extension surface**: register and activate tools at runtime and intercept model/tool lifecycle events
- **Tree sessions**: JSONL persistence, context continuation, and forks from any history entry
- **Multi-language repository profiles**: Python, JavaScript/TypeScript, Rust, Go, and Java detection with sandboxed command allowlists
- **Sandbox guardrails**: restricts allowed paths, blocks high-risk commands, limits max changed files and diff lines
- **Observability**: records task-level and attempt-level latency, model call counts, tool call counts
- **Hybrid code retrieval**: BM25, AST symbols, and import graph results fused with RRF
- **Multi-agent runtime**: Supervisor-selected Localization, Planner, Patch, and Reviewer stages
- **Task and repository memory**: validated promotion, confidence, deduplication, and invalidation
- **Evidence gates and ledger**: typed hand-offs and traceable requirement-to-verification links
- **SWE-bench adapter**: instance loading, RuntimePolicy execution, and official `model_patch` export
- **Trajectory data tools**: convert structured rollouts into SFT records, DPO preference pairs, and summaries

## Recommended Usage

- Run a source-independent task directly on a local repository:

```bash
python scripts/run_coding_task.py \
  --repo /path/to/repository \
  --intent review \
  --objective "Review cache concurrency and invalidation" \
  --session-file .agent/sessions/review.jsonl
```

- Run the Runtime on a local machine or server
- Configure a GitHub App and model API
- Process authorized GitHub Issues, interactive tasks, or SWE-bench instances

## Directory Structure

```text
./
  app/
    api/routes/
    core/
    db/
      migrations/
      models/
    schemas/
    services/
      comments/
      github/
      openai/
      sandbox/
      task_runner/
    tests/
      fixtures/toy_repo/
    workers/
    main.py
  experiments/
    swe_alignment/
  docs/
    swe_alignment.md
  secrets/
  .workspaces/
  .env.example
  alembic.ini
  docker-compose.yml
  Dockerfile
  Makefile
  pytest.ini
  README.md
  requirements.txt
```

## Architecture

### API Process

- `POST /webhooks/github`: verifies GitHub webhook signature, filters issues, persists repository / issue / task / raw webhook artifact
- `GET /health`: health check
- `GET /tasks`: paginated task list
- `GET /tasks/{task_id}`: task details, attempt records, artifacts
- `POST /tasks/{task_id}/rerun`: manually re-queue task
- `GET /repositories`: list of tracked repositories
- `GET /dashboard`: PR review / merge / integration dashboard
- `GET /dashboard/prs`: dashboard data API
- `POST /dashboard/prs/{task_id}/merge`: merge a mergeable PR
- `POST /dashboard/prs/{task_id}/resolve-conflict`: create conflict resolution task for conflicting PR
- `POST /dashboard/integrations`: create integration task from multiple PRs

### Worker Process

- Polls tasks in `triaged` state
- Exchanges GitHub App JWT for installation token
- Clones repository to an isolated temp directory
- Creates a new branch based on the default branch
- Reads `.agent.yml` or safe default config
- Runs install commands
- Uses the Supervisor to select a standard or deep multi-agent execution graph
- Runs read-only localization and evidence gates before workspace mutation
- Uses BM25, AST, and import-graph hybrid retrieval
- Runs Planner and independent Reviewer stages when selected
- Executes `list_files`, `search_code`, `read_file`, `apply_patch`, `git_diff`, and `run_tests`
- Recalls task memory during retries and promotes validated repository memory
- Up to 3 rounds of patch → test → retry
- On success: commit / push / create PR / issue comment
- On failure: records failure reason and test log, comments on issue
- Supports integration tasks
- Supports conflict resolution tasks

### State Machine

```text
received -> triaged -> sandbox_ready -> patching -> testing -> retrying -> patching
testing -> ready_for_pr -> pr_opened -> done
* -> failed
```

### Security Guardrails

- Only modifies files in `allowed_paths`
- Rejects changes to `blocked_paths`
- Immediately fails if file count or diff line count exceeds limit
- Intercepts dangerous commands in install/test commands
- All changes are made on a new branch
- PRs default to `needs-human-review` label

## Environment Variables

See `.env.example`:

- `APP_ENV`
- `DATABASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `GITHUB_APP_ID`
- `GITHUB_WEBHOOK_SECRET`
- `GITHUB_PRIVATE_KEY_PATH`
- `GITHUB_TARGET_LABELS`
- `WORKER_POLL_INTERVAL`
- `SANDBOX_BASE_IMAGE`
- `SANDBOX_TIMEOUT_SECONDS`
- `SANDBOX_MEMORY_LIMIT`
- `SANDBOX_CPU_LIMIT`
- `WORKSPACE_ROOT`
- `DOCKER_BIND_HOST_ROOT`
- `DOCKER_BIND_CONTAINER_ROOT`
- `LOG_LEVEL`

### Key Environment Variables

- `GITHUB_PRIVATE_KEY_PATH`
  - In Docker Compose mode, use the in-container path, e.g.:
    `GITHUB_PRIVATE_KEY_PATH=/app/secrets/your-app.private-key.pem`
  - In local Python mode, use the host absolute path, e.g.:
    `/Users/yourname/.../secrets/your-app.private-key.pem`
- `WORKSPACE_ROOT`
  - Docker Compose mode: recommended `/app/.workspaces`
  - Local mode: use default or set to a local directory
- `DOCKER_BIND_CONTAINER_ROOT` / `DOCKER_BIND_HOST_ROOT`
  - Map in-container workspace path back to host real path in Docker Compose mode
  - `docker-compose.yml` already injects these automatically, usually no manual changes needed

## Recommended Running Mode

Docker Compose is recommended for most users, as it most closely mirrors the experience of "cloning and self-hosting".

### Mode Comparison

- **Docker Compose mode**
  - Recommended for most users
  - API / worker / postgres all run in containers
  - Requires Docker Desktop or Docker Engine
  - `GITHUB_PRIVATE_KEY_PATH` should use the in-container path
- **Local Python mode**
  - Good for development and debugging
  - API / worker run in host Python environment
  - Still requires local Docker available
  - `GITHUB_PRIVATE_KEY_PATH` must be the host real path

## Local Development Setup

### Option 1: Docker Compose

1. **Prepare GitHub App private key file**

Place the downloaded `.pem` file at:

```text
secrets/<your-private-key>.pem
```

2. **Copy environment file**

```bash
cp .env.example .env
```

3. **Fill in GitHub App and OpenAI config**

At minimum, confirm these values:

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
GITHUB_APP_ID=...
GITHUB_WEBHOOK_SECRET=...
GITHUB_PRIVATE_KEY_PATH=/app/secrets/<your-private-key>.pem
DATABASE_URL=sqlite:///./local.db
```

4. **Start services**

```bash
docker compose up --build
```

5. **Run migrations**

If this is your first time starting and the database file is brand new:

```bash
docker compose exec api alembic upgrade head
```

If you already have an old `local.db` and migration fails with `table ... already exists`, use:

```bash
docker compose exec api alembic stamp head
```

Default services:

- API: `http://localhost:8000`
- PostgreSQL: `localhost:5432`
- Dashboard: `http://localhost:8000/dashboard`

6. **Check container status**

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f worker
```

### Option 2: Local Python

1. **Install dependencies and copy environment file**

```bash
python -m pip install -r requirements.txt
cp .env.example .env
```

2. **Change `GITHUB_PRIVATE_KEY_PATH` to host real path**

For example:

```text
GITHUB_PRIVATE_KEY_PATH=/Users/yourname/Desktop/micro-swe-agent/secrets/<your-private-key>.pem
```

3. **Run migrations and start**

```bash
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

In another terminal:

```bash
python -m app.workers.poller
```

### Extra Requirements for Docker Compose Mode

The worker calls the host Docker daemon to run sandbox containers. The current `docker-compose.yml` already includes:

- Docker socket mount
- Workspace directory path mapping
- `DOCKER_BIND_*` environment variables

If you modify the project directory structure or running mode, ensure:

- Host Docker is running
- Containers can execute `docker`
- In-container workspace path maps to the host real project directory

## Creating a GitHub App

1. Create a GitHub App in GitHub Developer Settings
2. Grant permissions:
   - Repository permissions: `Contents: Read & write`, `Issues: Read & write`, `Pull requests: Read & write`, `Metadata: Read-only`
3. Subscribe to webhook events:
   - `Issues`
4. Record:
   - App ID
   - Webhook secret
   - Private key PEM file
5. Install the App on target repositories

### Recommended GitHub App Permissions

- Repository permissions
  - `Contents: Read & write`
  - `Issues: Read & write`
  - `Pull requests: Read & write`
  - `Metadata: Read-only`

If permissions are insufficient, common failures manifest as:

- Clone failure
- Push failure
- Create PR failure
- Merge failure

## Webhook Local Debugging

Use ngrok or GitHub App webhook delivery:

```bash
ngrok http 8000
```

Configure the public address as the GitHub App webhook URL, e.g.:

```text
https://<your-ngrok-subdomain>.ngrok.app/webhooks/github
```

For local debugging, you can also manually send a signed JSON payload to the endpoint.

## OpenAI API Configuration

Set in `.env`:

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
```

The current implementation uses the OpenAI Responses API and retains tool calling and trace/artifact structures for easy future extension.

## Worker and Docker Sandbox

The worker calls the host Docker to run install and test commands.

- On Linux/macOS development, mount `/var/run/docker.sock` to `api` / `worker` containers
- Docker Desktop on Windows can also expose the Docker socket, but ensure containers can access the host Docker Engine
- Each task runs in an isolated temp directory with the repo mounted to `/workspace`
- In Docker Compose mode, the workspace defaults to `/app/.workspaces` in the container, and the system automatically converts to the host real path before mounting to the sandbox container

## Dashboard

Open:

```text
http://localhost:8000/dashboard
```

Current dashboard features:

- View open PRs
- View root cause / change summary / diff
- View mergeability status
- Open GitHub PR directly
- Merge non-conflicting PRs
- Select multiple PRs to create integration task
- Trigger conflict resolution task for conflicting PRs
- Show superseded status when old PR is replaced by resolved PR

## FAQ

### 1. `/dashboard/prs` returns 500

Common causes:

- `GITHUB_PRIVATE_KEY_PATH` is wrong
- Docker mode uses host path
- Local Python mode uses in-container path

### 2. `docker` not found in worker

Confirm:

```bash
docker compose exec worker sh -lc 'command -v docker'
docker compose exec worker docker ps
```

### 3. resolve conflict reports mount denied

This is usually caused by in-container paths being passed directly to host Docker. This version already maps via `DOCKER_BIND_HOST_ROOT` / `DOCKER_BIND_CONTAINER_ROOT`; if you modified compose or directory structure, check whether these two variables are still correct.

### 4. Task stuck at `patching`

Check:

```bash
curl http://127.0.0.1:8000/tasks
docker compose logs -f worker
```

### 5. `rerun` returns `task_not_rerunnable`

Only tasks in `failed` or `done` state can be rerun.

## Running Tests

```bash
python -m pytest app/tests -q -p no:cacheprovider
```

Current test coverage:

- Webhook signature verification
- Issue filtering logic
- State machine transitions
- Repo config parsing
- Diff / blocked path limits
- Task deduplication
- PR body generation
- Toy repo minimal end-to-end loop
- Dashboard / integration / conflict resolution
- Sandbox path mapping and repo-local `.venv`

## Minimal End-to-End Demo

### Pure Local Loop

Run the toy repo scenario directly from tests:

```bash
python -m pytest app/tests/test_toy_repo_integration.py -q -p no:cacheprovider
```

This test:

- Initializes a minimal Python toy repo
- Simulates issue-corresponding patch
- Applies patch via tool interface
- Runs toy repo pytest
- Generates PR body

### GitHub App Real Machine Integration

1. Start `postgres`, `api`, `worker`
2. Create an issue with target label in the target repository
3. Ensure issue body is not empty
4. GitHub webhook arrives at `POST /webhooks/github`
5. Visit `GET /tasks` to view tasks
6. Visit `GET /tasks/{task_id}` to view attempts, diff, test log, PR info
