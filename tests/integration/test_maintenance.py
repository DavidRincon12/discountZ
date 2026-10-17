"""Mantenimiento diario, canarios y resumen diario contra Postgres real (Task 20)."""

from __future__ import annotations

import random
from collections import deque
from datetime import date, timedelta

import psycopg
import pytest

from discountz.app.collect import Collector, SourceRun
from discountz.app.crawl import run_crawl
from discountz.app.maintenance import (
    MB,
    daily_summary,
    maintenance_due,
    run_maintenance,
    summary_due,
)
from discountz.app.render import render_daily
from discountz.config import Canary, load_params
from discountz.domain.health import BreakerState
from discountz.domain.scheduling import Task
from discountz.infra import db as repo
from discountz.infra.http import FakeHttp, PacedClient
from discountz.sources.fixtures import load_fixture
from discountz.sources.vtex import STORES, VtexSource
from tests.factories import offer
from tests.integration.test_crawl import Clock, FakeCatalog, deps, page_of
from tests.unit.series import T

pytestmark = pytest.mark.integration
DAY = timedelta(days=1)


def _listing(conn: psycopg.Connection, sku: str = "123456") -> int:
    return repo.upsert_listing(
        conn, offer(external_id=sku), "otros", "https://www.exito.com/x/p", T
    ).id


def _segment(
    conn: psycopg.Connection,
    lid: int,
    start: timedelta,
    end: timedelta | None,
    price: int,
    status: str = "o",
) -> None:
    started = T - start
    ended = None if end is None else T - end
    last = (ended or T) - timedelta(minutes=30)
    conn.execute(
        """INSERT INTO discountz.price_segment (listing_id, started_at, last_seen_at, ended_at,
               price_minor, in_stock, seller_class, unit_key, status, sample_count)
           VALUES (%s, %s, %s, %s, %s, true, 'f', 'un|1', %s, 10)""",
        (lid, started, last, ended, price, status),
    )


def test_old_segments_are_deleted_and_alltime_min_kept(db: psycopg.Connection) -> None:
    lid = _listing(db)
    _segment(db, lid, 420 * DAY, 410 * DAY, 80_000)
    _segment(db, lid, 410 * DAY, 405 * DAY, 50_000, status="g")  # error: no cuenta para el mínimo
    _segment(db, lid, 405 * DAY, None, 100_000)
    report = run_maintenance(db, T, load_params(), db_bytes=10 * MB)
    assert report.segments_deleted == 2
    row = db.execute(
        "SELECT alltime_min_minor FROM discountz.listing WHERE id = %s", (lid,)
    ).fetchone()
    assert row == (80_000,)
    left = db.execute("SELECT count(*) FROM discountz.price_segment").fetchone()
    assert left == (1,)


def test_glitch_older_than_7_days_becomes_open(db: psycopg.Connection) -> None:
    lid = _listing(db)
    _segment(db, lid, 30 * DAY, 8 * DAY, 100_000)
    _segment(db, lid, 8 * DAY, None, 10_000, status="g")  # U37: dura una semana ⇒ es el precio real
    other = _listing(db, "999")
    _segment(db, other, 3 * DAY, None, 10_000, status="g")
    report = run_maintenance(db, T, load_params(), db_bytes=10 * MB)
    assert report.glitches_reclassified == 1
    rows = db.execute(
        "SELECT listing_id, status FROM discountz.price_segment WHERE ended_at IS NULL ORDER BY 1"
    ).fetchall()
    assert rows == [(lid, "o"), (other, "g")]


def test_size_guard_pauses_discovery(db: psycopg.Connection) -> None:
    report = run_maintenance(db, T, load_params(), db_bytes=455 * MB)
    assert report.discovery_paused
    paused = db.execute("SELECT discovery_paused FROM discountz.bot_state").fetchone()
    assert paused == (True,)
    urgent = db.execute(
        "SELECT body_html FROM discountz.outbox WHERE purpose = 'system'"
    ).fetchall()
    assert any("450" in r[0] for r in urgent)
    run_maintenance(db, T + DAY, load_params(), db_bytes=100 * MB)
    paused = db.execute("SELECT discovery_paused FROM discountz.bot_state").fetchone()
    assert paused == (False,)


def test_prune_at_400_mb_drops_stale_discoveries(db: psycopg.Connection) -> None:
    stale = _listing(db, "111")
    db.execute(
        "UPDATE discountz.listing SET last_seen_at = %s, first_seen_at = %s WHERE id = %s",
        (T - 90 * DAY, T - 100 * DAY, stale),
    )
    followed = _listing(db, "222")
    db.execute(
        "UPDATE discountz.listing SET last_seen_at = %s, first_seen_at = %s WHERE id = %s",
        (T - 90 * DAY, T - 100 * DAY, followed),
    )
    repo.create_watch_on_listing(db, "exito", followed, None, T)
    report = run_maintenance(db, T, load_params(), db_bytes=410 * MB)
    assert report.listings_pruned == 1
    ids = [r[0] for r in db.execute("SELECT id FROM discountz.listing").fetchall()]
    assert ids == [followed]


def test_retention_of_runs_and_outbox(db: psycopg.Connection) -> None:
    old = repo.start_run(db, T - 40 * DAY, "schedule", 1, 1)
    repo.finish_run(db, old, T - 40 * DAY, "ok", repo.RunCounters())
    recent = repo.start_run(db, T - DAY, "schedule", 2, 1)
    repo.finish_run(db, recent, T - DAY, "ok", repo.RunCounters())
    repo.enqueue(db, "system", "x", priority=2, dedup_key="old:1", now=T - 40 * DAY, ttl=DAY)
    db.execute("UPDATE discountz.outbox SET created_at = %s", (T - 40 * DAY,))
    run_maintenance(db, T, load_params(), db_bytes=10 * MB)
    assert db.execute("SELECT id FROM discountz.run").fetchall() == [(recent,)]
    assert db.execute("SELECT count(*) FROM discountz.outbox").fetchone() == (0,)


def test_due_windows_follow_bogota_time(db: psycopg.Connection) -> None:
    early = T.replace(hour=7, minute=0)  # 02:00 Bogotá
    assert not maintenance_due(db, early)
    assert maintenance_due(db, T)  # 10:00 Bogotá
    run_maintenance(db, T, load_params(), db_bytes=10 * MB)
    assert not maintenance_due(db, T + timedelta(hours=1))
    assert not summary_due(db, T.replace(hour=12, minute=59))  # 07:59 Bogotá
    assert summary_due(db, T)


def test_summary_counts_cycles_by_trigger_and_token_warning(db: psycopg.Connection) -> None:
    for i in range(3):
        rid = repo.start_run(db, T - timedelta(hours=i + 1), "workflow_dispatch", 10 + i, 1)
        repo.finish_run(db, rid, T - timedelta(hours=i + 1), "ok", repo.RunCounters())
    rid = repo.start_run(db, T - timedelta(hours=5), "schedule", 99, 1)
    repo.finish_run(db, rid, T - timedelta(hours=5), "ok", repo.RunCounters())
    db.execute(
        "UPDATE discountz.bot_state SET dispatch_token_expires_on = %s",
        (date(2026, 10, 23),),
    )
    view = daily_summary(db, T)
    assert view.cycles == 4
    assert view.by_trigger == {"workflow_dispatch": 3, "schedule": 1}
    html = render_daily(view).html
    assert "3 por disparo externo, 1 por schedule" in html
    assert "token de disparo vence el 2026-10-23" in html


def test_out_of_range_canary_puts_source_in_quarantine(db: psycopg.Connection) -> None:
    params = load_params()
    store = STORES["exito"]
    url = f"{store.api_base}/catalog_system/pub/products/search?fq=skuId%3A102058783"
    http = FakeHttp({url: load_fixture("vtex", "exito_ficha_102058783")})
    budget = params.budgets["exito"]
    rnd = random.Random(1)
    paced = PacedClient(
        http, budget, lambda s: None, lambda: 0.0, rnd, lambda r: None, source="exito"
    )
    canary = Canary("exito", "102058783", "SSD", 10_000, 20_000)  # el precio real queda fuera
    collector = Collector(
        db, T, params, {"exito": [canary]}, rnd, lambda: 0.0, 1e9, timedelta(minutes=15)
    )
    run = SourceRun(
        VtexSource(store),
        paced,
        budget,
        BreakerState("closed", None, 0, 0, 0, 0),
        deque([Task("canary", "exito", ())]),
    )
    collector.run([run])
    assert run.canary_ok is False
    assert run.breaker.quarantine_cycles == 3
    row = db.execute("SELECT canary_ok FROM discountz.source WHERE code = 'exito'").fetchone()
    assert row == (False,)


# ───────────── Catálogo (spec del rastreo §7.2 y §8)


def _item(conn: psycopg.Connection, ext: str, seen_days_ago: int, source: str = "exito") -> int:
    seen = T - seen_days_ago * DAY
    row = conn.execute(
        """INSERT INTO discountz.catalog_item (source, external_id, topic, currency, price_minor,
               first_seen_at, stable_since, last_seen_at)
           VALUES (%s, %s, 'tecnologia', 'COP', 1000, %s, %s, %s) RETURNING id""",
        (source, ext, seen, seen, seen),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _change(conn: psycopg.Connection, item_id: int, days_ago: int) -> None:
    conn.execute(
        """INSERT INTO discountz.catalog_price_change (item_id, at, price_minor)
           VALUES (%s, %s, 900)""",
        (item_id, T - days_ago * DAY),
    )


def _changes(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM discountz.catalog_price_change").fetchone()
    assert row is not None
    return int(row[0])


def test_catalog_retention_90d_and_unseen_30d(db: psycopg.Connection) -> None:
    fresh = _item(db, "1", 0)
    _item(db, "2", 31)
    for days in (1, 60, 91):
        _change(db, fresh, days)
    run_maintenance(db, T, load_params(), db_bytes=10 * MB)
    items = db.execute("SELECT external_id FROM discountz.catalog_item").fetchall()
    assert items == [("1",)]
    assert _changes(db) == 2


def test_size_guard_400_shortens_retention_to_45d(db: psycopg.Connection) -> None:
    fresh = _item(db, "1", 0)
    for days in (1, 44, 46):
        _change(db, fresh, days)
    run_maintenance(db, T, load_params(), db_bytes=410 * MB)
    assert _changes(db) == 2


def test_size_guard_450_pauses_exito_carulla_crawl(db: psycopg.Connection) -> None:
    run_maintenance(db, T, load_params(), db_bytes=455 * MB)
    for code, expected in (("exito", "disabled"), ("carulla", "disabled"), ("olimpica", "ok")):
        src = FakeCatalog({"all": [page_of(1)]}, code=code)
        d, _ = deps(db, src, Clock())
        assert run_crawl(d, T, minutes=60).status == expected
    notice = db.execute(
        "SELECT body_html FROM discountz.outbox WHERE purpose = 'system' AND priority = 0"
    ).fetchone()
    assert notice is not None and "Éxito y Carulla" in notice[0]


def test_daily_summary_has_catalog_lines(db: psycopg.Connection) -> None:
    item = _item(db, "1", 0)
    _item(db, "2", 0, source="carulla")
    _change(db, item, 0)
    db.execute("INSERT INTO discountz.crawl_cursor (source, last_sweep_s) VALUES ('exito', 18720)")
    repo.enqueue(
        db, "alert", "x", priority=3, dedup_key="cat:1", now=T, ttl=DAY, topic="tecnologia"
    )
    view = daily_summary(db, T)
    assert view.catalog is not None
    assert view.catalog.items_by_source["exito"] == 1
    assert view.catalog.changes_today == 1
    assert view.catalog.alerts_by_topic == {"tecnologia": 1}
    assert view.catalog.last_sweep_s_by_source["exito"] == 18_720


def _channel_post(conn: psycopg.Connection, post_id: int, age: timedelta, dup: str | None) -> None:
    repo.insert_channel_post(
        conn,
        "tg_cajitatech",
        post_id,
        published_at=T - age,
        title_norm="set cuchillos",
        price_minor=1200,
        currency="USD",
        coupon=None,
        topic="hogar",
        duplicate_of=dup,
        now=T - age,
    )


def test_maintenance_deletes_channel_posts_after_7_days(db: psycopg.Connection) -> None:
    _channel_post(db, 1, timedelta(days=8), None)
    _channel_post(db, 2, timedelta(days=6), None)
    run_maintenance(db, T, load_params(), db_bytes=10 * MB)
    rows = db.execute("SELECT post_id FROM discountz.channel_post").fetchall()
    assert rows == [(2,)]


def test_daily_summary_counts_channel_posts(db: psycopg.Connection) -> None:
    for i in range(1, 4):
        _channel_post(db, i, timedelta(hours=i), None)
    _channel_post(db, 4, timedelta(hours=2), "tg_clubgratis:1")
    _channel_post(db, 5, timedelta(hours=30), None)
    repo.insert_channel_post(
        db,
        "dealnews",
        22269285,
        published_at=T,
        title_norm="wand",
        price_minor=4499,
        currency="USD",
        coupon=None,
        topic="belleza",
        duplicate_of=None,
        now=T,
        baseline=True,  # marcada en la primera lectura del feed, sin enviarse
    )
    view = daily_summary(db, T)
    assert view.catalog is not None
    assert (view.catalog.channel_posts, view.catalog.channel_duplicates) == (3, 1)
    assert "\nCanales: 3 ofertas (1 repetidas)" in render_daily(view).html
