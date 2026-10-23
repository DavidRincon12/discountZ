"""Render de alertas, resumen, lista, /estado y resumen diario (spec §5.2, §5.3 y §5.5)."""

from __future__ import annotations

import dataclasses
import html as html_lib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from discountz.app.compose import PRIORITY
from discountz.app.render import (
    CASILLERO,
    MAX_VISIBLE,
    AlertView,
    Button,
    DailyView,
    FlashSeen,
    channel_card,
    held_catalog_line,
    held_channel_line,
    render_alert,
    render_catalog_alert,
    render_channel_deal,
    render_daily,
    render_health,
    render_held_digest,
    render_interests,
    render_list,
    render_storm,
    render_summary,
    reply_markup,
    visible_length,
)
from discountz.domain.community import ChannelPost
from discountz.domain.interests import compile_interest
from discountz.domain.model import AlertKind, Flag
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.db import (
    CatalogDaily,
    HeldOffer,
    SearchItem,
    SourceStatus,
    StatusSnapshot,
    WatchItem,
)

GOLDEN = Path(__file__).parent.parent / "golden"
SEEN = datetime(2026, 10, 13, 15, 2, tzinfo=UTC)  # 10:02 en Bogotá


def usd(minor: int) -> Money:
    return Money(minor, Currency.USD)


def cop(minor: int) -> Money:
    return Money(minor, Currency.COP)


def golden(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8").rstrip("\n")


U07 = AlertView(
    listing_id=1234,
    watch_id=12,
    kind=AlertKind.HISTORIC_LOW,
    category="tecnologia",
    source="amazon_us",
    title='Monitor 27" IPS 165 Hz',
    url="https://www.amazon.com/dp/B0EXAMPLE1",
    price=usd(9999),
    before=usd(12999),
    reference=usd(12999),
    reference_label="precio habitual, mediana 90 d",
    real_saving_bp=2307,
    advertised_saving_bp=3333,
    list_price=usd(14999),
    history_min=usd(12999),
    history_median=usd(12999),
    history_days=90,
    flags=frozenset({Flag.INFLATED_LIST, Flag.FLASH_SEEN}),
    observed_at=SEEN,
    flash=FlashSeen(
        price=usd(8999),
        at=datetime(2026, 9, 3, 15, 0, tzinfo=UTC),
        duration=timedelta(hours=4, minutes=46),
    ),
)

PRICE_ERROR = AlertView(
    listing_id=55,
    watch_id=None,
    kind=AlertKind.PRICE_ERROR,
    category="tecnologia",
    source="exito",
    title='Portátil 15" 16 GB',
    url="https://www.exito.com/portatil-15/p",
    price=cop(299_900),
    before=cop(2_899_900),
    reference=cop(2_899_900),
    reference_label="precio habitual, mediana 90 d",
    real_saving_bp=8965,
    advertised_saving_bp=None,
    list_price=None,
    history_min=cop(2_799_900),
    history_median=cop(2_899_900),
    history_days=75,
    flags=frozenset(),
    observed_at=SEEN,
)

TARGET = AlertView(
    listing_id=77,
    watch_id=9,
    kind=AlertKind.TARGET_REACHED,
    category="hogar",
    source="olimpica",
    title="Freidora de aire 5 L",
    url="https://www.olimpica.com/freidora/p",
    price=cop(249_900),
    before=None,
    reference=None,
    reference_label=None,
    real_saving_bp=None,
    advertised_saving_bp=None,
    list_price=None,
    history_min=cop(259_900),
    history_median=cop(289_900),
    history_days=20,
    flags=frozenset({Flag.MARKETPLACE}),
    observed_at=SEEN,
)

GOOD = AlertView(
    listing_id=88,
    watch_id=3,
    kind=AlertKind.GOOD_DEAL,
    category="hogar",
    source="carulla",
    title="Cafetera <Express> & molino",
    url="https://www.carulla.com/cafetera/p",
    price=cop(189_900),
    before=cop(189_900),  # igual al actual: se omite
    reference=cop(223_400),
    reference_label="precio habitual, mediana 90 d",
    real_saving_bp=1500,
    advertised_saving_bp=4000,
    list_price=cop(316_500),
    history_min=cop(199_900),
    history_median=cop(223_400),
    history_days=45,
    flags=frozenset({Flag.SHORT_HISTORY}),
    observed_at=SEEN,
)

COMMUNITY = AlertView(
    listing_id=99,
    watch_id=None,
    kind=AlertKind.COMMUNITY_DEAL,
    category="componentes",
    source="newegg",
    title="Samsung 990 EVO 1TB",
    url="https://www.newegg.com/p/N82E1",
    price=usd(7499),
    before=None,
    reference=None,
    reference_label=None,
    real_saving_bp=None,
    advertised_saving_bp=2500,
    list_price=usd(9999),
    history_min=None,
    history_median=None,
    history_days=0,
    flags=frozenset({Flag.COMMUNITY, Flag.UNVERIFIED}),
    observed_at=SEEN,
)


@pytest.mark.parametrize(
    ("view", "name"),
    [
        (U07, "alert_historic_low_usd.html"),
        (PRICE_ERROR, "alert_price_error_cop.html"),
        (TARGET, "alert_target_reached_cop.html"),
        (GOOD, "alert_good_deal_cop.html"),
        (COMMUNITY, "alert_community_deal_usd.html"),
    ],
)
def test_alert_matches_golden(view: AlertView, name: str) -> None:
    assert render_alert(view).html == golden(name)


def test_historic_low_with_short_coverage_names_the_days() -> None:
    html = render_alert(dataclasses.replace(U07, history_days=45)).html
    assert html.startswith("<b>MÍNIMO EN 45 DÍAS DE HISTÓRICO</b>")


def test_buttons_for_followed_listing() -> None:
    msg = render_alert(U07)
    assert msg.buttons == [
        [Button("Abrir en Amazon US", url="https://www.amazon.com/dp/B0EXAMPLE1")],
        [
            Button("Silenciar 30 d", callback_data="mute:L1234:30"),
            Button("Dejar de seguir", callback_data="unf:p12"),
        ],
    ]
    assert reply_markup(msg.buttons) == {
        "inline_keyboard": [
            [{"text": "Abrir en Amazon US", "url": "https://www.amazon.com/dp/B0EXAMPLE1"}],
            [
                {"text": "Silenciar 30 d", "callback_data": "mute:L1234:30"},
                {"text": "Dejar de seguir", "callback_data": "unf:p12"},
            ],
        ]
    }


def test_discovered_listing_offers_follow_button() -> None:
    msg = render_alert(PRICE_ERROR)
    assert msg.buttons[1][1] == Button("Seguir", callback_data="fol:L55")
    for row in msg.buttons:
        for b in row:
            assert b.callback_data is None or len(b.callback_data.encode()) <= 64


def test_hostile_title_is_escaped() -> None:
    html = render_alert(dataclasses.replace(GOOD, title='<script>&"x"</script>')).html
    assert "<script>" not in html
    assert "&lt;script&gt;&amp;&quot;x&quot;&lt;/script&gt;" in html


def test_long_title_is_truncated_and_visible_text_fits() -> None:
    msg = render_alert(dataclasses.replace(U07, title="A" * 300))
    assert "A" * 119 + "…" in msg.html
    assert "A" * 120 not in msg.html
    assert visible_length(msg.html) <= MAX_VISIBLE


def test_usd_alert_says_casillero_and_cop_does_not() -> None:
    assert "🇺🇸 Compra por casillero" in render_alert(U07).html
    assert "casillero" not in render_alert(GOOD).html
    for word in ("puesto", "TRM", "≈"):
        assert word not in render_alert(U07).html


def test_summary_matches_golden_and_is_silent() -> None:
    items = [
        dataclasses.replace(
            COMMUNITY,
            kind=AlertKind.GOOD_DEAL,
            url="https://www.amazon.com/dp/B0EXAMPLE2",
            source="amazon_us",
            real_saving_bp=1800,
        ),
        dataclasses.replace(
            GOOD,
            category="componentes",
            title="SSD Kingston 1TB",
            url="https://www.exito.com/ssd-kingston-1tb/p",
            source="exito",
            price=cop(289_900),
            real_saving_bp=1250,
            advertised_saving_bp=None,
        ),
        dataclasses.replace(
            COMMUNITY,
            kind=AlertKind.GOOD_DEAL,
            title="RAM 32 GB & RGB",
            url="https://www.newegg.com/p/N82E1?a=1&b=2",
            price=usd(8999),
            real_saving_bp=1000,
            advertised_saving_bp=3000,
        ),
    ]
    msg = render_summary(items)
    assert msg.html == golden("summary_componentes.html")
    assert msg.silent
    assert msg.buttons == []


def test_summary_caps_visible_length() -> None:
    items = [dataclasses.replace(GOOD, category="hogar", title="T" * 120)] * 80
    msg = render_summary(items)
    assert visible_length(msg.html) <= MAX_VISIBLE
    assert "y " in msg.html.splitlines()[-1] and "más" in msg.html.splitlines()[-1]


def test_summary_rejects_empty() -> None:
    with pytest.raises(ValueError, match="vacío"):
        render_summary([])


def _watch(i: int, cat: str, **kw: object) -> WatchItem:
    base: dict[str, object] = {
        "id": i,
        "source": "exito",
        "title": f"SSD {i}",
        "category": cat,
        "price": cop(289_900),
        "target": cop(250_000),
        "pending": False,
        "auto": False,
    }
    base.update(kw)
    return WatchItem(**base)  # type: ignore[arg-type]


def test_list_groups_by_category_and_paginates_15() -> None:
    watches = [_watch(i, "componentes") for i in range(1, 18)] + [
        _watch(30, "hogar", title="Licuadora <x>", target=None, price=None, pending=True)
    ]
    searches = [SearchItem(3, "exito", "ssd 1tb", "componentes", enabled=True)]
    msgs = render_list(watches, searches)
    assert len(msgs) == 2
    first = msgs[0].html
    assert first.startswith("<b>#componentes</b>")
    assert "p1 · SSD 1 · Éxito · $289.900 · obj $250.000" in first
    rest = msgs[1].html
    assert "b3 · exito · «ssd 1tb»" in rest
    assert "<b>#hogar</b>" in rest
    assert "p30 · Licuadora &lt;x&gt; · Éxito · pendiente de primera lectura" in rest
    assert sum(m.html.count("\np") + m.html.count("\nb") for m in msgs) == 19


def test_list_marks_auto_watches() -> None:
    (msg,) = render_list([_watch(12, "tecnologia", auto=True), _watch(13, "tecnologia")], [])
    assert "a12 · SSD 12" in msg.html
    assert "p13 · SSD 13" in msg.html


def test_empty_list_explains_how_to_start() -> None:
    (msg,) = render_list([], [])
    assert "/seguir" in msg.html


NOW = datetime(2026, 10, 13, 15, 42, tzinfo=UTC)  # 10:42 en Bogotá


def test_health_renders_pre_table() -> None:
    snap = StatusSnapshot(
        last_run_started_at=NOW - timedelta(minutes=10, seconds=12),
        last_run_finished_at=NOW - timedelta(minutes=7),
        last_run_status="ok",
        gap_p50_s=14 * 60,
        gap_p95_s=31 * 60,
        sources=[
            SourceStatus("exito", True, "healthy", "closed", None, 0, 212, 214),
            SourceStatus(
                "amazon_us", True, "blocked", "open", NOW + timedelta(minutes=48), 2, 41, 60
            ),
            SourceStatus("walmart_us", False, "healthy", "closed", None, 0, 0, 0),
        ],
        db_bytes=142 * 1024 * 1024,
        overdue=3,
    )
    html = render_health(snap, NOW).html
    assert html.startswith("<pre>discountZ · 13-oct 10:42\n")
    assert html.endswith("</pre>")
    assert (
        "Último ciclo: hace 7 min (ok, 3 m 12 s) · intervalo entre ciclos p50 14 min / p95 31 min"
        in html
    )
    assert "exito         sana       212/214" in html
    assert "abierto hasta 11:30 (2.º)" in html
    assert "walmart_us    apagada" in html
    assert "BD 142 MB de 500 · Atrasados &gt; 1 h: 3" in html


def test_health_without_runs() -> None:
    snap = StatusSnapshot(None, None, None, None, None, [], 0, 0)
    assert "Último ciclo: ninguno" in render_health(snap, NOW).html


def test_daily_summary_line() -> None:
    view = DailyView(
        cycles=92,
        last_cycle_at=datetime(2026, 10, 13, 12, 58, tzinfo=UTC),
        alerts=6,
        source_notes=("exito sana", "amazon_us 2 bloqueos (recuperada)"),
        db_bytes=41 * 1024 * 1024,
    )
    msg = render_daily(view)
    assert msg.html == (
        "discountZ: 92 ciclos en 24 h (último 07:58) · 6 alertas · "
        "exito sana, amazon_us 2 bloqueos (recuperada) · BD 41 MB de 500"
    )
    assert msg.silent


def test_daily_summary_catalog_lines() -> None:
    view = DailyView(
        cycles=92,
        last_cycle_at=None,
        alerts=6,
        source_notes=(),
        db_bytes=41 * 1024 * 1024,
        catalog=CatalogDaily(
            items_by_source={"exito": 305_000, "carulla": 126_000},
            changes_today=1_234,
            alerts_by_topic={"tecnologia": 3, "hogar": 1},
            last_sweep_s_by_source={"exito": 18_720},
        ),
    )
    html = render_daily(view).html
    assert "\nCatálogo: Carulla 126.000, Éxito 305.000 (vuelta 5 h 12 min)" in html
    assert "1.234 cambios hoy" in html
    assert "alertas por tema: hogar 1, tecnologia 3" in html
    assert "Canales" not in html  # sin ofertas de canales no hay línea


def test_daily_summary_channel_line_without_catalog() -> None:
    view = DailyView(
        cycles=92,
        last_cycle_at=None,
        alerts=6,
        source_notes=(),
        db_bytes=41 * 1024 * 1024,
        catalog=CatalogDaily(
            items_by_source={},
            changes_today=0,
            alerts_by_topic={},
            last_sweep_s_by_source={},
            channel_posts=1_200,
            channel_duplicates=35,
        ),
    )
    html = render_daily(view).html
    assert html.endswith("\nCanales: 1.200 ofertas (35 repetidas)")
    assert "Catálogo" not in html


CARD_C01 = {
    "store": "exito",
    "topic": "tecnologia",
    "title": "Celular <Moto> G85 256 GB",
    "url": "https://www.exito.com/celular-moto-g85/p",
    "image_url": "https://exitocol.vteximg.com.br/arquivos/ids/1/a.jpg",
    "price_minor": 59_900,
    "usual_minor": 99_900,
    "currency": "COP",
    "drop_bp": 4004,
    "min_days": 5,
    "kind": "catalog_drop",
}


def test_catalog_alert_caption() -> None:
    msg = render_catalog_alert(CARD_C01, 42, "📱 Tecnología")
    lines = msg.html.split("\n")
    assert lines[0] == "<b>BAJÓ DE PRECIO</b> · #tecnologia · Éxito"
    assert lines[1] == "<b>Celular &lt;Moto&gt; G85 256 GB</b>"
    assert "<b>$59.900</b>  antes <s>$99.900</s> (-40,0 %)" in lines
    assert "Mínimo en 5 días" in lines
    assert lines[-1] == "<i>📱 Tecnología · L42</i>"
    assert visible_length(msg.html) <= 1024
    buttons = reply_markup(msg.buttons)["inline_keyboard"]
    assert buttons[0][0] == {"text": "Abrir en Éxito", "url": CARD_C01["url"]}
    assert buttons[1][1] == {"text": "Seguir", "callback_data": "fol:L42"}


def test_catalog_alert_with_interest_matches_golden() -> None:
    card = CARD_C01 | {
        "topic": "hogar",
        "title": "Silla Ergonómica de Oficina",
        "interest": "Silla de oficina",
    }
    msg = render_catalog_alert(card, 42, "🛋️ Hogar y muebles")
    assert msg.html == golden("alert_interest_cop.html")
    escaped = render_catalog_alert(card | {"interest": "Silla <gamer>"}, 42, "🛋️ Hogar y muebles")
    assert escaped.html.split("\n")[2] == "🎯 Silla &lt;gamer&gt;"
    assert "🎯" not in render_catalog_alert(CARD_C01, 42, "📱 Tecnología").html


def test_catalog_price_error_caption_usd() -> None:
    card = CARD_C01 | {
        "store": "bestbuy_us",
        "kind": "price_error",
        "currency": "USD",
        "price_minor": 1_999,
        "usual_minor": 19_999,
        "drop_bp": 9000,
        "title": "x" * 2000,
    }
    msg = render_catalog_alert(card, 7, "📱 Tecnología")
    assert msg.html.startswith("<b>POSIBLE ERROR DE PRECIO: verifica antes de pagar</b>")
    assert "<i>La tienda puede anular el pedido.</i>" in msg.html
    assert CASILLERO in msg.html
    assert "Mínimo en" not in msg.html
    assert visible_length(msg.html) <= 1024


def test_catalog_alert_model_low_caption() -> None:
    card = CARD_C01 | {
        "store": "ebay_us",
        "kind": "model_low",
        "currency": "USD",
        "price_minor": 8_000,
        "usual_minor": 10_000,
        "drop_bp": 2000,
        "min_days": 0,
        "reference_label": "Mediana de 5 publicaciones del mismo modelo",
        "note": "vendedor 99,6 % (12.345) · incluye envío a Miami US$7,58",
        "peers": 5,
    }
    msg = render_catalog_alert(card, 9, "📱 Tecnología")
    lines = msg.html.split("\n")
    assert lines[0] == "<b>MÁS BARATO QUE SU MODELO</b> · #tecnologia · eBay"
    assert (
        "<b>US$80,00</b>  mediana de 5 publicaciones del mismo modelo US$100,00 (-20,0 %)" in lines
    )
    assert "vendedor 99,6 % (12.345) · incluye envío a Miami US$7,58" in lines
    assert "Mínimo en" not in msg.html
    assert CASILLERO in msg.html
    buttons = reply_markup(msg.buttons)["inline_keyboard"]
    assert buttons[0][0]["text"] == "Abrir en eBay"


def test_catalog_drop_keeps_minimum_line_and_adds_note() -> None:
    card = CARD_C01 | {"note": "vendedor 100 % (500)"}
    msg = render_catalog_alert(card, 9, "📱 Tecnología")
    assert "Mínimo en 5 días" in msg.html
    assert "vendedor 100 % (500)" in msg.html


def test_catalog_drop_has_header_in_text_alerts() -> None:
    view = dataclasses.replace(GOOD, kind=AlertKind.CATALOG_DROP)
    assert render_alert(view).html.startswith("<b>BAJÓ DE PRECIO</b>")


def test_compose_priority_covers_every_kind() -> None:
    assert set(PRIORITY) == set(AlertKind)


# ───────────── Ofertas de canales de Telegram (spec de canales §5)


def _channel_post(**kw: object) -> ChannelPost:
    base: dict[str, object] = {
        "channel": "tg_clubgratis",
        "post_id": 59513,
        "published_at": SEEN,
        "title": "Arcweg traje de baño para hombre 7 pulgadas",
        "price": Money(499, Currency.USD),
        "coupon": "T6YLVQZS",
        "store": "Amazon",
        "hashtags": ("banador",),
        "image_url": None,
        "link": SafeUrl.parse("https://joylink.io/clubgratis/x", frozenset({"joylink.io"})),
    }
    base.update(kw)
    return ChannelPost(**base)  # type: ignore[arg-type]


CHANNEL_COP = {
    "channel": "tg_cajitatech",
    "post_id": 88168,
    "title": '55" OLED S95H 4K Televisor Smart (2026)',
    "price": Money(4_499_910, Currency.COP),
    "coupon": None,
    "store": None,
    "link": SafeUrl.parse("https://t.me/cajitatech/88168", frozenset({"t.me"})),
}


@pytest.mark.parametrize(
    ("kw", "topic", "name"),
    [({}, "deportes", "channel_deal_usd.html"), (CHANNEL_COP, "electro", "channel_deal_cop.html")],
)
def test_render_channel_deal_matches_golden(kw: dict[str, object], topic: str, name: str) -> None:
    assert render_channel_deal(_channel_post(**kw), topic).html == golden(name)


def test_channel_deal_with_interest_matches_golden() -> None:
    post = _channel_post(
        **CHANNEL_COP | {"title": 'Monitor gamer 27" 165 Hz', "price": Money(899_900, Currency.COP)}
    )
    assert render_channel_deal(post, "tecnologia", "Monitor").html == golden(
        "channel_deal_interest.html"
    )
    assert channel_card(post, "tecnologia", "Monitor")["interest"] == "Monitor"
    assert channel_card(post, "tecnologia")["interest"] is None


def test_render_channel_deal_buttons() -> None:
    assert render_channel_deal(_channel_post(), "deportes").buttons == [
        [Button("Ver oferta", url="https://joylink.io/clubgratis/x")],
        [Button("Ver en el canal", url="https://t.me/Clubgratis/59513")],
    ]
    # Sin enlace propio (cae a la publicación): un solo botón.
    assert render_channel_deal(_channel_post(**CHANNEL_COP), "electro").buttons == [
        [Button("Ver en el canal", url="https://t.me/cajitatech/88168")]
    ]


def test_render_channel_deal_escapes_title() -> None:
    msg = render_channel_deal(_channel_post(title="Levi's <b>501</b> & Co"), "moda")
    assert "<b>Levi&#x27;s &lt;b&gt;501&lt;/b&gt; &amp; Co</b>" in msg.html
    assert "<b>501</b>" not in msg.html
    assert visible_length(msg.html) <= 1024


def test_render_channel_deal_cuts_long_title() -> None:
    msg = render_channel_deal(_channel_post(title="x" * 300), "otros")
    assert "x" * 119 + "…" in msg.html
    assert "x" * 121 not in msg.html


def test_channel_card_fields() -> None:
    card = channel_card(_channel_post(), "deportes")
    assert card == {
        "store": "tg_clubgratis",
        "topic": "deportes",
        "title": "Arcweg traje de baño para hombre 7 pulgadas",
        "url": "https://joylink.io/clubgratis/x",
        "image_url": None,
        "price_minor": 499,
        "currency": "USD",
        "kind": "community_deal",
        "coupon": "T6YLVQZS",
        "badge": "Clubgratis · sin verificar",
        "casillero": False,
        "interest": None,
    }


def test_channel_deal_says_casillero_only_when_the_post_does() -> None:
    """Las ofertas de Amazon de los canales suelen enviar directo a Colombia (David, 2026-10-22)."""
    assert CASILLERO not in render_channel_deal(_channel_post(), "deportes").html
    assert CASILLERO in render_channel_deal(_channel_post(casillero=True), "deportes").html
    assert CASILLERO in render_channel_deal(_channel_post(**SLICKDEALS), "tecnologia").html


# ───────────── Ofertas de feeds sin verificar (DealNews y Slickdeals Amazon)

DEALNEWS_URL = (
    "https://www.dealnews.com/3-in-1-Ice-Depuffing-Wand-for-45-free-shipping/22269285.html?iref=rss"
)
DEALNEWS = {
    "channel": "dealnews",
    "post_id": 22269285,
    "title": "3-in-1 Ice Depuffing Wand for $45 + free shipping",
    "price": Money(4499, Currency.USD),
    "coupon": "4IQ9VRP5",
    "hashtags": ("skin", "care"),
    "link": SafeUrl.parse(DEALNEWS_URL, frozenset({"www.dealnews.com"})),
    "post_url": SafeUrl.parse(DEALNEWS_URL, frozenset({"www.dealnews.com"})),
    "casillero": True,
}
SLICKDEALS = {
    "channel": "slickdeals_amazon",
    "post_id": 20089260,
    "title": "WayLite Magnetic up to 25W Wireless Charger w/ Kickstand $8.50",
    "price": Money(850, Currency.USD),
    "coupon": None,
    "hashtags": (),
    "link": SafeUrl.parse("https://www.amazon.com/dp/B0GX12H56R", frozenset({"www.amazon.com"})),
    "post_url": SafeUrl.parse(
        "https://slickdeals.net/f/20089260-25w-waylite", frozenset({"slickdeals.net"})
    ),
    "casillero": True,
}


def test_render_dealnews_deal_matches_golden() -> None:
    msg = render_channel_deal(_channel_post(**DEALNEWS), "belleza")
    assert msg.html == golden("feed_deal_dealnews.html")


def test_dealnews_deal_has_one_button_with_its_link_untouched() -> None:
    assert render_channel_deal(_channel_post(**DEALNEWS), "belleza").buttons == [
        [Button("Ver en DealNews", url=DEALNEWS_URL)]
    ]


def test_slickdeals_deal_links_to_amazon_and_to_the_thread() -> None:
    msg = render_channel_deal(_channel_post(**SLICKDEALS), "tecnologia")
    assert msg.html.startswith("<b>OFERTA DE SLICKDEALS</b> · #tecnologia · Amazon\n")
    assert "📣 Visto en Slickdeals · sin verificar" in msg.html
    assert msg.buttons == [
        [Button("Ver oferta", url="https://www.amazon.com/dp/B0GX12H56R")],
        [Button("Ver en Slickdeals", url="https://slickdeals.net/f/20089260-25w-waylite")],
    ]


def test_feed_card_badge_names_the_source() -> None:
    card = channel_card(_channel_post(**DEALNEWS), "belleza")
    assert (card["badge"], card["url"]) == ("DealNews · sin verificar", DEALNEWS_URL)


def test_model_low_has_header_in_text_alerts() -> None:
    view = dataclasses.replace(GOOD, kind=AlertKind.MODEL_LOW)
    assert render_alert(view).html.startswith("<b>MÁS BARATO QUE SU MODELO</b>")


def test_health_columns_stay_aligned_with_long_codes() -> None:
    """`slickdeals_amazon` y `tg_descuentoshome` (17 letras) corrían la columna de Salud."""
    sources = [
        SourceStatus("exito", True, "healthy", "closed", None, 0, 212, 214),
        SourceStatus("slickdeals_amazon", True, "healthy", "closed", None, 0, 9, 9),
        SourceStatus("tg_descuentoshome", False, "healthy", "closed", None, 0, 0, 0),
    ]
    snap = StatusSnapshot(None, None, None, None, None, sources, 0, 0)
    lines = html_lib.unescape(render_health(snap, NOW).html).splitlines()
    table = [line for line in lines if line.startswith(("Fuente", "exito", "slick", "tg_"))]
    assert len(table) == 4
    assert {line.find(line.split()[1]) for line in table} == {18}


def test_storm_message_lists_the_errors_of_one_store_and_is_loud() -> None:
    second = dataclasses.replace(PRICE_ERROR, title="Nevera & congelador", listing_id=99)
    msg = render_storm([PRICE_ERROR, second])
    assert "2 errores de precio a la vez" in msg.html
    assert "verifica" in msg.html
    assert "Nevera &amp; congelador" in msg.html
    assert not msg.silent
    assert visible_length(msg.html) <= MAX_VISIBLE


# ───────────── Resumen de la noche (ADR 0012)


def test_held_catalog_line() -> None:
    assert held_catalog_line(CARD_C01) == (
        '• <a href="https://www.exito.com/celular-moto-g85/p">Celular &lt;Moto&gt; G85 256 GB</a>'
        " · <b>$59.900</b> antes <s>$99.900</s> (-40,0 %)"
    )


def test_held_price_error_line_is_marked() -> None:
    line = held_catalog_line(CARD_C01 | {"kind": "price_error", "drop_bp": 8000})
    assert line.startswith("• 🔥 <a ")


def test_held_channel_line() -> None:
    assert held_channel_line(_channel_post()) == (
        '• <a href="https://joylink.io/clubgratis/x">Arcweg traje de baño para hombre 7 pulgadas'
        "</a> · <b>US$4,99</b> · cupón <code>T6YLVQZS</code>"
    )


def _held(source: str, drop: int | None, line: str, i: int = 0) -> HeldOffer:
    return HeldOffer(i, source, drop, line)


def test_held_digest_matches_golden() -> None:
    lunchbox = (
        '• <a href="https://www.exito.com/l/p">Lonchera Totto</a>'
        " · <b>$103.900</b> antes <s>$129.900</s> (-20,0 %)"
    )
    rows = [
        _held("exito", 4004, held_catalog_line(CARD_C01), 1),
        _held("exito", 2001, lunchbox, 2),
        _held("tg_clubgratis", None, held_channel_line(_channel_post()), 3),
    ]
    assert render_held_digest(rows, 8) == [golden("held_digest.html")]


def test_held_digest_counts_what_does_not_fit_per_source() -> None:
    rows = [_held("exito", 1000 - i, f"• oferta {i}", i) for i in range(45)]
    (body,) = render_held_digest(rows, 8)
    assert "• oferta 39" in body and "• oferta 40" not in body
    assert body.endswith("… y 5 más de Éxito")


def test_held_digest_splits_into_messages_that_fit() -> None:
    sources = ("exito", "alkosto", "jumbo")
    rows = [_held(s, 1000, f"• {'x' * 110} {s} {i}", i) for s in sources for i in range(40)]
    bodies = render_held_digest(rows, 8)
    assert len(bodies) > 1
    assert all(visible_length(b) <= MAX_VISIBLE for b in bodies)
    joined = "\n".join(bodies)
    assert all(f"jumbo {i}" in joined for i in range(40))
    assert all(b.startswith("<b>") for b in bodies[1:])  # cada uno dice de qué fuente sigue


def test_held_digest_of_one_offer_says_so_in_singular() -> None:
    (body,) = render_held_digest([_held("exito", 2000, "• x")], 8)
    assert body.startswith("🌙 <b>Resumen de la noche</b> · 1 oferta\n")


INTEREST_TITLES = {"tecnologia": "📱 Tecnología", "hogar": "🛋️ Hogar y muebles"}


def test_interest_list_groups_by_topic_and_marks_seeds() -> None:
    monitor = compile_interest("Monitor", "tecnologia", ["monitor"])
    desk = compile_interest("Escritorio <alto>", "hogar", ["escritorio"])
    mine = compile_interest("rtx 5070", "tecnologia", ["rtx 5070"], personal_id=1)
    assert render_interests([(1, monitor), (2, desk), (56, mine)], INTEREST_TITLES) == [
        "🎯 <b>Intereses</b> · 2 de la lista base (🌱) y 1 tuyo\n"
        "<i>Agrega uno: /interes frase, sinónimo… [#tema] · quita uno tuyo: "
        "/interes quitar &lt;n&gt;</i>\n"
        "\n"
        "<b>📱 Tecnología</b>\n"
        "1. 🌱 Monitor\n"
        "56. rtx 5070\n"
        "\n"
        "<b>🛋️ Hogar y muebles</b>\n"
        "2. 🌱 Escritorio &lt;alto&gt;"
    ]


def test_interest_list_splits_at_the_telegram_limit() -> None:
    seeds = [(n, compile_interest(f"Semilla {n:02}", "tecnologia", ["abc"])) for n in range(1, 56)]
    mine = [
        (55 + i, compile_interest("n" * 37 + f"{i:03}", "hogar", ["abc"], personal_id=i))
        for i in range(1, 101)
    ]
    bodies = render_interests([*seeds, *mine], INTEREST_TITLES)
    assert len(bodies) > 1
    assert all(visible_length(b) <= MAX_VISIBLE for b in bodies)
    assert all(b.startswith("<b>") for b in bodies[1:])  # cada mensaje dice de qué tema sigue
    text = "\n".join(bodies)
    assert all(f"\n{n}. " in text for n, _ in [*seeds, *mine])


def test_interest_list_without_interests() -> None:
    assert render_interests([], INTEREST_TITLES) == [
        "No hay intereses. Agrega uno: /interes frase, sinónimo… [#tema]"
    ]


def test_9to5toys_deal_credits_9to5toys_and_links_to_its_article() -> None:
    url = "https://9to5toys.com/2026/10/22/lisen-140w-cable/"
    link = SafeUrl.parse(url, frozenset({"9to5toys.com"}))
    post = _channel_post(
        **{**SLICKDEALS, "channel": "ninetofivetoys", "link": link, "post_url": link}
    )
    msg = render_channel_deal(post, "tecnologia")
    assert "Visto en 9to5Toys · sin verificar" in msg.html
    assert msg.buttons == [[Button("Ver en 9to5Toys", url=url)]]


def test_woot_card_names_woot_and_the_casillero() -> None:
    url = "https://computers.woot.com/offers/lenovo-ideapad"
    link = SafeUrl.parse(url, frozenset({"computers.woot.com"}))
    post = ChannelPost(
        channel="woot",
        post_id=1,
        published_at=datetime(2026, 10, 23, 5, tzinfo=UTC),
        title="Lenovo IdeaPad Slim 5",
        price=Money(549_99, Currency.USD),
        coupon=None,
        store="Woot",
        hashtags=("computers", "laptops"),
        image_url=None,
        link=link,
        post_url=link,
        casillero=True,
    )
    msg = render_channel_deal(post, "tecnologia")
    assert "📣 Visto en Woot · sin verificar" in msg.html
    assert CASILLERO in msg.html
    assert msg.html.startswith("<b>OFERTA DE WOOT</b> · #tecnologia · Woot")
    assert [[b.text for b in row] for row in msg.buttons] == [["Ver en Woot"]]
