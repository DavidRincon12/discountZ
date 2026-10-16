import re
import subprocess
import sys
from pathlib import Path

import pytest

from discountz.__main__ import EXIT_CONFIG, main
from discountz.sources.base import Capability
from discountz.sources.registry import load_all

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
ENV = {
    "DISCOUNTZ_DATABASE_URL": "postgresql://nadie@127.0.0.1:1/x",
    "DISCOUNTZ_TELEGRAM_BOT_TOKEN": "1:x",
    "DISCOUNTZ_TELEGRAM_CHAT_ID": "1",
    "DISCOUNTZ_TELEGRAM_OWNER_ID": "1",
}


def test_cli_help_lists_subcommands() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "discountz", "--help"], capture_output=True, text=True, check=True
    ).stdout
    for cmd in ("run", "migrate", "check-config", "record-fixture"):
        assert cmd in out


def test_cli_help_lists_crawl() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "discountz", "crawl", "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "--source" in out
    assert "--minutes" in out


def test_crawl_unknown_source_exits_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    assert main(["crawl", "--source", "tienda_x"]) == EXIT_CONFIG
    assert main(["crawl", "--source", "slickdeals"]) == EXIT_CONFIG  # sin catálogo


def test_crawl_workflow_matrix_matches_catalog_sources() -> None:
    text = WORKFLOWS.joinpath("crawl.yml").read_text(encoding="utf-8")
    m = re.search(r"source: \[([^\]]*)\]", text)
    assert m is not None
    matrix = {s.strip() for s in m.group(1).split(",")}
    catalog = {
        code for code, s in load_all().items() if Capability.CATALOG in s.descriptor.capabilities
    }
    assert matrix == catalog
    assert "vars.DISCOUNTZ_RASTREO == 'true'" in text
    assert "BESTBUY_API_KEY" in text
    assert "timeout-minutes: 55" in text
