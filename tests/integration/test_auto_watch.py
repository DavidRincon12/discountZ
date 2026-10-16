"""Seguimientos automáticos desde Slickdeals (spec del rastreo §4.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest

from discountz.app.cycle import run_cycle
from discountz.app.maintenance import run_maintenance
from discountz.config import load_params
from discountz.domain.model import ListingRef
from discountz.domain.money import Currency
from discountz.infra import db as repo
from discountz.infra.http import FakeHttp
from tests.e2e.test_cycle import (  # noqa: F401 - `conn` es una fixture
    AMAZON_DP,
    EVO_LEAD,
    LEAD_ASIN,
    amazon_page,
    conn,
    deps,
    feed_http,
    slickdeals_search,
    telegram,
)
from tests.factories import offer
from tests.integration.conftest import db, empty_db_url  # noqa: F401
from tests.unit.series import T

pytestmark = pytest.mark.integration
TTL = timedelta(days=30)
OTHER_ASIN = "B0CCCCCCC1"
OTHER_LEAD = (
    "Cafetera Ninja $49.99",
    f'<a data-product-exitWebsite="amazon.com" data-aps-asin="{OTHER_ASIN}">Amazon</a>',
)


def watches(cx: psycopg.Connection) -> list[tuple[str, str, object]]:
    rows = cx.execute(
        """SELECT w.origin, coalesce(w.pending_ref, 'id:' || l.external_id), w.expires_at
             FROM discountz.watch w LEFT JOIN discountz.listing l ON l.id = w.listing_id
            ORDER BY 2"""
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def ref(asin: str) -> ListingRef:
    return ListingRef("amazon_us", asin)


def listing(cx: psycopg.Connection, asin: str) -> int:
    o = offer(source="amazon_us", external_id=asin, price=8_999, currency=Currency.USD)
    return repo.upsert_listing(cx, o, "otros", f"https://www.amazon.com/dp/{asin}", T).id


def test_every_routable_lead_creates_auto_watch(conn: psycopg.Connection) -> None:  # noqa: F811
    slickdeals_search(conn)  # «990 evo»: la cafetera no coincide y aun así se sigue
    amazon = FakeHttp({AMAZON_DP: amazon_page(7_499)})
    run_cycle(deps(conn, feed_http(amazon, EVO_LEAD, OTHER_LEAD), telegram()), T)
    assert watches(conn) == [
        ("auto", f"id:{OTHER_ASIN}", T + TTL),
        ("auto", f"id:{LEAD_ASIN}", T + TTL),
    ]


def test_auto_watch_cap_keeps_newest(db: psycopg.Connection) -> None:  # noqa: F811
    for i in range(3):
        repo.create_auto_watch(
            db, "amazon_us", ref(f"B0AAAAAAA{i}"), T + timedelta(minutes=i), TTL, cap=2
        )
    assert [w[1] for w in watches(db)] == ["id:B0AAAAAAA1", "id:B0AAAAAAA2"]
    assert (
        repo.create_auto_watch(db, "newegg", ListingRef("newegg", "N82E1"), T, TTL, cap=0) is None
    )


def test_alert_extends_auto_watch_30_days(db: psycopg.Connection) -> None:  # noqa: F811
    lid = listing(db, LEAD_ASIN)
    repo.create_auto_watch(db, "amazon_us", ref(LEAD_ASIN), T - timedelta(days=20), TTL, cap=10)
    repo.create_auto_watch(db, "amazon_us", ref(OTHER_ASIN), T - timedelta(days=20), TTL, cap=10)
    repo.extend_auto_watch(db, lid, T + TTL)
    exp = {w[1]: w[2] for w in watches(db)}
    assert exp[f"id:{LEAD_ASIN}"] == T + TTL
    assert exp[f"id:{OTHER_ASIN}"] == T + timedelta(days=10)


def test_lead_alert_extends_auto_watch_in_cycle(conn: psycopg.Connection) -> None:  # noqa: F811
    slickdeals_search(conn)
    old = T - timedelta(days=20)
    o = offer(source="amazon_us", external_id=LEAD_ASIN, price=8_999, currency=Currency.USD)
    repo.upsert_listing(conn, replace(o, observed_at=old), "componentes", AMAZON_DP, old)
    repo.create_auto_watch(conn, "amazon_us", ref(LEAD_ASIN), old, TTL, cap=10)
    # Leído hace tiempo y sin lectura vencida: entra por el lead de Slickdeals.
    conn.execute("UPDATE discountz.watch SET next_check_at = %s", (T + timedelta(hours=1),))
    amazon = FakeHttp({AMAZON_DP: amazon_page(7_499)})
    run_cycle(deps(conn, feed_http(amazon, EVO_LEAD), telegram()), T)
    assert conn.execute("SELECT count(*) FROM discountz.alert").fetchone() == (1,)
    assert watches(conn)[0][2] == T + TTL


def test_maintenance_expires_old_auto_watches(db: psycopg.Connection) -> None:  # noqa: F811
    repo.create_auto_watch(db, "amazon_us", ref(LEAD_ASIN), T - timedelta(days=31), TTL, cap=10)
    repo.create_auto_watch(db, "amazon_us", ref(OTHER_ASIN), T - timedelta(days=5), TTL, cap=10)
    repo.create_watch(db, "amazon_us", "id:B0MANUAL01", "otros", None, T - timedelta(days=90))
    run_maintenance(db, T, load_params(), db_bytes=0)
    assert [w[1] for w in watches(db)] == [f"id:{OTHER_ASIN}", "id:B0MANUAL01"]


def test_manual_watch_not_overwritten_by_auto(db: psycopg.Connection) -> None:  # noqa: F811
    lid = listing(db, LEAD_ASIN)
    repo.create_watch_on_listing(db, "amazon_us", lid, None, T)
    repo.create_watch(db, "amazon_us", f"id:{OTHER_ASIN}", "otros", None, T)
    assert repo.create_auto_watch(db, "amazon_us", ref(LEAD_ASIN), T, TTL, cap=10) is None
    assert repo.create_auto_watch(db, "amazon_us", ref(OTHER_ASIN), T, TTL, cap=10) is None
    assert {w[0] for w in watches(db)} == {"manual"}


def test_follow_turns_auto_watch_into_manual(db: psycopg.Connection) -> None:  # noqa: F811
    lid = listing(db, LEAD_ASIN)
    repo.create_auto_watch(db, "amazon_us", ref(LEAD_ASIN), T, TTL, cap=10)
    repo.create_auto_watch(db, "amazon_us", ref(OTHER_ASIN), T, TTL, cap=10)
    repo.create_watch_on_listing(db, "amazon_us", lid, None, T)
    repo.create_watch(db, "amazon_us", f"id:{OTHER_ASIN}", "otros", None, T)
    assert [(w[0], w[2]) for w in watches(db)] == [("manual", None), ("manual", None)]
