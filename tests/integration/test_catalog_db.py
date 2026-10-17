"""Repositorio del catálogo, cursores, temas y outbox con tarjeta (migración 0002)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from discountz.domain.catalog import CatalogState
from discountz.infra import db as repo
from discountz.infra.db import CatalogRow, CrawlCursor

pytestmark = pytest.mark.integration
T = datetime(2026, 10, 16, 12, tzinfo=UTC)


def state(price: int, *, last_changed: datetime | None = None, reads: int = 1) -> CatalogState:
    return CatalogState(price, None, None, T, T, reads, last_changed, None, None)


def row(
    ext: str,
    price: int,
    *,
    changed: bool = True,
    ean: str | None = None,
    last_changed: datetime | None = None,
    reads: int = 1,
) -> CatalogRow:
    state_ = state(price, last_changed=last_changed, reads=reads)
    return CatalogRow(ext, ean, "tecnologia", "COP", state_, changed)


def count(db: psycopg.Connection, table: str) -> int:
    r = db.execute(f"SELECT count(*) FROM discountz.{table}").fetchone()  # noqa: S608
    assert r is not None
    return int(r[0])


def test_save_page_upserts_and_logs_only_changes(db: psycopg.Connection) -> None:
    repo.save_catalog_page(db, "exito", [row("1", 99_900), row("2", 50_000)], T)
    repo.save_catalog_page(
        db,
        "exito",
        [row("1", 99_900, changed=False, reads=2), row("2", 45_000, changed=True)],
        T + timedelta(hours=1),
    )
    states = repo.load_catalog_states(db, "exito", ["1", "2", "3"])
    assert set(states) == {"1", "2"}
    assert states["1"].reads_at_price == 2
    assert states["2"].price_minor == 45_000
    assert count(db, "catalog_item") == 2
    assert count(db, "catalog_price_change") == 3
    seen = db.execute(
        "SELECT last_seen_at FROM discountz.catalog_item WHERE external_id = '1'"
    ).fetchone()
    assert seen == (T + timedelta(hours=1),)


def test_hot_ids_ordered_by_recent_change(db: psycopg.Connection) -> None:
    repo.save_catalog_page(
        db,
        "exito",
        [
            row("1", 10_000, last_changed=T - timedelta(days=1)),
            row("2", 20_000, last_changed=T - timedelta(hours=1)),
            row("3", 30_000, last_changed=T - timedelta(days=9)),
            row("4", 40_000),
        ],
        T,
    )
    assert repo.hot_catalog_ids(db, "exito", T - timedelta(days=7), 10) == ["2", "1"]
    assert repo.hot_catalog_ids(db, "exito", T - timedelta(days=7), 1) == ["2"]


def test_cursor_roundtrip(db: psycopg.Connection) -> None:
    empty = repo.get_cursor(db, "exito")
    assert empty == CrawlCursor("exito", 0, 0, 0, None, None, None, None)
    assert (empty.partitions, empty.day, empty.day_requests) == ((), None, 0)
    saved = CrawlCursor(
        "exito",
        2,
        5,
        7,
        T,
        3600,
        T,
        T - timedelta(hours=1),
        partitions=("C:/1/", "C:/2/|P:[1 TO 99]"),
        day=T.date(),
        day_requests=321,
    )
    repo.save_cursor(db, saved)
    assert repo.get_cursor(db, "exito") == saved


def test_crawl_lease_is_exclusive_per_source(db: psycopg.Connection) -> None:
    ttl = timedelta(minutes=60)
    assert repo.acquire_crawl_lease(db, "exito", "a", T, ttl)
    assert not repo.acquire_crawl_lease(db, "exito", "b", T, ttl)
    assert repo.acquire_crawl_lease(db, "carulla", "b", T, ttl)
    assert repo.acquire_crawl_lease(db, "exito", "b", T + ttl + timedelta(seconds=1), ttl)
    repo.release_crawl_lease(db, "exito", "b")
    assert repo.acquire_crawl_lease(db, "exito", "c", T, ttl)


def test_recent_alert_for_ean_tolerance(db: psycopg.Connection) -> None:
    repo.save_catalog_page(db, "exito", [row("1", 100_000, ean="7701234567890")], T)
    repo.mark_catalog_alert(db, "exito", "1", 100_000, T)
    since = T - timedelta(hours=6)
    ean = "7701234567890"
    assert repo.recent_alert_for_ean(db, ean, "COP", 99_000, since, exclude_source="carulla")
    assert not repo.recent_alert_for_ean(db, ean, "COP", 98_000, since, exclude_source="carulla")
    assert not repo.recent_alert_for_ean(db, ean, "COP", 99_000, since, exclude_source="exito")
    assert not repo.recent_alert_for_ean(
        db, ean, "COP", 99_000, T + timedelta(minutes=1), exclude_source="carulla"
    )
    s = repo.load_catalog_states(db, "exito", ["1"])["1"]
    assert (s.last_alert_at, s.last_alert_minor) == (T, 100_000)


def test_topics_roundtrip(db: psycopg.Connection) -> None:
    assert repo.topic_threads(db) == {}
    repo.save_topic(db, "tecnologia", "📱 Tecnología", 77)
    repo.save_topic(db, "tecnologia", "📱 Tecnología", 78)
    assert repo.topic_threads(db) == {"tecnologia": 78}


def test_enqueue_with_card_and_topic(db: psycopg.Connection) -> None:
    card = {"store": "exito", "price_minor": 59_900}
    repo.enqueue(
        db,
        "alert",
        "<b>x</b>",
        priority=2,
        dedup_key="cat:1",
        now=T,
        ttl=timedelta(hours=24),
        topic="tecnologia",
        card=card,
    )
    (claimed,) = repo.claim_outbox(db, 5, T)
    assert claimed.topic == "tecnologia"
    assert claimed.card == card


def test_watch_origin_defaults_manual(db: psycopg.Connection) -> None:
    db.execute(
        """INSERT INTO discountz.watch (source, pending_ref, next_check_at)
           VALUES ('exito', 'id:123', now())"""
    )
    assert db.execute("SELECT origin, expires_at FROM discountz.watch").fetchone() == (
        "manual",
        None,
    )


def test_alert_accepts_catalog_drop(db: psycopg.Connection) -> None:
    db.execute(
        """INSERT INTO discountz.listing (source, external_id, canonical_url, title,
               first_seen_at, last_seen_at, last_in_stock, last_seller_class)
           VALUES ('exito', '1', 'https://www.exito.com/x/p', 't', now(), now(), true, 'f')"""
    )
    db.execute(
        """INSERT INTO discountz.alert (listing_id, kind, maturity, price_minor, observed_at)
           SELECT id, 'catalog_drop', 'warm', 59900, now() FROM discountz.listing"""
    )
    assert count(db, "alert") == 1
