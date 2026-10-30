"""Semillas por EAN y relectura de lo seguido (spec de Mercado Libre §1.3), contra Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from discountz.infra import db as repo
from discountz.infra.db import EanSeed, SeedMark, SeedWindow, TrackedItem

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


def _window(
    conn: psycopg.Connection,
    after: SeedMark | None = None,
    limit: int = 10,
    min_minor: int = MIN,
) -> SeedWindow:
    return repo.ean_seed_window(conn, ML, CARDS, min_minor, after, limit)


def _mark(conn: psycopg.Connection, source: str, external_id: str) -> SeedMark:
    row = conn.execute(
        """SELECT price_minor, id FROM discountz.catalog_item
            WHERE source = %s AND external_id = %s""",
        (source, external_id),
    ).fetchone()
    assert row is not None
    return SeedMark(row[0], row[1])


def _seed_index_scans(conn: psycopg.Connection) -> int:
    conn.execute("SELECT pg_stat_force_next_flush()")
    row = conn.execute(
        """SELECT idx_scan FROM pg_stat_user_indexes
            WHERE indexrelname = 'catalog_item_seed_ix'"""
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_ean_seed_window_takes_card_topics_cop_and_min_price(db: psycopg.Connection) -> None:
    _item(db, "exito", "1", ean=A, price=2_000_000)
    _item(db, "exito", "2", ean=B, topic="belleza", price=2_000_000)  # tema apagado
    _item(db, "alkosto", "3", ean=C, price=50_000)  # bajo el mínimo
    _item(db, "ebay_us", "4", ean=D, currency="USD", price=99_999)  # en dólares
    _item(db, "exito", "5", ean=None)
    assert _window(db) == SeedWindow((EanSeed(A, "tecnologia"),), None)


def test_ean_seed_window_skips_tracked_and_looked_up(db: psycopg.Connection) -> None:
    for i, ean in enumerate((A, B, C)):
        _item(db, "exito", str(i), ean=ean)
    _item(db, ML, "MCO1", ean=A)  # ya seguido
    repo.record_ean_lookups(db, ML, [(B, None)], NOW)  # ya buscado
    assert _window(db).seeds == (EanSeed(C, "tecnologia"),)


def test_ean_seed_window_keeps_the_most_expensive_row_of_each_ean(
    db: psycopg.Connection,
) -> None:
    _item(db, "exito", "1", ean=E, topic="hogar", price=500_000, seen=NOW - 24 * HOUR)
    _item(db, "jumbo", "2", ean=E, topic="electro", price=400_000, seen=NOW)
    _item(db, "alkosto", "3", ean=F, price=900_000)
    assert _window(db).seeds == (EanSeed(F, "tecnologia"), EanSeed(E, "hogar"))


def test_ean_seed_window_goes_on_after_the_mark(db: psycopg.Connection) -> None:
    for i, ean in enumerate((A, B, C, D)):
        _item(db, "exito", str(i), ean=ean, price=2_000_000 - i * 100_000)
    first = _window(db, limit=2)
    assert first == SeedWindow(
        (EanSeed(A, "tecnologia"), EanSeed(B, "tecnologia")), _mark(db, "exito", "1")
    )
    second = _window(db, first.after, limit=2)
    assert second == SeedWindow(
        (EanSeed(C, "tecnologia"), EanSeed(D, "tecnologia")), _mark(db, "exito", "3")
    )
    assert _window(db, second.after, limit=2) == SeedWindow((), None)  # se acabó el catálogo


def test_ean_seed_window_breaks_price_ties_by_id(db: psycopg.Connection) -> None:
    for i, ean in enumerate((A, B, C)):
        _item(db, "exito", str(i), ean=ean)  # mismo precio: manda el id, de mayor a menor
    after: SeedMark | None = None
    for ext, ean in (("2", C), ("1", B), ("0", A)):
        window = _window(db, after, limit=1)
        assert window == SeedWindow((EanSeed(ean, "tecnologia"),), _mark(db, "exito", ext))
        after = window.after
    assert _window(db, after, limit=1) == SeedWindow((), None)


def test_ean_seed_window_counts_skipped_rows(db: psycopg.Connection) -> None:
    for i, ean in enumerate((A, B, C)):
        _item(db, "exito", str(i), ean=ean, price=2_000_000 - i * 100_000)
    repo.record_ean_lookups(db, ML, [(A, None), (B, None)], NOW)
    assert _window(db, limit=2) == SeedWindow((), _mark(db, "exito", "1"))


def test_ean_seed_window_uses_the_seed_index_below_its_floor(db: psycopg.Connection) -> None:
    _item(db, "exito", "1", ean=A, price=2_000_000)
    _item(db, "exito", "2", ean=B, price=80_000)  # bajo el piso del índice
    db.execute("SET enable_seqscan = off")  # en una tabla chica el planificador la preferiría
    before = _seed_index_scans(db)
    assert _window(db, min_minor=50_000).seeds == (EanSeed(A, "tecnologia"),)
    assert _seed_index_scans(db) > before


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
