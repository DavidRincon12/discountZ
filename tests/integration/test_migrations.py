from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest
from psycopg import errors

from discountz.__main__ import main
from discountz.domain.errors import LeaseBusy
from discountz.infra.db import SCHEMA_VERSION, connect
from discountz.infra.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    current_version,
    migrate,
    migrate_with_lease,
)

pytestmark = pytest.mark.integration


def _scalar(conn: psycopg.Connection, query: str) -> object:
    row = conn.execute(query).fetchone()
    assert row is not None
    return row[0]


def test_up_down_up(empty_db_url: str) -> None:
    with connect(empty_db_url) as conn:
        assert migrate(conn, "head") == [1, 2, 3, 4]
        assert current_version(conn) == SCHEMA_VERSION == 4
        assert _scalar(conn, "SELECT count(*) FROM discountz.source") == 14
        assert migrate(conn, 3) == [4]
        assert _scalar(conn, "SELECT count(*) FROM discountz.source WHERE kind = 'channel'") == 3
        assert migrate(conn, 1) == [3, 2]
        assert _scalar(conn, "SELECT to_regclass('discountz.catalog_item')") is None
        assert _scalar(conn, "SELECT to_regclass('discountz.listing')") is not None
        assert migrate(conn, 0) == [1]
        assert current_version(conn) == 0
        assert _scalar(conn, "SELECT to_regclass('discountz.listing')") is None
        assert migrate(conn, "head") == [1, 2, 3, 4]
        assert migrate(conn, "head") == []


def test_down_0004_removes_feed_channels_with_their_posts(db: psycopg.Connection) -> None:
    db.execute(
        """INSERT INTO discountz.channel_cursor (channel, last_id, updated_at)
           VALUES ('dealnews', 22269285, now())"""
    )
    db.execute(
        """INSERT INTO discountz.channel_post (channel, post_id, published_at, title_norm,
               price_minor, currency, topic, seen_at)
           VALUES ('dealnews', 22269285, now(), 'wand', 4499, 'USD', 'belleza', now())"""
    )
    assert migrate(db, 3) == [4]
    assert _scalar(db, "SELECT count(*) FROM discountz.channel_post") == 0
    assert _scalar(db, "SELECT count(*) FROM discountz.channel_cursor") == 0


def test_seed_sources_and_gates(db: psycopg.Connection) -> None:
    rows = db.execute("SELECT code, enabled FROM discountz.source ORDER BY code").fetchall()
    assert dict(rows) == {
        "amazon_us": True,
        "bestbuy_us": False,
        "carulla": True,
        "dealnews": True,
        "exito": True,
        "newegg": True,
        "olimpica": True,
        "panamericana": True,
        "slickdeals": True,
        "slickdeals_amazon": True,
        "tg_cajitatech": True,
        "tg_clubgratis": True,
        "tg_descuentostech": True,
        "walmart_us": False,
    }


def _listing(conn: psycopg.Connection) -> int:
    row = conn.execute(
        """INSERT INTO discountz.listing (source, external_id, canonical_url, title, first_seen_at,
               last_seen_at, last_in_stock, last_seller_class)
           VALUES ('exito', '123', 'https://www.exito.com/x/p', 'TV', now(), now(), true, 'f')
           RETURNING id"""
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_second_open_segment_is_rejected(db: psycopg.Connection) -> None:
    lid = _listing(db)
    insert = """INSERT INTO discountz.price_segment (listing_id, started_at, last_seen_at,
                    price_minor, in_stock, seller_class)
                VALUES (%s, %s, %s, 1000, true, 'f')"""
    db.execute(insert, (lid, "2026-10-01T00:00Z", "2026-10-01T00:00Z"))
    with pytest.raises(errors.UniqueViolation) as exc:
        db.execute(insert, (lid, "2026-10-02T00:00Z", "2026-10-02T00:00Z"))
    assert exc.value.diag.constraint_name == "price_segment_open_uq"


def test_pending_watch_with_listing_is_rejected(db: psycopg.Connection) -> None:
    lid = _listing(db)
    with pytest.raises(errors.CheckViolation) as exc:
        db.execute(
            """INSERT INTO discountz.watch (source, listing_id, pending_ref, status, next_check_at)
               VALUES ('exito', %s, 'id:123', 'pending', now())""",
            (lid,),
        )
    assert exc.value.diag.constraint_name == "watch_resolved_ck"


def test_money_minor_rejects_zero(db: psycopg.Connection) -> None:
    lid = _listing(db)
    with pytest.raises(errors.CheckViolation):
        db.execute("UPDATE discountz.listing SET last_price_minor = 0 WHERE id = %s", (lid,))


def test_alert_flags_accept_hot_and_unverified(db: psycopg.Connection) -> None:
    lid = _listing(db)
    db.execute(
        """INSERT INTO discountz.alert (listing_id, kind, maturity, price_minor, flags, observed_at)
           VALUES (%s, 'community_deal', 'cold', 1000, ARRAY['hot','unverified'], now())""",
        (lid,),
    )


def test_editing_applied_migration_fails(empty_db_url: str, tmp_path: Path) -> None:
    folder = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, folder)
    with connect(empty_db_url) as conn:
        migrate(conn, "head", directory=folder)
        up = folder / "0001_init.up.sql"
        up.write_text(up.read_text() + "\n-- cambio\n")
        with pytest.raises(MigrationError) as exc:
            migrate(conn, "head", directory=folder)
        assert str(exc.value) == "checksum_mismatch"


def test_migrate_waits_for_lease_then_gives_up(db: psycopg.Connection) -> None:
    db.execute(
        "UPDATE discountz.run_lease SET holder = 'gh:1:1', acquired_at = now(),"
        " expires_at = now() + interval '16 minutes' WHERE id = 1"
    )
    clock = [0.0]

    def fake_sleep(s: float) -> None:
        clock[0] += s

    with pytest.raises(LeaseBusy):
        migrate_with_lease(
            db, "head", holder="migrate:9", wait_s=20, sleep=fake_sleep, monotonic=lambda: clock[0]
        )
    assert clock[0] >= 20


def test_migrate_with_free_lease_releases_it(db: psycopg.Connection) -> None:
    assert migrate_with_lease(
        db, 0, holder="migrate:9", wait_s=1, sleep=lambda s: None, monotonic=lambda: 0.0
    ) == [4, 3, 2, 1]
    assert migrate_with_lease(
        db, "head", holder="migrate:9", wait_s=1, sleep=lambda s: None, monotonic=lambda: 0.0
    ) == [1, 2, 3, 4]
    assert _scalar(db, "SELECT holder FROM discountz.run_lease") is None


def test_cli_migrate_head_and_zero(empty_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCOUNTZ_MIGRATOR_DATABASE_URL", empty_db_url)
    assert main(["migrate", "--to", "head", "--wait-lease", "1m"]) == 0
    assert main(["migrate", "--to", "0"]) == 0
    with connect(empty_db_url) as conn:
        assert current_version(conn) == 0
