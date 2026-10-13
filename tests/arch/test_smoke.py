import subprocess
import sys


def test_cli_help_lists_subcommands() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "discountz", "--help"], capture_output=True, text=True, check=True
    ).stdout
    for cmd in ("run", "migrate", "check-config", "record-fixture"):
        assert cmd in out
