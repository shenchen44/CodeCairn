# General Coding Agent Runtime

## Goal

The runtime is not a GitHub Issue solver with extra entry points. It is a
source-independent coding harness that can be embedded in a CLI, IDE, API,
automation worker, or benchmark adapter.

The core contract is:

```text
CodingTask + Repository + Session + Active Tools + Model
                         |
                         v
              Agent lifecycle events
                         |
                         v
             Result + workspace state
```

GitHub Issue-to-PR and SWE-bench remain supported, but they are adapters and
delivery mechanisms rather than core runtime concepts.

## Pi-Inspired Principles

The design borrows four principles from Pi:

1. Keep the model/tool loop small and stable.
2. Register and activate tools at runtime instead of baking every workflow
   into the loop.
3. Expose lifecycle events so policy, permissions, observability, and custom
   workflows can be extensions.
4. Persist append-only session history and allow branches from prior entries.

This project deliberately retains evidence gates, sandbox limits, optional
multi-agent stages, and benchmark telemetry as extensions around that core.
Pi intentionally leaves many of those choices to users.

Primary references:

- https://pi.dev/
- https://pi.dev/docs/latest/extensions
- https://pi.dev/docs/latest/session-format
- https://github.com/earendil-works/pi/tree/main/packages/agent

## Core Contracts

### CodingTask

`CodingTask` contains:

- objective and description
- intent: `change`, `review`, `investigate`, or `explain`
- source and delivery target
- explicit requirements and acceptance criteria
- repository and task constraints
- prior context and adapter metadata

Only `change` tasks expose mutation tools and require a workspace diff.
Read-only task types bypass localization, patch recovery, and evidence gates
that only make sense for code changes.

### Tool Runtime

`AgentToolbox` provides built-in repository tools plus a dynamic
`ToolRegistry`. Callers can:

- register tools with capability metadata
- activate a subset of built-in and extension tools
- override the active profile for a task or phase
- prevent mutation tools from appearing on read-only tasks

Task constraints are enforced below the model at edit and diff-validation
time. Multi-file exact edits are transactional.

### Extensions

`ExtensionManager` emits:

- `before_model`
- `model_response`
- `tool_call`
- `tool_result`
- `run_end`

Extensions may update event payloads or block an action. This supports policy,
permissions, tracing, custom context injection, and task-specific behavior
without adding branches to the core loop.

### Sessions

`AgentSession` stores append-only JSONL entries. Entries point to parents, so a
session can continue linearly or fork from any previous entry. The same session
contract can be used by interactive, RPC, API, or automation clients.

## Repository Profiles

Without `.agent.yml`, the runtime detects common project markers:

| Marker | Language/profile | Default verification |
| --- | --- | --- |
| Python/default | Python | `pytest -q` |
| `package.json` | JavaScript/TypeScript | package-manager test |
| `Cargo.toml` | Rust | `cargo test` |
| `go.mod` | Go | `go test ./...` |
| `pom.xml` | Java | `mvn test` |

Python AST tools are only advertised for Python repositories. Other languages
use language-neutral file search, exact editing, Git history, and sandboxed
project commands. Language-server adapters can be added through the tool
registry later.

## Adaptive Progress

Mutation pressure is no longer tied to a benchmark-specific turn number.
For change tasks it activates when:

- tool calls stop producing new successful observations,
- an explicit policy deadline is configured, or
- the reserved implementation turns are reached.

It never activates for review, investigation, or explanation tasks. If the
evidence is insufficient, the model may return `blocked` instead of being
forced to guess.

## Adapters

Current adapters:

- `github_issue_task`: GitHub issue and integration metadata to `CodingTask`
- `swe_bench_task`: benchmark instance to patch-delivery `CodingTask`
- `interactive_task`: direct user/API task construction

Entry points create a `CodingTask` and avoid adding source checks inside the
agent loop.
