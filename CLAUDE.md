# CodeCairn Development Guide

## Commands

```bash
uv sync
uv run pytest -q
uv run cairn --version
npm --prefix packages/pi-extension run check
```

## Product Boundary

CodeCairn is an evidence and review layer around established coding agents. Pi
owns model interaction and code mutation. CodeCairn owns:

- host-neutral event capture and secret redaction;
- pre-mutation Decision Records and mutation coverage gates;
- Change Proof compilation and Evidence Ledger integrity;
- side-by-side Review Workspace and evidence graph exports;
- sandboxed verification and CI trust assessment;
- GitHub PR description, comment, and check publication.

Do not add a second interactive coding runtime, model loop, issue worker,
database service, or benchmark harness to the production package.

## Package Layout

- `codecairn/review`: analysis, models, ledger, UI, export, and CI trust
- `codecairn/verification`: repository policy and sandbox runner
- `codecairn/github`: authentication and publication
- `packages/pi-extension`: Pi lifecycle adapter and mutation gate
- `tests`: product-focused tests

Capture and review modules use stable domain names. Schema migrations may
recognize historical data, but new module, command, and UI names must not carry
temporary version suffixes.
