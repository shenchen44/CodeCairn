import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codecairn.cli import main as cairn_main  # noqa: E402


def main() -> int:
    return cairn_main(["run", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
