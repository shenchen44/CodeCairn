from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DATASET = "princeton-nlp/SWE-bench_Lite"
DATASET_SERVER_ROWS_URL = "https://datasets-server.huggingface.co/rows"


def _fetch_page(*, split: str, offset: int, length: int, config: str = "default", retries: int = 3) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "dataset": DATASET,
            "config": config,
            "split": split,
            "offset": offset,
            "length": length,
        }
    )
    url = f"{DATASET_SERVER_ROWS_URL}?{query}"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - network-dependent
            last_error = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"failed_to_fetch_huggingface_rows: {url}: {last_error}")


def fetch_rows(*, split: str, limit: int | None, page_size: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        remaining = None if limit is None else max(limit - len(rows), 0)
        if remaining == 0:
            break
        length = page_size if remaining is None else min(page_size, remaining)
        payload = _fetch_page(split=split, offset=offset, length=length)
        page_rows = [item["row"] for item in payload.get("rows", [])]
        rows.extend(page_rows)
        offset += len(page_rows)
        num_rows_total = payload.get("num_rows_total")
        if not page_rows or (num_rows_total is not None and offset >= int(num_rows_total)):
            break
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch SWE-bench Lite from Hugging Face without extra dependencies")
    parser.add_argument("--split", choices=["dev", "test"], default="test")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--output", default="data/swe/swebench_lite_test.jsonl")
    args = parser.parse_args()

    rows = fetch_rows(split=args.split, limit=args.limit, page_size=args.page_size)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"dataset": DATASET, "split": args.split, "rows": len(rows), "output": str(output)}, indent=2))


if __name__ == "__main__":
    main()
