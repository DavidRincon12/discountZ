"""Render de alertas, resumen, lista, /estado y resumen diario (spec §5.2, §5.3 y §5.5)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from discountz.app.render import (
    MAX_VISIBLE,
    AlertView,
    Button,
    DailyView,
    FlashSeen,
    render_alert,
    render_daily,
    render_health,
    render_list,
    render_summary,
    reply_markup,
    visible_length,
)
from discountz.domain.model import AlertKind, Flag
from discountz.domain.money import Currency, Money
from discountz.infra.db import SearchItem, SourceStatus, StatusSnapshot, WatchItem

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
