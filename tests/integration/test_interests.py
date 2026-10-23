"""Intereses activos: la semilla y luego los personales (spec del perfil §3.1)."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from discountz import __main__ as cli
from discountz.app.interests import active_interests
from discountz.config import load_interests
from discountz.domain.interests import match_interest
from discountz.infra import db as repo
from discountz.infra.migrate import migrate

pytestmark = pytest.mark.integration

T = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)


def test_active_interests_are_the_seed_then_personal_by_id(db: psycopg.Connection) -> None:
    cfg = load_interests()
    welder = repo.add_interest(db, "Soldador", "ferreteria", ["soldador"], T)
    gpu = repo.add_interest(db, "RTX 5070", "tecnologia", ["rtx 5070"], T)
    found = active_interests(db, cfg)
    assert found[: len(cfg.seeds)] == cfg.seeds
    personal = [(i.name, i.personal_id) for i in found[len(cfg.seeds) :]]
    assert personal == [("Soldador", welder), ("RTX 5070", gpu)]
    hit = match_interest("Soldador de punta fina", found)
    assert hit is not None and hit.personal_id == welder


def test_crawl_reads_no_interests_on_an_old_schema(db: psycopg.Connection) -> None:
    """Desplegado antes de migrar: la corrida termina en `skipped_schema` en vez de fallar."""
    cfg = load_interests()
    repo.add_interest(db, "Soldador", "ferreteria", ["soldador"], T)
    assert len(cli._crawl_interests(db, cfg)) == len(cfg.seeds) + 1
    migrate(db, 11)
    assert cli._crawl_interests(db, cfg) == ()
