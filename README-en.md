# CodeCairn

CodeCairn is a local-first evidence and review layer for AI-assisted code
changes. Pi provides the interactive coding runtime; CodeCairn captures
implementation decisions, connects them to the resulting diff, runs bounded
verification, and prepares a reviewable Change Proof.

CodeCairn records explicit rationale and repository evidence. It does not
capture or expose a model's hidden chain of thought.

## Install

```bash
python -m pip install -e .
pi install "$(pwd)/packages/pi-extension"
```

Start Pi inside the repository you want to change. Before `edit` or `write`,
the extension's `cairn_decision` tool records the summary, rationale,
alternatives, affected paths, source evidence, risks, and verification plan.

```bash
cd /path/to/repository
pi
```

After coding, enter `/cairn` in Pi. CodeCairn opens its browser review
workspace immediately and streams changed files into the sidebar as analysis
completes.

## Review Workspace

The workspace provides:

- aligned before/after source with highlighted diff rows;
- file and hunk navigation;
- implementation decisions linked to claims and repository evidence;
- requirement mappings, residual risks, and provenance labels;
- local verification in a restricted Docker environment;
- stale-state and integrity checks;
- Markdown, JSON, HTML, SVG, and PNG export;
- GitHub PR description, comment, and check publishing.

Run it directly when a change already exists:

```bash
cairn review --base main --requirement "Empty input returns zero"
```

Headless exports use the same Change Proof:

```bash
cairn review --base main --format json
cairn review --base main --format markdown
cairn review --base main --format html --output change-proof.html
```

## Capture

Agent adapters submit host-neutral events through one stable CLI:

```bash
cairn capture ingest --host pi --repo .
cairn capture sessions --repo .
cairn capture show SESSION_ID --repo .
cairn capture replay --repo .
```

Events are redacted, appended idempotently, and linked with SHA-256 hashes
under `~/.codecairn/captures/`. The Pi extension writes a redacted local spool
when the collector is unavailable.

## Architecture

```text
Pi extension
  -> CaptureEvent + DecisionRecord
  -> Change Proof compiler
  -> Review Workspace + Evidence Graph
  -> Sandbox verification
  -> GitHub delivery
```

The product package is intentionally small:

```text
codecairn/
  review/         change analysis, evidence, UI, export, CI trust
  verification/   repository policy and sandbox execution
  github/         authentication and PR publication
  cli.py           local command surface
packages/
  pi-extension/   Pi lifecycle adapter and mutation gate
tests/             product-focused reliability tests
```

See [the product PRD](docs/codecairn_product_prd_zh.md) and
[Pi extension guide](docs/pi_extension.md).
