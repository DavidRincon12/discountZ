import re
import subprocess
import sys
from pathlib import Path

import pytest

from discountz.__main__ import EXIT_BLOCKED, EXIT_BUG, EXIT_CONFIG, EXIT_DB, EXIT_OK, main
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
    for cmd in ("run", "migrate", "check-config", "record-fixture", "setup-bot"):
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
    assert "DISCOUNTZ_EBAY_APP_ID: ${{ secrets.EBAY_APP_ID }}" in text
    assert "DISCOUNTZ_EBAY_CERT_ID: ${{ secrets.EBAY_CERT_ID }}" in text
    assert "timeout-minutes: 55" in text


def test_cycle_workflow_passes_ebay_credentials_and_probe_is_gone() -> None:
    text = WORKFLOWS.joinpath("cycle.yml").read_text(encoding="utf-8")
    assert "DISCOUNTZ_EBAY_APP_ID: ${{ secrets.EBAY_APP_ID }}" in text
    assert "DISCOUNTZ_EBAY_CERT_ID: ${{ secrets.EBAY_CERT_ID }}" in text
    assert not WORKFLOWS.joinpath("ebay-probe.yml").exists()


def test_workflows_pass_meli_credentials() -> None:
    for name in ("crawl.yml", "cycle.yml"):
        text = WORKFLOWS.joinpath(name).read_text(encoding="utf-8")
        assert "DISCOUNTZ_MELI_CLIENT_ID: ${{ secrets.MELI_CLIENT_ID }}" in text
        assert "DISCOUNTZ_MELI_CLIENT_SECRET: ${{ secrets.MELI_CLIENT_SECRET }}" in text


def test_crawl_meli_without_credentials_is_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    for name in ("DISCOUNTZ_MELI_CLIENT_ID", "DISCOUNTZ_MELI_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert main(["crawl", "--source", "mercadolibre"]) == EXIT_OK  # la BD es inalcanzable
    assert "sin credenciales" in capsys.readouterr().out


def test_exit_codes_are_distinct() -> None:
    """En Actions, una tienda bloqueada no debe leerse como la BD caída (spec v1 §1.4)."""
    codes = (EXIT_OK, EXIT_BUG, EXIT_CONFIG, EXIT_DB, EXIT_BLOCKED)
    assert len(set(codes)) == len(codes)
