# CodeCairn

**[English](README-en.md) | [中文](README-zh.md)**

CodeCairn is a local-first evidence and review layer for AI-assisted code
changes. It integrates with Pi to capture implementation decisions, connect
them to the resulting diff, run bounded verification, and produce a reviewable
Change Proof.

```bash
python -m pip install -e .
pi install "$(pwd)/packages/pi-extension"
cd /path/to/repository
pi
```

Use `/cairn` in Pi after coding, or run `cairn review` directly in any Git
repository with local changes.

See the [English guide](README-en.md), [中文说明](README-zh.md), and
[product PRD](docs/codecairn_product_prd_zh.md).
