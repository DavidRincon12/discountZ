"""Acceso a datos de los canales de Telegram (spec de canales §6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from discountz.infra import db as dbm
from discountz.infra.migrate import current_version, migrate

pytestmark = pytest.mark.integration

T = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _post(
    conn: psycopg.Connection,
    code: str,
    post_id: int,
    *,
    seen: datetime = T,
    duplicate_of: str | None = None,
    price: int = 3000,
    currency: str = "USD",
    baseline: bool = False,
) -> bool:
    return dbm.insert_channel_post(
        conn,
        code,
        post_id,
        published_at=seen,
        title_norm="armatus playstation 5",
        price_minor=price,
        currency=currency,
        coupon=None,
        topic="tecnologia",
        duplicate_of=duplicate_of,
        now=seen,
        baseline=baseline,
    )


def test_migration_registers_channel_sources(db: psycopg.Connection) -> None:
    rows = db.execute(
        "SELECT code, kind, enabled FROM discountz.source WHERE kind='channel' ORDER BY code"
    ).fetchall()
    assert rows == [
        ("dealnews", "channel", True),
        ("slickdeals_amazon", "channel", True),
        ("tg_cajitatech", "channel", True),
        ("tg_clubgratis", "channel", True),
        ("tg_descuentoshome", "channel", True),
        ("tg_descuentostech", "channel", True),
        ("tg_ofertu", "channel", True),
    ]


def test_channel_cursor_roundtrip(db: psycopg.Connection) -> None:
    assert dbm.get_channel_cursor(db, "tg_clubgratis") is None
    dbm.save_channel_cursor(db, "tg_clubgratis", 59494, T)
    dbm.save_channel_cursor(db, "tg_clubgratis", 59500, T + timedelta(hours=1))
    assert dbm.get_channel_cursor(db, "tg_clubgratis") == dbm.ChannelCursor(
        59500, T + timedelta(hours=1)
    )


def test_insert_channel_post_is_idempotent(db: psycopg.Connection) -> None:
    assert _post(db, "tg_cajitatech", 88168) is True
    assert _post(db, "tg_cajitatech", 88168) is False


def test_recent_posts_skip_duplicates_and_old(db: psycopg.Connection) -> None:
    _post(db, "tg_cajitatech", 88100, seen=T - timedelta(hours=25))
    _post(db, "tg_cajitatech", 88168)
    _post(db, "tg_clubgratis", 59510, duplicate_of="tg_cajitatech:88168")
    recent = dbm.recent_channel_posts(db, T - timedelta(hours=24))
    assert [r.key for r in recent] == ["tg_cajitatech:88168"]
    assert recent[0] == dbm.RecentPost(
        "tg_cajitatech:88168", "armatus playstation 5", 3000, "USD", None
    )


def test_delete_old_channel_posts(db: psycopg.Connection) -> None:
    _post(db, "tg_cajitatech", 1, seen=T - timedelta(days=8))
    _post(db, "tg_cajitatech", 2, seen=T - timedelta(days=6))
    assert dbm.delete_old_channel_posts(db, T - timedelta(days=7)) == 1


@pytest.mark.parametrize(("price", "currency"), [(0, "USD"), (3000, "EUR")])
def test_channel_post_rejects_bad_values(db: psycopg.Connection, price: int, currency: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _post(db, "tg_cajitatech", 7, price=price, currency=currency)


def test_migration_0003_down_restores_schema_2(db: psycopg.Connection) -> None:
    dbm.save_channel_cursor(db, "tg_clubgratis", 1, T)
    assert migrate(db, 2) == [7, 6, 5, 4, 3]
    assert current_version(db) == 2
    assert db.execute("SELECT to_regclass('discountz.channel_post')").fetchone() == (None,)
    assert db.execute("SELECT count(*) FROM discountz.source").fetchone() == (9,)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE discountz.source SET kind = 'channel' WHERE code = 'exito'")


def test_channel_post_ids_only_of_that_channel(db: psycopg.Connection) -> None:
    _post(db, "dealnews", 22269285)
    _post(db, "slickdeals_amazon", 20089260)
    assert dbm.channel_post_ids(db, "dealnews", [22269285, 20089260, 7]) == {22269285}
    assert dbm.channel_post_ids(db, "dealnews", []) == set()


def test_baseline_posts_are_known_but_never_originals(db: psycopg.Connection) -> None:
    _post(db, "dealnews", 22269285, baseline=True)
    assert dbm.channel_post_ids(db, "dealnews", [22269285]) == {22269285}
    assert dbm.recent_channel_posts(db, T - timedelta(hours=24)) == []
