from __future__ import annotations

import argparse
import json
from pathlib import Path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Select SWE-bench retry targets from an alignment summary")
    parser.add_argument("--instances", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--buckets",
        nargs="+",
        default=["no_patch", "local_patch_apply_failed", "official_error"],
        help="summary.ids buckets to include",
    )
    args = parser.parse_args()

    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    target_ids: set[str] = set()
    for bucket in args.buckets:
        target_ids.update(str(item) for item in summary.get("ids", {}).get(bucket, []))

    selected = []
    for row in _read_jsonl(Path(args.instances)):
        if str(row.get("instance_id")) in target_ids:
            selected.append(row)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "targets": len(selected),
                "ids": [row["instance_id"] for row in selected],
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
