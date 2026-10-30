"""Semillas por EAN y relectura de lo seguido (spec de Mercado Libre §1.3), contra Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from discountz.infra import db as repo
from discountz.infra.db import EanSeed, TrackedItem

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
ML = "mercadolibre"
CARDS = ("tecnologia", "electro", "hogar")
MIN = 100_000
A, B, C, D, E, F = (f"77000000000{i:02d}" for i in range(1, 7))


def _item(
    conn: psycopg.Connection,
    source: str,
    external_id: str,
    *,
    ean: str | None,
    topic: str = "tecnologia",
    currency: str = "COP",
    price: int = 1_000_000,
    seen: datetime = NOW,
    checked: datetime | None = None,
) -> None:
    conn.execute(
        """INSERT INTO discountz.catalog_item (source, external_id, ean, topic, currency,
               price_minor, first_seen_at, stable_since, last_seen_at, checked_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (source, external_id, ean, topic, currency, price, seen, seen, seen, checked),
    )


def _seeds(conn: psycopg.Connection, limit: int = 10) -> list[EanSeed]:
    return repo.ean_seeds(conn, ML, CARDS, MIN, limit)


def test_ean_seeds_take_card_topics_cop_and_min_price(db: psycopg.Connection) -> None:
    _item(db, "exito", "1", ean=A, price=2_000_000)
    _item(db, "exito", "2", ean=B, topic="belleza", price=2_000_000)  # tema apagado
    _item(db, "alkosto", "3", ean=C, price=50_000)  # bajo el mínimo
    _item(db, "ebay_us", "4", ean=D, currency="USD", price=99_999)  # en dólares
    _item(db, "exito", "5", ean=None)
    assert _seeds(db) == [EanSeed(A, "tecnologia")]


def test_ean_seeds_skip_tracked_and_looked_up(db: psycopg.Connection) -> None:
    for i, ean in enumerate((A, B, C)):
        _item(db, "exito", str(i), ean=ean)
    _item(db, ML, "MCO1", ean=A)  # ya seguido
    repo.record_ean_lookups(db, ML, [(B, None)], NOW)  # ya buscado
    assert _seeds(db) == [EanSeed(C, "tecnologia")]


def test_ean_seeds_use_most_recent_row_and_price_desc(db: psycopg.Connection) -> None:
    _item(db, "exito", "1", ean=E, topic="hogar", price=300_000, seen=NOW - 24 * HOUR)
    _item(db, "jumbo", "2", ean=E, topic="electro", price=400_000, seen=NOW)
    _item(db, "alkosto", "3", ean=F, price=900_000)
    assert _seeds(db) == [EanSeed(F, "tecnologia"), EanSeed(E, "electro")]
    assert _seeds(db, limit=1) == [EanSeed(F, "tecnologia")]


def test_record_ean_lookups_upserts(db: psycopg.Connection) -> None:
    repo.record_ean_lookups(db, ML, [(A, None), (B, "MCO1")], NOW)
    repo.record_ean_lookups(db, ML, [(A, "MCO2")], NOW + HOUR)
    rows = db.execute(
        "SELECT ean, checked_at, external_id FROM discountz.ean_lookup ORDER BY ean"
    ).fetchall()
    assert rows == [(A, NOW + HOUR, "MCO2"), (B, NOW, "MCO1")]


def test_catalog_refresh_items_oldest_check_first(db: psycopg.Connection) -> None:
    _item(db, ML, "MCO1", ean=A, seen=NOW - 9 * HOUR, checked=NOW - 8 * HOUR)
    _item(db, ML, "MCO2", ean=B, topic="hogar", seen=NOW - 7 * HOUR)  # nunca pedido aparte
    _item(db, ML, "MCO3", ean=C, seen=NOW - 3 * HOUR, checked=NOW - 2 * HOUR)  # reciente
    _item(db, ML, "MCO4", ean=D, topic="belleza", seen=NOW - 10 * HOUR)  # tema apagado
    _item(db, "exito", "5", ean=E, seen=NOW - 10 * HOUR)  # otra fuente
    before = NOW - 6 * HOUR
    assert repo.catalog_refresh_items(db, ML, before, 10, skip_topics=("belleza",)) == [
        TrackedItem("MCO1", A, "tecnologia"),
        TrackedItem("MCO2", B, "hogar"),
    ]
    assert repo.catalog_refresh_items(db, ML, before, 1) == [TrackedItem("MCO4", D, "belleza")]


def test_mark_catalog_checked_keeps_last_seen_at(db: psycopg.Connection) -> None:
    _item(db, ML, "MCO1", ean=A, seen=NOW - 9 * HOUR)
    _item(db, ML, "MCO2", ean=B, seen=NOW - 9 * HOUR)
    repo.mark_catalog_checked(db, ML, ["MCO1"], NOW)
    rows = db.execute(
        """SELECT external_id, last_seen_at, checked_at FROM discountz.catalog_item
            ORDER BY external_id"""
    ).fetchall()
    assert rows == [("MCO1", NOW - 9 * HOUR, NOW), ("MCO2", NOW - 9 * HOUR, None)]
