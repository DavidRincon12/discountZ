"""El ciclo completo de punta a punta: BD real, VTEX con fixtures y Telegram falso (Task 19)."""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from discountz.app import cycle as cycle_module
from discountz.app.cycle import CycleDeps, run_cycle
from discountz.config import Settings, load_canaries, load_params
from discountz.domain.money import Currency
from discountz.domain.scheduling import plan_run
from discountz.infra import db as repo
from discountz.infra.healthchecks import Healthchecks
from discountz.infra.http import FakeHttp, HttpClient, HttpResponse
from discountz.infra.log import configure_logging
from discountz.infra.telegram import TelegramClient
from discountz.sources.fixtures import load_fixture
from discountz.sources.registry import load_all
from discountz.sources.slickdeals import RSS_URL
from discountz.sources.vtex import STORES
from tests.factories import offer
from tests.integration.conftest import connect_extra, db, empty_db_url  # noqa: F401
from tests.unit.series import T

pytestmark = pytest.mark.integration

TOKEN = "777000:TOKEN-de-prueba_e2e"  # noqa: S105 - falso
TG = f"https://api.telegram.org/bot{TOKEN}"
OWNER = 42
PAN = STORES["panamericana"]
EXITO = STORES["exito"]
PAN_SKU = "659232"
PAN_FETCH = f"{PAN.api_base}/catalog_system/pub/products/search?fq=skuId%3A{PAN_SKU}"
PAN_SIM = f"{PAN.api_base}/checkout/pub/orderForms/simulation?sc=1"
EXITO_SKU = "102058783"
EXITO_FETCH = f"{EXITO.api_base}/catalog_system/pub/products/search?fq=skuId%3A{EXITO_SKU}"


def jresp(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status, "https://x/", {}, json.dumps(payload).encode(), 3)


@dataclass
class Clock:
    t: float = 1000.0
    slept: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def pan_detail(price: int, list_price: int) -> HttpResponse:
    data: list[dict[str, Any]] = json.loads(load_fixture("vtex", "panamericana_ficha_659232").body)
    co = data[0]["items"][0]["sellers"][0]["commertialOffer"]
    co["Price"], co["ListPrice"], co["PriceWithoutDiscount"] = price, list_price, list_price
    return jresp(data)


def simulation(cents: int) -> HttpResponse:
    return jresp(
        {
            "items": [
                {"id": PAN_SKU, "sellingPrice": cents, "availability": "available", "seller": "1"}
            ]
        }
    )


def tg_update(update_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 900 + update_id,
            "from": {"id": OWNER},
            "chat": {"id": OWNER},
            "text": text,
        },
    }


def telegram(updates: Sequence[dict[str, object]] = ()) -> FakeHttp:
    batches = [jresp({"ok": True, "result": list(updates)})] if updates else []
    return FakeHttp(
        {
            f"{TG}/getUpdates": [*batches, jresp({"ok": True, "result": []})],
            f"{TG}/sendMessage": jresp({"ok": True, "result": {"message_id": 1}}),
            f"{TG}/answerCallbackQuery": jresp({"ok": True, "result": True}),
        }
    )


def sent(tg: FakeHttp) -> list[str]:
    return [
        str(body["text"])
        for _, url, body in tg.calls
        if url.endswith("/sendMessage") and body is not None
    ]


def deps(
    conn: psycopg.Connection,
    http: Mapping[str, HttpClient],
    tg: FakeHttp,
    clock: Clock | None = None,
) -> CycleDeps:
    clock = clock or Clock()
    settings = Settings(
        database_url=SecretStr("postgresql://postgres:postgres@localhost:5432/postgres"),
        telegram_bot_token=SecretStr(TOKEN),
        telegram_chat_id=OWNER,
        telegram_owner_id=OWNER,
    )
    return CycleDeps(
        conn=conn,
        sources=load_all(),
        http=http,
        telegram=TelegramClient(SecretStr(TOKEN), tg),
        params=load_params(),
        canaries=load_canaries(),
        settings=settings,
        holder="local:test",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        rnd=random.Random(7),
        healthchecks=Healthchecks(None, tg),
    )


@pytest.fixture
def conn(db: psycopg.Connection) -> Iterator[psycopg.Connection]:  # noqa: F811
    # Canarios al día: las pruebas no los ejercitan.
    db.execute("UPDATE discountz.source SET canary_checked_at = %s, canary_ok = true", (T,))
    # Mantenimiento y resumen diario ya hechos hoy: tienen sus propias pruebas.
    db.execute(
        "UPDATE discountz.bot_state SET last_maintenance_on = %s, last_summary_on = %s",
        (T.date(), T.date()),
    )
    yield db


def seed(
    conn: psycopg.Connection,
    source: str,
    sku: str,
    price: int,
    list_price: int | None,
    *,
    days: int = 90,
    category: str = "tecnologia",
) -> int:
    """Listing seguido con un segmento estable de `days` días que llega hasta T − 40 min."""
    start = T - timedelta(days=days)
    o = offer(source=source, external_id=sku, price=price, list_price=list_price, observed_at=start)
    lid = repo.upsert_listing(conn, o, category, f"https://www.{source}.com/x/p", start).id
    conn.execute(
        """INSERT INTO discountz.price_segment (listing_id, started_at, last_seen_at, price_minor,
               list_price_minor, in_stock, seller_class, unit_key, status, sample_count)
           VALUES (%s, %s, %s, %s, %s, true, 'f', 'un|1', 'o', 2000)""",
        (lid, start, T - timedelta(minutes=40), price, list_price),
    )
    conn.execute(
        "UPDATE discountz.listing SET last_seen_at = %s WHERE id = %s",
        (T - timedelta(minutes=40), lid),
    )
    repo.create_watch_on_listing(conn, source, lid, None, start)
    return lid


def statuses(conn: psycopg.Connection, lid: int) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT price_minor, status FROM discountz.price_segment WHERE listing_id = %s"
        " ORDER BY started_at",
        (lid,),
    ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


def test_new_follow_creates_listing_and_replies(conn: psycopg.Connection) -> None:
    url = "https://www.panamericana.com.co/datafono-bold-plus-659232/p"
    tg = telegram([tg_update(1, f"/seguir {url}")])
    ficha = load_fixture("vtex", "panamericana_ficha_659232")
    http = FakeHttp(
        {
            f"{PAN.api_base}/catalog_system/pub/products/search/datafono-bold-plus-659232/p": ficha,
            PAN_FETCH: ficha,
        }
    )
    result = run_cycle(deps(conn, {"panamericana": http}, tg), T)
    assert (result.status, result.exit_code) == ("ok", 0)
    w = conn.execute("SELECT status, listing_id FROM discountz.watch").fetchone()
    assert w is not None and w[0] == "active" and w[1] is not None
    texts = sent(tg)
    assert len(texts) == 2
    assert texts[0].startswith("p1: recibido")
    assert "Siguiendo p1: Datáfono Bold Plus · $149.000 · en stock" in texts[1]


def test_price_error_confirmed_by_simulation_sends_once(conn: psycopg.Connection) -> None:
    lid = seed(conn, "panamericana", PAN_SKU, 2_299_000, 2_499_000)
    tg = telegram()
    http = FakeHttp({PAN_FETCH: pan_detail(229_900, 2_499_000), PAN_SIM: simulation(22_990_000)})
    result = run_cycle(deps(conn, {"panamericana": http}, tg), T)
    assert result.status == "ok"
    alerts = conn.execute("SELECT kind, delivery FROM discountz.alert").fetchall()
    assert alerts == [("price_error", "queued")]
    assert statuses(conn, lid)[-1] == (229_900, "g")
    (text,) = sent(tg)
    assert text.startswith("<b>POSIBLE ERROR DE PRECIO")


def test_price_error_simulation_mismatch_no_alert(conn: psycopg.Connection) -> None:
    lid = seed(conn, "panamericana", PAN_SKU, 2_299_000, 2_499_000)
    tg = telegram()
    http = FakeHttp({PAN_FETCH: pan_detail(229_900, 2_499_000), PAN_SIM: simulation(229_900_000)})
    run_cycle(deps(conn, {"panamericana": http}, tg), T)
    assert conn.execute("SELECT count(*) FROM discountz.alert").fetchone() == (0,)
    assert statuses(conn, lid)[-2:] == [(229_900, "s"), (2_299_000, "o")]
    mismatch = conn.execute(
        "SELECT sum(confirm_mismatch) FROM discountz.source_hourly_stat"
        " WHERE source = 'panamericana'"
    ).fetchone()
    assert mismatch == (1,)
    assert sent(tg) == []


class Die(BaseException):
    """El job muere (timeout de Actions): no lo atrapa ningún `except Exception`."""


def test_crash_after_commit_before_send_delivers_once(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(conn, "panamericana", PAN_SKU, 2_299_000, 2_499_000)
    tg = telegram()
    http = FakeHttp({PAN_FETCH: pan_detail(229_900, 2_499_000), PAN_SIM: simulation(22_990_000)})

    def die(*args: object, **kwargs: object) -> int:
        raise Die

    with monkeypatch.context() as m:
        m.setattr(cycle_module, "compose_alerts", die)
        with pytest.raises(Die):
            run_cycle(deps(conn, {"panamericana": http}, tg), T)
    assert conn.execute("SELECT delivery FROM discountz.alert").fetchall() == [("new",)]
    assert sent(tg) == []
    conn.execute(
        "UPDATE discountz.run_lease SET expires_at = now() - interval '1 second'"
        " WHERE holder IS NOT NULL"
    )
    later = T + timedelta(minutes=20)
    run_cycle(deps(conn, {"panamericana": http}, tg), later)
    run_cycle(deps(conn, {"panamericana": http}, tg), later + timedelta(minutes=15))
    texts = sent(tg)
    assert len(texts) == 1
    assert texts[0].startswith("<b>POSIBLE ERROR DE PRECIO")


def test_source_failure_marks_partial_and_others_continue(conn: psycopg.Connection) -> None:
    seed(conn, "exito", EXITO_SKU, 1_100_000, None)
    seed(conn, "panamericana", PAN_SKU, 2_299_000, 2_499_000)
    tg = telegram()
    http: dict[str, HttpClient] = {
        "exito": FakeHttp({EXITO_FETCH: HttpResponse(403, "https://x/", {}, b"Forbidden", 1)}),
        "panamericana": FakeHttp(
            {PAN_FETCH: pan_detail(229_900, 2_499_000), PAN_SIM: simulation(22_990_000)}
        ),
    }
    result = run_cycle(deps(conn, http, tg), T)
    assert (result.status, result.exit_code) == ("partial", 0)
    assert repo.load_breakers(conn)["exito"].state == "open"
    texts = sent(tg)
    assert any(t.startswith("<b>POSIBLE ERROR DE PRECIO") for t in texts)
    assert any("exito" in t and "bloque" in t for t in texts)


def test_price_drop_marks_hot_and_next_plan_prioritizes_it(conn: psycopg.Connection) -> None:
    lid = seed(conn, "exito", EXITO_SKU, 1_100_000, None, days=30)
    tg = telegram()
    http = FakeHttp({EXITO_FETCH: load_fixture("vtex", "exito_ficha_102058783")})
    run_cycle(deps(conn, {"exito": http}, tg), T)
    hot = conn.execute("SELECT hot_until FROM discountz.listing WHERE id = %s", (lid,)).fetchone()
    assert hot == (T + timedelta(hours=2),)
    params = load_params()
    later = T + timedelta(minutes=5)
    inp = repo.load_plan_input(conn, later, params.budgets, params.events, timedelta(minutes=15))
    plan = plan_run(inp, later)
    assert plan["exito"][0].kind == "hot"
    assert lid in plan["exito"][0].payload_ids


def test_lease_taken_returns_skipped_locked(conn: psycopg.Connection) -> None:
    seed(conn, "panamericana", PAN_SKU, 2_299_000, 2_499_000)
    assert repo.acquire_lease(conn, "gh:1:1")
    tg = telegram()
    http = FakeHttp({})
    result = run_cycle(deps(conn, {"panamericana": http}, tg), T)
    assert (result.status, result.exit_code) == ("skipped_locked", 0)
    assert http.calls == []
    assert tg.calls == []
    assert conn.execute("SELECT count(*) FROM discountz.price_segment").fetchone() == (1,)


def test_logs_contain_no_secrets(
    conn: psycopg.Connection, capfd: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    try:
        seed(conn, "exito", EXITO_SKU, 1_100_000, None)
        seed(conn, "panamericana", PAN_SKU, 2_299_000, 2_499_000)
        tg = telegram()
        http: dict[str, HttpClient] = {
            "exito": FakeHttp({EXITO_FETCH: HttpResponse(403, "https://x/", {}, b"no", 1)}),
            "panamericana": FakeHttp(
                {PAN_FETCH: pan_detail(229_900, 2_499_000), PAN_SIM: simulation(22_990_000)}
            ),
        }
        run_cycle(deps(conn, http, tg), T)
        logging.getLogger("discountz").error("prueba %s", f"{TG}/sendMessage")
    finally:
        logging.getLogger().handlers.clear()
    out = capfd.readouterr()
    text = out.out + out.err
    assert "cycle_finished" in text
    assert TOKEN not in text
    assert "postgres:postgres@" not in text
    assert "Datáfono" not in text and "panamericana.com.co" not in text


# ───────────── Slickdeals (Task 24): U34 y U34b de punta a punta

LEAD_ASIN = "B0DHLBDSP7"
AMAZON_DP = f"https://www.amazon.com/dp/{LEAD_ASIN}"


def slickdeals_rss(*items: tuple[str, str]) -> HttpResponse:
    body = "".join(
        f"<item><title><![CDATA[{title}]]></title>"
        "<pubDate>Tue, 13 Oct 2026 12:00:00 +0000</pubDate>"
        f"<content:encoded><![CDATA[{link}]]></content:encoded></item>"
        for title, link in items
    )
    xml = (
        '<?xml version="1.0"?><rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Slickdeals</title>{body}</channel></rss>"
    )
    return HttpResponse(200, "https://slickdeals.net/newsearch.php", {}, xml.encode(), 3)


EVO_LEAD = (
    "Samsung 990 EVO 1TB NVMe SSD $74.99",
    f'<a data-product-exitWebsite="amazon.com" data-aps-asin="{LEAD_ASIN}">Amazon</a>',
)
WALMART_LEAD = (
    "Samsung 990 EVO 2TB NVMe SSD $119.99",
    '<a href="https://www.walmart.com/ip/5113175776">Walmart</a>',
)


def amazon_page(cents: int) -> HttpResponse:
    price = f"${cents // 100}.{cents % 100:02d}"
    html = f"""<span id="productTitle">Samsung 990 EVO SSD 1TB</span>
      <input id="ASIN" value="{LEAD_ASIN}"><div id="corePrice_feature_div">
      <span class="a-price"><span class="a-offscreen">{price}</span></span></div>
      <div id="availability"><span>In Stock</span></div>
      <div id="merchantInfoFeature_feature_div">
      <span class="offer-display-feature-text-message">Amazon.com</span></div>"""
    return HttpResponse(200, AMAZON_DP, {}, html.encode(), 3)


def slickdeals_search(conn: psycopg.Connection) -> None:
    conn.execute(
        """INSERT INTO discountz.search (source, query, category, next_run_at)
           VALUES ('slickdeals', '990 evo', 'componentes', %s)""",
        (T - timedelta(minutes=1),),
    )


def feed_http(amazon: FakeHttp, *items: tuple[str, str]) -> dict[str, HttpClient]:
    rss = slickdeals_rss(*items)
    return {"slickdeals": FakeHttp({RSS_URL: rss}), "amazon_us": amazon}


def test_u34_slickdeals_lead_alerts_community_deal(conn: psycopg.Connection) -> None:
    slickdeals_search(conn)
    tg = telegram()
    amazon = FakeHttp({AMAZON_DP: amazon_page(7_499)})
    result = run_cycle(deps(conn, feed_http(amazon, EVO_LEAD, WALMART_LEAD), tg), T)
    assert (result.status, result.exit_code) == ("ok", 0)
    assert [url for _, url, _ in amazon.calls] == [AMAZON_DP]  # Walmart está desactivada
    alerts = conn.execute("SELECT kind, flags FROM discountz.alert").fetchall()
    assert alerts == [("community_deal", ["community", "short_history"])]
    listing = conn.execute("SELECT source, external_id, category FROM discountz.listing")
    assert listing.fetchall() == [("amazon_us", LEAD_ASIN, "componentes")]
    (text,) = sent(tg)
    assert text.startswith("<b>OFERTA DE LA COMUNIDAD")
    run = conn.execute("SELECT last_result_count FROM discountz.search").fetchone()
    assert run == (2,)


def test_u34b_lead_price_gone_no_alert(conn: psycopg.Connection) -> None:
    slickdeals_search(conn)
    tg = telegram()
    amazon = FakeHttp({AMAZON_DP: amazon_page(8_999)})
    run_cycle(deps(conn, feed_http(amazon, EVO_LEAD), tg), T)
    assert conn.execute("SELECT count(*) FROM discountz.alert").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM discountz.listing").fetchone() == (1,)
    assert sent(tg) == []


def test_lead_skipped_when_listing_read_less_than_6h_ago(conn: psycopg.Connection) -> None:
    slickdeals_search(conn)
    o = offer(
        source="amazon_us",
        external_id=LEAD_ASIN,
        price=8_999,
        currency=Currency.USD,
        observed_at=T - timedelta(hours=5),
    )
    repo.upsert_listing(conn, o, "componentes", AMAZON_DP, T - timedelta(hours=5))
    tg = telegram()
    amazon = FakeHttp({AMAZON_DP: amazon_page(7_499)})
    run_cycle(deps(conn, feed_http(amazon, EVO_LEAD), tg), T)
    assert amazon.calls == []
    assert conn.execute("SELECT count(*) FROM discountz.alert").fetchone() == (0,)
