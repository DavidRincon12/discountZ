import pytest

from discountz.__main__ import build_parser, parse_duration


@pytest.mark.parametrize(("text", "seconds"), [("20m", 1200), ("90s", 90), ("1h", 3600), ("0m", 0)])
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


def test_migrate_arguments() -> None:
    args = build_parser().parse_args(["migrate", "--to", "head", "--wait-lease", "20m"])
    assert (args.to, args.wait_lease) == ("head", 1200)
    args = build_parser().parse_args(["migrate", "--to", "0"])
    assert (args.to, args.wait_lease) == (0, 0)


def test_migrate_rejects_bad_target() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["migrate", "--to", "ayer"])
