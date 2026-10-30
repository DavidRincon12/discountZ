"""Rastreo sembrado con los EAN de otras tiendas (spec de Mercado Libre §1.3), contra Postgres."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import psycopg
import pytest

from discountz.app.crawl import run_crawl
from discountz.domain.catalog import CatalogObservation, CatalogState
from discountz.domain.errors import CredentialUnavailable, HardBlock, ParseError, Unavailable
from discountz.domain.health import effective_state
from discountz.domain.model import ExtractionPath, ListingRef, Offer, SellerClass
from discountz.domain.money import Currency, Money
from discountz.infra import db as repo
from discountz.infra.db import CatalogRow
from discountz.sources.base import SourceContext
from tests.integration.test_crawl import Clock, FakeCatalog, T, count, deps

pytestmark = pytest.mark.integration
ML = "mercadolibre"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def gtin(n: int) -> str:
    """EAN-13 colombiano válido, distinto para cada `n`."""
    body = f"770{n:09d}"
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return body + str((10 - total % 10) % 10)


A, B, C, D, E = (gtin(n) for n in range(1, 6))
BAD = "6921732899395"  # dígito de control malo


@dataclass
class FakeEanCatalog(FakeCatalog):
    """Fuente sembrada (Mercado Libre): cada búsqueda por EAN es una petición."""

    by_ean: dict[str, str] = field(default_factory=dict)
    reads_ok: int | None = None  # lecturas que responden antes de que la red falle del todo
    broken: dict[str, Exception] = field(default_factory=dict)  # EAN o producto → su falla
    code: str = ML
    max_batch: int = 1

    def find_by_ean(self, ctx: SourceContext, ean: str) -> str | None:
        ctx.http.request("GET", f"https://fake/search/{ean}")
        if ean in self.broken:
            raise self.broken[ean]
        return self.by_ean.get(ean)

    def fetch_catalog_items(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> Sequence[CatalogObservation]:
        if self.reads_ok is not None:
            if self.reads_ok == 0:
                ctx.http.request("GET", "https://fake/items")
                raise Unavailable(self.code, "retries_exhausted")
            self.reads_ok -= 1
        for ext in external_ids:
            if ext in self.broken:
                ctx.http.request("GET", "https://fake/items")
                raise self.broken[ext]
        return list(super().fetch_catalog_items(ctx, external_ids))


def meli(ext: str, price: int) -> CatalogObservation:
    """Lo que da la fuente: sin ruta de categoría ni EAN (los pone el rastreo)."""
    return CatalogObservation(ext, Money(price, Currency.COP), "", None, SellerClass.FIRST_PARTY)


def meli_offer(ext: str, price: int) -> Offer:
    return Offer(
        ref=ListingRef(ML, ext),
        parent_external_id=None,
        title=f"Taladro {ext}",
        price=Money(price, Currency.COP),
        list_price=None,
        in_stock=True,
        seller_class=SellerClass.FIRST_PARTY,
        unit_key="un|1",
        extraction=ExtractionPath.API_JSON,
        observed_at=T,
        url_path=f"/p/{ext}",
    )


def store_row(
    conn: psycopg.Connection,
    source: str,
    ext: str,
    ean: str,
    *,
    topic: str = "tecnologia",
    price: int = 2_000_000,
    at: datetime = T - HOUR,
) -> None:
    state = CatalogState(price, price, price, at, at, 2, None, None, None)
    repo.save_catalog_page(conn, source, [CatalogRow(ext, ean, topic, "COP", state)], at)


def lookups(conn: psycopg.Connection) -> dict[str, str | None]:
    rows = conn.execute("SELECT ean, external_id FROM discountz.ean_lookup").fetchall()
    return {r[0]: r[1] for r in rows}


def tracked(conn: psycopg.Connection) -> int:
    return count(conn, "catalog_item", f"source = '{ML}'")


def checked(conn: psycopg.Connection) -> dict[str, datetime | None]:
    rows = conn.execute(
        "SELECT external_id, checked_at FROM discountz.catalog_item WHERE source = %s", (ML,)
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def aged(conn: psycopg.Connection, *exts: str) -> None:
    """Productos seguidos y por releer, del más viejo al más nuevo."""
    for i, ext in enumerate(exts):
        store_row(conn, ML, ext, gtin(100 + i), at=T - (20 - i) * HOUR)


def test_seeds_track_found_products_with_seed_ean_and_topic(
    db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    store_row(db, "exito", "1", A, price=2_000_000)
    store_row(db, "exito", "2", B, topic="electro", price=1_500_000)
    src = FakeEanCatalog({}, by_ean={A: "MCO1"}, hot={"MCO1": meli("MCO1", 1_900_000)})
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "ok"
    row = db.execute(
        """SELECT ean, topic, price_minor FROM discountz.catalog_item
            WHERE source = %s AND external_id = 'MCO1'""",
        (ML,),
    ).fetchone()
    assert row == (A, "tecnologia", 1_900_000)
    assert lookups(db) == {A: "MCO1", B: None}
    assert "crawl_seeds source=mercadolibre count=2 found=1" in caplog.text


def test_invalid_gtin_is_recorded_without_requests(db: psycopg.Connection) -> None:
    store_row(db, "exito", "1", BAD)
    src = FakeEanCatalog({})
    d, transport = deps(db, src, Clock())
    run_crawl(d, T, minutes=30)
    assert lookups(db) == {BAD: None}
    assert "search" not in transport.kinds()


def test_two_seeds_of_the_same_product_track_it_once(db: psycopg.Connection) -> None:
    store_row(db, "exito", "1", A)
    store_row(db, "jumbo", "2", B)
    src = FakeEanCatalog({}, by_ean={A: "MCO1", B: "MCO1"}, hot={"MCO1": meli("MCO1", 1_900_000)})
    d, transport = deps(db, src, Clock())
    run_crawl(d, T, minutes=30)
    assert tracked(db) == 1
    assert transport.kinds().count("items") == 1
    assert lookups(db) == {A: "MCO1", B: "MCO1"}


def test_already_tracked_product_is_not_read_again_from_a_seed(db: psycopg.Connection) -> None:
    store_row(db, ML, "MCO1", C, at=T - HOUR)  # seguido por otro EAN y al día
    store_row(db, "exito", "1", A)
    src = FakeEanCatalog({}, by_ean={A: "MCO1"}, hot={"MCO1": meli("MCO1", 1_900_000)})
    d, transport = deps(db, src, Clock())
    run_crawl(d, T, minutes=30)
    assert "items" not in transport.kinds()
    assert lookups(db) == {A: "MCO1"}


def test_found_product_without_sellers_is_not_searched_again(db: psycopg.Connection) -> None:
    store_row(db, "exito", "1", C)
    src = FakeEanCatalog({}, by_ean={C: "MCO2"})  # sin vendedores elegibles: ninguna observación
    d, _ = deps(db, src, Clock())
    run_crawl(d, T, minutes=30)
    assert tracked(db) == 0
    assert lookups(db) == {C: "MCO2"}
    d2, transport2 = deps(db, src, Clock())
    run_crawl(d2, T + HOUR, minutes=30)
    assert "search" not in transport2.kinds()


def test_refresh_reads_tracked_items_and_marks_checked(db: psycopg.Connection) -> None:
    store_row(db, ML, "MCO1", A, price=1_000_000, at=T - 7 * HOUR)
    store_row(db, ML, "MCO2", B, topic="hogar", price=900_000, at=T - 7 * HOUR)
    store_row(db, ML, "MCO3", C, topic="tema_viejo", price=800_000, at=T - 7 * HOUR)
    store_row(db, ML, "MCO4", D, topic="belleza", price=700_000, at=T - 7 * HOUR)  # apagado
    hot = {"MCO1": meli("MCO1", 1_000_000), "MCO3": meli("MCO3", 800_000)}
    src = FakeEanCatalog({}, hot=hot)
    d, transport = deps(db, src, Clock())
    run_crawl(d, T, minutes=30)
    rows = db.execute(
        """SELECT external_id, ean, topic, last_seen_at, checked_at FROM discountz.catalog_item
            WHERE source = %s ORDER BY external_id""",
        (ML,),
    ).fetchall()
    assert rows == [
        ("MCO1", A, "tecnologia", T, T),
        ("MCO2", B, "hogar", T - 7 * HOUR, T),  # sin vendedores: marcada, no vista
        ("MCO3", C, "otros", T, T),  # tema que ya no existe: el de la ruta
        ("MCO4", D, "belleza", T - 7 * HOUR, None),
    ]
    assert transport.kinds().count("items") == 3


def test_unreadable_read_is_marked_and_read_once(db: psycopg.Connection) -> None:
    aged(db, "MCO1", "MCO2")
    store_row(db, "exito", "1", E)
    hot = {"MCO2": meli("MCO2", 2_000_000), "MCO9": meli("MCO9", 1_900_000)}
    broken: dict[str, Exception] = {"MCO1": ParseError("schema")}
    src = FakeEanCatalog({}, by_ean={E: "MCO9"}, hot=hot, broken=broken)
    d, transport = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "ok"
    assert checked(db) == {"MCO1": T, "MCO2": T, "MCO9": None}
    assert transport.kinds().count("items") == 3  # la ilegible no se repite en la otra pasada
    assert lookups(db) == {E: "MCO9"}


def test_scattered_failures_do_not_open_the_breaker(db: psycopg.Connection) -> None:
    ids = ("MCO1", "MCO2", "MCO3", "MCO4", "MCO5", "MCO6")
    aged(db, *ids)
    hot = {i: meli(i, 2_000_000) for i in ids[1::2]}
    src = FakeEanCatalog({}, hot=hot, broken=dict.fromkeys(ids[::2], ParseError("schema")))
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "ok"
    assert effective_state(repo.load_breakers(db)[ML], T) == "closed"


def test_product_that_always_errors_is_skipped_and_marked(db: psycopg.Connection) -> None:
    aged(db, "MCO1", "MCO2")
    store_row(db, "exito", "1", E)
    hot = {"MCO2": meli("MCO2", 2_000_000), "MCO9": meli("MCO9", 1_900_000)}
    broken: dict[str, Exception] = {"MCO1": Unavailable(ML, "http_error", 400)}
    src = FakeEanCatalog({}, by_ean={E: "MCO9"}, hot=hot, broken=broken)
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "ok"
    assert checked(db) == {"MCO1": T, "MCO2": T, "MCO9": None}
    assert lookups(db) == {E: "MCO9"}


def test_product_with_a_lasting_5xx_does_not_come_back_first(db: psycopg.Connection) -> None:
    aged(db, "MCO1", "MCO2")
    broken: dict[str, Exception] = {"MCO1": Unavailable(ML, "retries_exhausted")}
    src = FakeEanCatalog({}, hot={"MCO2": meli("MCO2", 2_000_000)}, broken=broken)
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "partial"  # tras los reintentos, la corrida para
    assert checked(db) == {"MCO1": T, "MCO2": None}  # pero ese producto cuenta como pedido
    d2, _ = deps(db, src, Clock())
    assert run_crawl(d2, T + HOUR, minutes=30).status == "ok"
    assert checked(db) == {"MCO1": T, "MCO2": T + HOUR}


@pytest.mark.parametrize("bad", [Unavailable(ML, "http_error", 400), ParseError("schema")])
def test_three_failures_in_a_row_stop_the_run(db: psycopg.Connection, bad: Exception) -> None:
    aged(db, "MCO1", "MCO2", "MCO3", "MCO4")
    broken: dict[str, Exception] = dict.fromkeys(("MCO1", "MCO2", "MCO3"), bad)
    src = FakeEanCatalog({}, hot={"MCO4": meli("MCO4", 2_000_000)}, broken=broken)
    d, transport = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "partial"
    assert transport.kinds().count("items") == 3
    assert checked(db) == {"MCO1": T, "MCO2": T, "MCO3": T, "MCO4": None}


def test_blocked_product_is_marked(db: psycopg.Connection) -> None:
    aged(db, "MCO1", "MCO2")
    broken: dict[str, Exception] = {"MCO1": HardBlock(ML, "forbidden", 403)}
    src = FakeEanCatalog({}, hot={"MCO2": meli("MCO2", 2_000_000)}, broken=broken)
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "blocked"
    assert checked(db) == {"MCO1": T, "MCO2": None}


def test_seed_whose_product_fails_is_recorded(db: psycopg.Connection) -> None:
    store_row(db, "exito", "1", A)
    store_row(db, "exito", "2", B, price=1_500_000)
    broken: dict[str, Exception] = {"MCO1": Unavailable(ML, "http_error", 400)}
    hot = {"MCO2": meli("MCO2", 1_400_000)}
    src = FakeEanCatalog({}, by_ean={A: "MCO1", B: "MCO2"}, hot=hot, broken=broken)
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "ok"
    assert lookups(db) == {A: "MCO1", B: "MCO2"}
    assert tracked(db) == 1


@pytest.mark.parametrize(
    "error", [CredentialUnavailable(ML, "bad_key", 401), Unavailable(ML, "deadline")]
)
def test_stop_that_is_not_the_products_fault_records_nothing(
    db: psycopg.Connection, error: Exception
) -> None:
    store_row(db, "exito", "1", A)
    src = FakeEanCatalog({}, by_ean={A: "MCO1"}, broken={"MCO1": error})
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "partial"
    assert lookups(db) == {}  # la semilla se repite en la próxima corrida


def test_seed_whose_search_keeps_failing_is_recorded(db: psycopg.Connection) -> None:
    store_row(db, "exito", "1", A)
    src = FakeEanCatalog({}, broken={A: Unavailable(ML, "retries_exhausted")})
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "partial"
    assert lookups(db) == {A: None}  # no vuelve primero en cada corrida


def test_refresh_leaves_room_for_seeds(db: psycopg.Connection) -> None:
    for i, ean in enumerate((A, B, C, D)):
        store_row(db, ML, f"MCO{i}", ean, at=T - 7 * HOUR)  # seguidos y por releer
    store_row(db, "exito", "1", E)
    hot = {f"MCO{i}": meli(f"MCO{i}", 2_000_000) for i in range(4)}
    src = FakeEanCatalog({}, by_ean={E: "MCO9"}, hot=hot | {"MCO9": meli("MCO9", 1_900_000)})
    d, transport = deps(db, src, Clock(), max_per_run=4)  # el 25 % (1) relee antes de sembrar
    run_crawl(d, T, minutes=30)
    assert transport.kinds() == ["items", "search", "items", "items"]
    assert lookups(db) == {E: "MCO9"}


def test_seeds_stop_at_max_items_and_when_discovery_paused(db: psycopg.Connection) -> None:
    store_row(db, ML, "MCO9", C, at=T - HOUR)  # seguido y al día
    store_row(db, "exito", "1", A)
    src = FakeEanCatalog({}, by_ean={A: "MCO1"}, hot={"MCO1": meli("MCO1", 1_900_000)})
    d, transport = deps(db, src, Clock(), max_items=1)
    run_crawl(d, T, minutes=30)
    assert "search" not in transport.kinds()
    repo.set_maintenance_state(db, T.date(), 455 * 1024 * 1024, discovery_paused=True)
    d2, transport2 = deps(db, src, Clock())
    run_crawl(d2, T + HOUR, minutes=30)
    assert "search" not in transport2.kinds()
    assert tracked(db) == 1


def test_run_cut_mid_chunk_keeps_what_was_done(db: psycopg.Connection) -> None:
    eans = [A, B, C, D, E]
    for i, ean in enumerate(eans):
        store_row(db, "exito", str(i), ean, price=2_000_000 - i)
    products = {ean: f"MCO{i}" for i, ean in enumerate(eans)}
    hot = {p: meli(p, 1_900_000) for p in products.values()}
    src = FakeEanCatalog({}, by_ean=products, hot=hot)
    d, _ = deps(db, src, Clock(), max_per_run=8)  # 5 búsquedas y 3 lecturas
    run_crawl(d, T, minutes=30)
    assert tracked(db) == 3
    assert lookups(db) == {A: "MCO0", B: "MCO1", C: "MCO2"}


def test_failure_mid_chunk_keeps_what_was_done(db: psycopg.Connection) -> None:
    eans = [A, B, C, D, E]
    for i, ean in enumerate(eans):
        store_row(db, "exito", str(i), ean, price=2_000_000 - i)
    products = {ean: f"MCO{i}" for i, ean in enumerate(eans)}
    hot = {p: meli(p, 1_900_000) for p in products.values()}
    src = FakeEanCatalog({}, by_ean=products, hot=hot, reads_ok=3)
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=30).status == "partial"
    assert tracked(db) == 3
    # La semilla que se leía al parar cuenta como buscada: un producto roto no vuelve primero.
    assert lookups(db) == {A: "MCO0", B: "MCO1", C: "MCO2", D: "MCO3"}


def test_drop_after_warmup_alerts_with_card(db: psycopg.Connection) -> None:
    store_row(db, "exito", "1", A, topic="ferreteria", price=1_200_000)
    src = FakeEanCatalog({}, by_ean={A: "MCO1"}, hot={"MCO1": meli("MCO1", 1_000_000)})
    for at in (T, T + DAY, T + 2 * DAY):
        d, _ = deps(db, src, Clock())
        assert run_crawl(d, at, minutes=30).alerts == 0
    src.hot["MCO1"] = meli("MCO1", 800_000)
    src.offers["MCO1"] = meli_offer("MCO1", 800_000)
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T + 4 * DAY, minutes=30).alerts == 1
    (row,) = repo.claim_outbox(db, 10, T + 4 * DAY)
    assert row.topic == "ferreteria"
    assert row.card is not None
    assert (row.card["store"], row.card["topic"], row.card["price_minor"]) == (
        ML,
        "ferreteria",
        800_000,
    )


def test_drop_read_while_stopping_is_confirmed_on_the_next_run(db: psycopg.Connection) -> None:
    store_row(db, "exito", "1", A, topic="ferreteria", price=1_200_000)
    src = FakeEanCatalog({}, by_ean={A: "MCO1"}, hot={"MCO1": meli("MCO1", 1_000_000)})
    for at in (T, T + DAY, T + 2 * DAY):
        d, _ = deps(db, src, Clock())
        run_crawl(d, at, minutes=30)
    store_row(db, ML, "MCO2", B, at=T + 2 * DAY + HOUR)  # se relee después de MCO1
    src.hot["MCO1"] = meli("MCO1", 800_000)
    src.offers["MCO1"] = meli_offer("MCO1", 800_000)
    src.reads_ok = 1  # MCO1 responde y la red se cae en MCO2
    d, transport = deps(db, src, Clock())
    result = run_crawl(d, T + 4 * DAY, minutes=30)
    assert (result.status, result.alerts) == ("partial", 0)
    assert "listings" not in transport.kinds()  # parando no se confirma nada
    src.reads_ok = None
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T + 4 * DAY + 7 * HOUR, minutes=30).alerts == 1
