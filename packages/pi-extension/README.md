# CodeCairn for Pi

This Pi package records coding events as CodeCairn Capture Events.
It adds:

- `cairn_decision`, a structured pre-mutation decision tool.
- A configurable edit/write gate (`CODECAIRN_GATE_MODE=block|warn|off`).
- `/cairn`, which opens the local CodeCairn review workspace.
- `/cairn status`, which reports captured decisions and inspected paths.

Install CodeCairn first so the `cairn` executable is on `PATH`, then add this
directory as a local Pi package:

```bash
pi install /absolute/path/to/codecairn/packages/pi-extension
```

The default gate mode is `block`, which requires an accepted `cairn_decision`
before every `edit` or `write`. Use `CODECAIRN_GATE_MODE=warn` only when
evaluating false blocks, or `CODECAIRN_GATE_MODE=off` to disable the gate.

If `cairn` is temporarily unavailable, the extension writes redacted events to
`~/.codecairn/spool/pi/events.jsonl`. Replay them later with:

```bash
cairn capture replay
```
