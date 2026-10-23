"""El ciclo completo de punta a punta: BD real, VTEX con fixtures y Telegram falso (Task 19)."""

from __future__ import annotations

import io
import json
import logging
import random
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from PIL import Image
from pydantic import SecretStr

from discountz.app import cycle as cycle_module
from discountz.app.cycle import CycleDeps, run_cycle
from discountz.config import (
    InterestsConfig,
    Settings,
    load_canaries,
    load_params,
    load_topics,
)
from discountz.domain.interests import compile_interest
from discountz.domain.money import Currency
from discountz.domain.scheduling import plan_run
from discountz.infra import db as repo
from discountz.infra.db import connect
from discountz.infra.healthchecks import Healthchecks
from discountz.infra.http import FakeHttp, HttpClient, HttpResponse
from discountz.infra.log import configure_logging
from discountz.infra.migrate import migrate
from discountz.infra.telegram import TelegramClient
from discountz.sources.dealnews import FEED_URL as DEALNEWS_FEED
from discountz.sources.fixtures import load_fixture
from discountz.sources.registry import load_all
from discountz.sources.slickdeals import RSS_URL
from discountz.sources.vtex import STORES
from tests.factories import offer
from tests.integration.conftest import connect_extra, db, empty_db_url  # noqa: F401
from tests.profiles import spec_profile
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
        topics=spec_profile(),
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


def test_cycle_delivers_card_photo_with_photo_client(conn: psycopg.Connection) -> None:
    img = "https://exitocol.vteximg.com.br/arquivos/ids/1/a.jpg"
    card = {
        "store": "exito",
        "topic": "tecnologia",
        "title": "Celular",
        "url": "https://www.exito.com/celular/p",
        "image_url": img,
        "price_minor": 59_900,
        "usual_minor": 99_900,
        "currency": "COP",
        "drop_bp": 4004,
        "min_days": 5,
        "kind": "catalog_drop",
    }
    repo.enqueue(
        conn,
        "alert",
        "<b>tarjeta</b>",
        priority=3,
        dedup_key="cat:1",
        now=T,
        ttl=timedelta(hours=24),
        topic="tecnologia",
        card=card,
    )
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (10, 120, 200)).save(buf, "JPEG")
    photos = FakeHttp({img: HttpResponse(200, img, {}, buf.getvalue(), 1)})
    tg = FakeHttp(
        {
            **telegram().routes,
            f"{TG}/sendPhoto": jresp({"ok": True, "result": {"message_id": 2}}),
        }
    )
    run_cycle(replace(deps(conn, {}, tg), photos=photos), T)
    assert [c[1] for c in photos.calls] == [img]
    assert len(tg.uploads) == 1


def test_cycle_on_previous_schema_skips_with_notice(empty_db_url: str) -> None:  # noqa: F811
    """Entre el merge y la migración el ciclo corre contra el esquema 1: avisa y no falla."""
    with connect(empty_db_url) as old:
        migrate(old, 1)
        result = run_cycle(deps(old, {}, telegram()), T)
        assert result.status == "skipped_schema"
        rows = old.execute("SELECT purpose, body_html FROM discountz.outbox").fetchall()
        assert len(rows) == 1 and "migración pendiente" in rows[0][1]


def test_cycle_delivers_channel_deal_card(conn: psycopg.Connection) -> None:
    now = datetime(2026, 10, 17, 15, 40, tzinfo=UTC)
    conn.execute(
        "UPDATE discountz.bot_state SET last_maintenance_on = %s, last_summary_on = %s",
        (now.date(), now.date()),
    )
    for i, code in enumerate(load_topics().params, start=10):
        repo.save_topic(conn, code, code, i)
    repo.save_channel_cursor(conn, "tg_cajitatech", 88176, now - timedelta(hours=1))
    page = load_fixture("telegram", "cajitatech")
    empty = load_fixture("telegram", "cajitatech-empty")
    base = "https://t.me/s/cajitatech?after="
    http = FakeHttp(
        {
            f"{base}88176": HttpResponse(200, page.url, {}, page.body, 1),
            f"{base}88180": HttpResponse(200, empty.url, {}, empty.body, 1),
        }
    )
    img = "https://cdn1.telesco.pe/file/MYCeMARoA5n3GWv0xZwGkytMIAfMBBh-2SYFJLukLehA-mDctcM7S9"
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (10, 120, 200)).save(buf, "JPEG")
    photos = _AnyPhoto(buf.getvalue())
    tg = FakeHttp(
        {
            **telegram().routes,
            f"{TG}/sendPhoto": jresp({"ok": True, "result": {"message_id": 2}}),
        }
    )
    run_cycle(replace(deps(conn, {"tg_cajitatech": http}, tg), photos=photos), now)
    assert [u for u in photos.urls if u.startswith(img)] == photos.urls and len(photos.urls) == 1
    assert len(tg.uploads) == 1  # tras el cursor solo 88177 (placa madre); 88178 y 88180: Hogar
    data, _ = tg.uploads[0]
    assert data is not None
    topic = conn.execute("SELECT topic FROM discountz.outbox WHERE purpose = 'alert'").fetchone()
    assert topic is not None
    assert data["message_thread_id"] == str(repo.topic_threads(conn)[topic[0]])
    buttons = [
        b["text"] for row in json.loads(data["reply_markup"])["inline_keyboard"] for b in row
    ]
    assert buttons == ["Ver oferta", "Ver en el canal"]
    assert repo.get_channel_cursor(conn, "tg_cajitatech") == repo.ChannelCursor(88180, now)


def test_cycle_sends_a_channel_deal_that_matches_an_interest(conn: psycopg.Connection) -> None:
    """Hogar va solo por interés: el carro rodante (88180) sale con su línea 🎯 (spec del perfil
    §3.3) y el filtro antipolvo (88178), también de Hogar, queda visto."""
    now = datetime(2026, 10, 17, 15, 40, tzinfo=UTC)
    conn.execute(
        "UPDATE discountz.bot_state SET last_maintenance_on = %s, last_summary_on = %s",
        (now.date(), now.date()),
    )
    repo.save_channel_cursor(conn, "tg_cajitatech", 88176, now - timedelta(hours=1))
    page = load_fixture("telegram", "cajitatech")
    empty = load_fixture("telegram", "cajitatech-empty")
    base = "https://t.me/s/cajitatech?after="
    http = FakeHttp(
        {
            f"{base}88176": HttpResponse(200, page.url, {}, page.body, 1),
            f"{base}88180": HttpResponse(200, empty.url, {}, empty.body, 1),
        }
    )
    cart = compile_interest("Carro rodante", "hogar", ["carro rodante"])
    d = replace(
        deps(conn, {"tg_cajitatech": http}, telegram()), interests=InterestsConfig(3, (cart,))
    )
    run_cycle(d, now)
    rows = conn.execute(
        "SELECT dedup_key, body_html FROM discountz.outbox WHERE purpose = 'alert' ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == ["canal:tg_cajitatech:88177", "canal:tg_cajitatech:88180"]
    assert "🎯 Carro rodante" in rows[1][1]


def test_cycle_marks_dealnews_feed_then_delivers_its_new_deal(conn: psycopg.Connection) -> None:
    now = datetime(2026, 10, 17, 18, 16, tzinfo=UTC)
    conn.execute(
        "UPDATE discountz.bot_state SET last_maintenance_on = %s, last_summary_on = %s",
        (now.date(), now.date()),
    )
    for i, code in enumerate(load_topics().params, start=10):
        repo.save_topic(conn, code, code, i)
    feed = load_fixture("dealnews", "amazon")
    guid = feed.body.index(b"/22285975.html?iref=rss</guid>")  # dron de US$30: Tecnología
    start = feed.body.rindex(b"<item>", 0, guid)
    end = feed.body.index(b"</item>", guid) + len(b"</item>")
    before = feed.body[:start] + feed.body[end:]  # el feed antes de la oferta 22285975
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (10, 120, 200)).save(buf, "JPEG")

    def cycle(body: bytes, at: datetime) -> tuple[FakeHttp, _AnyPhoto]:
        http = FakeHttp({DEALNEWS_FEED: HttpResponse(200, feed.url, {}, body, 1)})
        photos = _AnyPhoto(buf.getvalue())
        tg = FakeHttp(
            {
                **telegram().routes,
                f"{TG}/sendPhoto": jresp({"ok": True, "result": {"message_id": 2}}),
            }
        )
        run_cycle(replace(deps(conn, {"dealnews": http}, tg), photos=photos), at)
        return tg, photos

    tg, photos = cycle(before, now)
    assert (tg.uploads, photos.urls) == ([], [])  # primera lectura: solo marca lo que hay
    assert conn.execute("SELECT count(*) FROM discountz.outbox").fetchone() == (0,)
    tg, photos = cycle(feed.body, now + timedelta(minutes=11))
    assert len(tg.uploads) == 1 and len(photos.urls) == 1
    assert photos.urls[0].startswith("https://d.dlnws.com/")
    data, _ = tg.uploads[0]
    assert data is not None
    assert "Visto en DealNews · sin verificar" in data["caption"]
    assert data["message_thread_id"] == str(repo.topic_threads(conn)["tecnologia"])
    keyboard = json.loads(data["reply_markup"])["inline_keyboard"]
    assert [(b["text"], b["url"]) for row in keyboard for b in row] == [
        (
            "Ver en DealNews",
            "https://www.dealnews.com/1080-p-Mini-Camera-Drone-2-Pack-for-30-free-shipping-w-Prime"
            "/22285975.html?iref=rss",
        )
    ]


def test_cycle_marks_slickdeals_amazon_then_delivers_its_new_deal(
    conn: psycopg.Connection,
) -> None:
    now = datetime(2026, 10, 13, 23, 30, tzinfo=UTC)
    conn.execute(
        "UPDATE discountz.bot_state SET last_maintenance_on = %s, last_summary_on = %s",
        (now.date(), now.date()),
    )
    for i, code in enumerate(load_topics().params, start=10):
        repo.save_topic(conn, code, code, i)
    feed = load_fixture("slickdeals", "frontpage")
    guid = feed.body.index(b">thread-20153970</guid>")  # US$51: por casillero sí compensa
    start = feed.body.rindex(b"<item>", 0, guid)
    end = feed.body.index(b"</item>", guid) + len(b"</item>")
    before = feed.body[:start] + feed.body[end:]  # la portada antes del hilo 20153970
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (10, 120, 200)).save(buf, "JPEG")

    def cycle(body: bytes, at: datetime) -> tuple[FakeHttp, _AnyPhoto]:
        http = FakeHttp({RSS_URL: HttpResponse(200, feed.url, {}, body, 1)})
        photos = _AnyPhoto(buf.getvalue())
        tg = FakeHttp(
            {
                **telegram().routes,
                f"{TG}/sendPhoto": jresp({"ok": True, "result": {"message_id": 2}}),
            }
        )
        run_cycle(replace(deps(conn, {"slickdeals_amazon": http}, tg), photos=photos), at)
        return tg, photos

    tg, photos = cycle(before, now)
    assert (tg.uploads, photos.urls) == ([], [])  # primera lectura: solo marca lo que hay
    assert conn.execute("SELECT count(*) FROM discountz.outbox").fetchone() == (0,)
    tg, photos = cycle(feed.body, now + timedelta(minutes=31))  # se lee cada 30 min
    assert len(tg.uploads) == 1 and len(photos.urls) == 1
    assert photos.urls[0].startswith("https://static.slickdealscdn.com/")
    data, _ = tg.uploads[0]
    assert data is not None
    assert "Visto en Slickdeals · sin verificar" in data["caption"]
    assert data["message_thread_id"] == str(repo.topic_threads(conn)["tecnologia"])
    keyboard = json.loads(data["reply_markup"])["inline_keyboard"]
    assert [(b["text"], b["url"]) for row in keyboard for b in row] == [
        ("Ver oferta", "https://www.amazon.com/dp/B0CCP8KYGG"),
        (
            "Ver en Slickdeals",
            "https://slickdeals.net/f/20153970-50-99-8bitdo-retro-87-key-mechanical-keyboard-w-"
            "bluetooth-2-4ghz-usb-c-n-edition-at-amazon",
        ),
    ]


@dataclass
class _AnyPhoto:
    """Transporte de fotos que responde la misma imagen a cualquier URL y la registra."""

    body: bytes
    urls: list[str] = field(default_factory=list)

    def request(self, method: str, url: str, **kw: object) -> HttpResponse:
        self.urls.append(url)
        return HttpResponse(200, url, {}, self.body, 1)
