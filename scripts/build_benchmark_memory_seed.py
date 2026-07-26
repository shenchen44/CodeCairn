from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.swe_alignment.data import write_jsonl  # noqa: E402
from experiments.swe_alignment.memory import (  # noqa: E402
    build_verified_memory_seed,
)


def _read_jsonl(paths: list[str]) -> list[dict]:
    rows = []
    for value in paths:
        path = Path(value)
        rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a leak-resistant benchmark memory seed",
    )
    parser.add_argument("--evaluated-rollouts", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    memories = build_verified_memory_seed(
        _read_jsonl(args.evaluated_rollouts),
    )
    write_jsonl(args.output, memories)
    print(
        json.dumps(
            {
                "verified_memories": len(memories),
                "output": args.output,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
