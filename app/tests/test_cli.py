import pytest

from codecairn import __version__
from codecairn.cli import build_parser, main


def test_run_command_parses_general_coding_task_options():
    args = build_parser().parse_args(
        [
            "run",
            "--repo",
            "/tmp/project",
            "--intent",
            "review",
            "--objective",
            "Review cache invalidation",
        ]
    )

    assert args.command == "run"
    assert args.intent == "review"
    assert args.variant == "full"


def test_version_uses_codecairn_brand(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"CodeCairn {__version__}"
