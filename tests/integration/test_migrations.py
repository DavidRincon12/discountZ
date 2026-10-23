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
        assert migrate(conn, "head") == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        assert current_version(conn) == SCHEMA_VERSION == 14
        assert _scalar(conn, "SELECT count(*) FROM discountz.source") == 22
        assert migrate(conn, 3) == [14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4]
        assert _scalar(conn, "SELECT count(*) FROM discountz.source WHERE kind = 'channel'") == 3
        assert migrate(conn, 1) == [3, 2]
        assert _scalar(conn, "SELECT to_regclass('discountz.catalog_item')") is None
        assert _scalar(conn, "SELECT to_regclass('discountz.listing')") is not None
        assert migrate(conn, 0) == [1]
        assert current_version(conn) == 0
        assert _scalar(conn, "SELECT to_regclass('discountz.listing')") is None
        assert migrate(conn, "head") == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        assert migrate(conn, "head") == []


def test_down_0004_removes_feeds_and_the_baseline_flag(db: psycopg.Connection) -> None:
    db.execute(
        """INSERT INTO discountz.channel_cursor (channel, last_id, updated_at)
           VALUES ('dealnews', 22269285, now())"""
    )
    db.execute(
        """INSERT INTO discountz.channel_post (channel, post_id, published_at, title_norm,
               price_minor, currency, topic, seen_at)
           VALUES ('dealnews', 22269285, now(), 'wand', 4499, 'USD', 'belleza', now()),
                  ('tg_cajitatech', 88168, now(), 'oled tv', 4499, 'USD', 'electro', now())"""
    )
    assert _scalar(db, "SELECT bool_or(baseline) FROM discountz.channel_post") is False
    assert migrate(db, 3) == [14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4]
    assert _scalar(db, "SELECT channel FROM discountz.channel_post") == "tg_cajitatech"
    assert _scalar(db, "SELECT count(*) FROM discountz.channel_cursor") == 0
    flag = """SELECT count(*) FROM information_schema.columns
               WHERE table_schema = 'discountz' AND table_name = 'channel_post'
                 AND column_name = 'baseline'"""
    assert _scalar(db, flag) == 0


def test_seed_sources_and_gates(db: psycopg.Connection) -> None:
    rows = db.execute("SELECT code, enabled FROM discountz.source ORDER BY code").fetchall()
    assert dict(rows) == {
        "alkosto": True,
        "amazon_us": True,
        "bestbuy_us": False,
        "carulla": True,
        "dealnews": True,
        "ebay_us": True,
        "exito": True,
        "jumbo": True,
        "newegg": True,
        "ninetofivetoys": True,
        "olimpica": True,
        "panamericana": True,
        "pepeganga": True,
        "slickdeals": True,
        "slickdeals_amazon": True,
        "tg_cajitatech": True,
        "tg_clubgratis": True,
        "tg_descuentoshome": True,
        "tg_descuentostech": True,
        "tg_ofertu": True,
        "walmart_us": False,
        "woot": True,
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
    ) == [14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    assert migrate_with_lease(
        db, "head", holder="migrate:9", wait_s=1, sleep=lambda s: None, monotonic=lambda: 0.0
    ) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert _scalar(db, "SELECT holder FROM discountz.run_lease") is None


def test_cli_migrate_head_and_zero(empty_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCOUNTZ_MIGRATOR_DATABASE_URL", empty_db_url)
    assert main(["migrate", "--to", "head", "--wait-lease", "1m"]) == 0
    assert main(["migrate", "--to", "0"]) == 0
    with connect(empty_db_url) as conn:
        assert current_version(conn) == 0


def test_0005_up_down(db: psycopg.Connection) -> None:
    assert _scalar(db, "SELECT kind FROM discountz.source WHERE code = 'ebay_us'") == "store"
    db.execute(
        """INSERT INTO discountz.catalog_item (source, external_id, model_key, topic, currency,
               price_minor, first_seen_at, stable_since, last_seen_at)
           VALUES ('ebay_us', '123456789012', '2288855660', 'tecnologia', 'USD', 8999,
                   now(), now(), now())"""
    )
    with pytest.raises(errors.CheckViolation), db.transaction():
        db.execute("UPDATE discountz.catalog_item SET model_key = 'abc'")
    db.execute(
        """INSERT INTO discountz.listing (source, external_id, canonical_url, title,
               first_seen_at, last_seen_at, last_in_stock, last_seller_class)
           VALUES ('ebay_us', '123456789012', 'https://www.ebay.com/itm/123456789012', 't',
                   now(), now(), true, 'f')"""
    )
    db.execute(
        """INSERT INTO discountz.alert (listing_id, kind, maturity, price_minor, observed_at)
           SELECT id, 'model_low', 'cold', 8999, now() FROM discountz.listing"""
    )
    assert migrate(db, 4) == [14, 13, 12, 11, 10, 9, 8, 7, 6, 5]
    assert _scalar(db, "SELECT count(*) FROM discountz.source WHERE code = 'ebay_us'") == 0
    assert _scalar(db, "SELECT count(*) FROM discountz.alert") == 0
    assert (
        _scalar(
            db,
            """SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'catalog_item' AND column_name = 'model_key'""",
        )
        == 0
    )
    assert migrate(db, "head") == [5, 6, 7, 8, 9, 10, 11, 12, 13, 14]


def test_0006_up_down(db: psycopg.Connection) -> None:
    db.execute(
        "INSERT INTO discountz.channel_cursor (channel, last_id, updated_at)"
        " VALUES ('tg_ofertu', 9737, now())"
    )
    db.execute(
        """INSERT INTO discountz.channel_post (channel, post_id, seen_at, title_norm, price_minor,
               currency, topic, published_at)
           VALUES ('tg_descuentoshome', 26814, now(), 'bandeja', 1400, 'USD', 'hogar', now())"""
    )
    assert migrate(db, 5) == [14, 13, 12, 11, 10, 9, 8, 7, 6]
    codes = "SELECT count(*) FROM discountz.source WHERE code IN ('tg_descuentoshome','tg_ofertu')"
    assert _scalar(db, codes) == 0
    assert _scalar(db, "SELECT count(*) FROM discountz.channel_post") == 0
    assert migrate(db, "head") == [6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert _scalar(db, codes) == 2


def test_0007_up_down(db: psycopg.Connection) -> None:
    code = "SELECT count(*) FROM discountz.source WHERE code = 'alkosto'"
    assert _scalar(db, "SELECT kind || currency FROM discountz.source WHERE code = 'alkosto'") == (
        "storeCOP"
    )
    db.execute(
        """INSERT INTO discountz.catalog_item (source, external_id, topic, currency,
               price_minor, first_seen_at, stable_since, last_seen_at)
           VALUES ('alkosto', '7705946474559', 'tecnologia', 'COP', 1649900,
                   now(), now(), now())"""
    )
    db.execute(
        """INSERT INTO discountz.listing (source, external_id, canonical_url, title,
               first_seen_at, last_seen_at, last_in_stock, last_seller_class)
           VALUES ('alkosto', '7705946474559', 'https://www.alkosto.com/tv/p/7705946474559',
                   't', now(), now(), true, 'f')"""
    )
    db.execute(
        """INSERT INTO discountz.watch (source, pending_ref, next_check_at)
           VALUES ('alkosto', 'slug:tv', now())"""
    )
    assert migrate(db, 6) == [14, 13, 12, 11, 10, 9, 8, 7]
    assert _scalar(db, code) == 0
    assert _scalar(db, "SELECT count(*) FROM discountz.catalog_item") == 0
    assert migrate(db, "head") == [7, 8, 9, 10, 11, 12, 13, 14]
    assert _scalar(db, code) == 1


def test_0008_up_down(db: psycopg.Connection) -> None:
    codes = "SELECT count(*) FROM discountz.source WHERE code IN ('jumbo', 'pepeganga')"
    assert _scalar(db, "SELECT kind || currency FROM discountz.source WHERE code = 'jumbo'") == (
        "storeCOP"
    )
    db.execute(
        """INSERT INTO discountz.catalog_item (source, external_id, topic, currency,
               price_minor, first_seen_at, stable_since, last_seen_at)
           VALUES ('jumbo', '12345', 'mercado', 'COP', 44990, now(), now(), now())"""
    )
    db.execute(
        """INSERT INTO discountz.listing (source, external_id, canonical_url, title,
               first_seen_at, last_seen_at, last_in_stock, last_seller_class)
           VALUES ('pepeganga', '455', 'https://www.pepeganga.com/mordedor-455/p',
                   't', now(), now(), true, 'f')"""
    )
    db.execute(
        """INSERT INTO discountz.watch (source, pending_ref, next_check_at)
           VALUES ('jumbo', 'slug:tv', now())"""
    )
    assert migrate(db, 7) == [14, 13, 12, 11, 10, 9, 8]
    assert _scalar(db, codes) == 0
    assert _scalar(db, "SELECT count(*) FROM discountz.catalog_item") == 0
    assert _scalar(db, "SELECT count(*) FROM discountz.listing") == 0
    assert _scalar(db, "SELECT count(*) FROM discountz.watch") == 0
    assert migrate(db, "head") == [8, 9, 10, 11, 12, 13, 14]
    assert _scalar(db, codes) == 2


def test_0009_up_down(db: psycopg.Connection) -> None:
    has = """SELECT count(*) FROM information_schema.columns
              WHERE table_schema = 'discountz' AND table_name = 'catalog_item'
                AND column_name = 'min_seen_at'"""
    assert _scalar(db, has) == 1
    assert migrate(db, 8) == [14, 13, 12, 11, 10, 9]
    assert _scalar(db, has) == 0
    assert migrate(db, "head") == [9, 10, 11, 12, 13, 14]
    assert _scalar(db, has) == 1


def test_0010_up_down(db: psycopg.Connection) -> None:
    """El tope diario: `offer_log` y la alerta retenida (`held`) para el resumen de la noche."""
    db.execute(
        """INSERT INTO discountz.offer_log (source, day, topic, kind, drop_bp, carded, line,
               created_at)
           VALUES ('exito', '2026-10-22', 'hogar', 'catalog_drop', 2000, false, '• x', now())"""
    )
    db.execute(
        """INSERT INTO discountz.listing (source, external_id, canonical_url, title,
               first_seen_at, last_seen_at, last_in_stock, last_seller_class)
           VALUES ('exito', '1', 'https://www.exito.com/x/p', 't', now(), now(), true, 'f')"""
    )
    db.execute(
        """INSERT INTO discountz.alert (listing_id, kind, maturity, price_minor, delivery,
               observed_at)
           SELECT id, 'catalog_drop', 'warm', 119900, 'held', now() FROM discountz.listing"""
    )
    with pytest.raises(errors.CheckViolation):  # retenida sin línea para el resumen
        db.execute(
            """INSERT INTO discountz.offer_log (source, day, topic, kind, carded, created_at)
               VALUES ('exito', '2026-10-22', 'hogar', 'catalog_drop', false, now())"""
        )
    assert migrate(db, 9) == [14, 13, 12, 11, 10]
    assert _scalar(db, "SELECT to_regclass('discountz.offer_log')") is None
    assert _scalar(db, "SELECT delivery FROM discountz.alert") == "suppressed"
    assert migrate(db, "head") == [10, 11, 12, 13, 14]
    assert _scalar(db, "SELECT count(*) FROM discountz.offer_log") == 0


def _catalog_item(conn: psycopg.Connection, source: str, ext: str) -> None:
    conn.execute(
        """INSERT INTO discountz.catalog_item (source, external_id, topic, currency, price_minor,
               first_seen_at, stable_since, last_seen_at)
           VALUES (%s, %s, 'hogar', 'COP', 1000, now(), now(), now())""",
        (source, ext),
    )


def test_0011_drops_price_changes_and_catalog_of_switched_off_stores(
    db: psycopg.Connection,
) -> None:
    """Sin `catalog_price_change` (PR #31) ni el catálogo de Carulla y Olímpica apagadas."""
    assert migrate(db, 10) == [14, 13, 12, 11]
    db.execute("UPDATE discountz.source SET enabled = false WHERE code IN ('carulla', 'olimpica')")
    for source in ("carulla", "olimpica", "exito"):
        _catalog_item(db, source, "1")
    assert migrate(db, "head") == [11, 12, 13, 14]
    assert _scalar(db, "SELECT to_regclass('discountz.catalog_price_change')") is None
    assert _scalar(db, "SELECT string_agg(source, ',') FROM discountz.catalog_item") == "exito"
    assert migrate(db, 10) == [14, 13, 12, 11]
    assert _scalar(db, "SELECT count(*) FROM discountz.catalog_price_change") == 0


def test_0011_keeps_catalog_of_a_store_switched_back_on(db: psycopg.Connection) -> None:
    """Si David la volvió a prender antes de migrar, su catálogo se queda."""
    assert migrate(db, 10) == [14, 13, 12, 11]
    db.execute("UPDATE discountz.source SET enabled = false WHERE code = 'olimpica'")
    _catalog_item(db, "carulla", "1")
    _catalog_item(db, "olimpica", "1")
    assert migrate(db, "head") == [11, 12, 13, 14]
    assert _scalar(db, "SELECT string_agg(source, ',') FROM discountz.catalog_item") == "carulla"


def test_0012_up_down(db: psycopg.Connection) -> None:
    """Intereses personales (`interest`) y el interés que sacó cada tarjeta (`offer_log`)."""
    db.execute(
        """INSERT INTO discountz.interest (name, topic, terms, created_at)
           VALUES ('RTX 5070', 'tecnologia', ARRAY['rtx 5070', 'rtx5070'], now())"""
    )
    with pytest.raises(errors.CheckViolation):  # más de 10 términos
        db.execute(
            """INSERT INTO discountz.interest (name, topic, terms, created_at)
               VALUES ('Muchos', 'otros', array_fill('x'::text, ARRAY[11]), now())"""
        )
    with pytest.raises(errors.CheckViolation):  # nombre de más de 40 caracteres
        db.execute(
            """INSERT INTO discountz.interest (name, topic, terms, created_at)
               VALUES (repeat('x', 41), 'otros', ARRAY['x'], now())"""
        )
    db.execute(
        """INSERT INTO discountz.offer_log (source, day, topic, kind, drop_bp, carded,
               created_at, interest)
           VALUES ('exito', '2026-10-22', 'hogar', 'catalog_drop', 2000, true, now(),
               'Silla de oficina')"""
    )
    column = """SELECT count(*) FROM information_schema.columns
                 WHERE table_schema = 'discountz' AND table_name = 'offer_log'
                   AND column_name = 'interest'"""
    assert migrate(db, 11) == [14, 13, 12]
    assert _scalar(db, "SELECT to_regclass('discountz.interest')") is None
    assert _scalar(db, column) == 0
    assert _scalar(db, "SELECT count(*) FROM discountz.offer_log") == 1
    assert migrate(db, "head") == [12, 13, 14]
    assert _scalar(db, column) == 1
    assert _scalar(db, "SELECT count(*) FROM discountz.interest") == 0


def test_0013_up_down(db: psycopg.Connection) -> None:
    code = "SELECT kind, currency, enabled FROM discountz.source WHERE code = 'ninetofivetoys'"
    assert db.execute(code).fetchone() == ("channel", "USD", True)
    db.execute(
        """INSERT INTO discountz.channel_post (channel, post_id, seen_at, title_norm, price_minor,
               currency, topic, published_at)
           VALUES ('ninetofivetoys', 1238275, now(), 'apple tv', 12900, 'USD', 'electro', now())"""
    )
    assert migrate(db, 12) == [14, 13]
    assert db.execute(code).fetchone() is None
    assert _scalar(db, "SELECT count(*) FROM discountz.channel_post") == 0
    assert migrate(db, "head") == [13, 14]


def test_0014_up_down(db: psycopg.Connection) -> None:
    """Woot como canal en USD; al revertir se van su cursor y sus publicaciones."""
    row = db.execute(
        "SELECT kind, currency, enabled FROM discountz.source WHERE code = 'woot'"
    ).fetchone()
    assert row == ("channel", "USD", True)
    db.execute(
        """INSERT INTO discountz.channel_post (channel, post_id, published_at, title_norm,
               price_minor, currency, topic, seen_at)
           VALUES ('woot', 123456789, now(), 'ssd', 4999, 'USD', 'tecnologia', now())"""
    )
    assert migrate(db, 13) == [14]
    assert _scalar(db, "SELECT count(*) FROM discountz.source WHERE code = 'woot'") == 0
    assert _scalar(db, "SELECT count(*) FROM discountz.channel_post") == 0
    assert migrate(db, "head") == [14]
