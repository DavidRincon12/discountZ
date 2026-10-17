"""Mensajes de Telegram en HTML (spec §5.2, §5.3 y §5.5). Puro: sin I/O ni reloj.

Todo texto que viene de una tienda o de David se escapa con `html.escape(…, quote=True)`.
Sin costo puesto ni TRM (decisión D10): los listings en USD dicen «🇺🇸 Compra por casillero».
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from discountz.domain.community import ChannelPost
from discountz.domain.model import AlertKind, Flag
from discountz.domain.money import Currency, Money, format_money
from discountz.infra.db import CatalogDaily, SearchItem, StatusSnapshot, WatchItem
from discountz.sources.telegram_channels import CHANNELS

MAX_VISIBLE = 4096
MAX_TITLE = 120
LIST_TITLE = 40
LIST_PAGE = 15
DB_LIMIT_MB = 500
MUTE_BUTTON_DAYS = 30
CASILLERO = "🇺🇸 Compra por casillero"
REFERENCE_LABEL = "precio habitual, mediana 90 d"
# Colombia no tiene horario de verano: UTC−5 fijo, sin depender de tzdata.
BOGOTA = timezone(timedelta(hours=-5), "COT")
_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

SOURCE_NAMES = {
    "exito": "Éxito",
    "carulla": "Carulla",
    "olimpica": "Olímpica",
    "panamericana": "Panamericana",
    "amazon_us": "Amazon US",
    "newegg": "Newegg",
    "walmart_us": "Walmart US",
    "bestbuy_us": "Best Buy US",
    "slickdeals": "Slickdeals",
    "tg_descuentostech": "DescuentosTech",
    "tg_clubgratis": "Clubgratis",
    "tg_cajitatech": "cajitatech",
}

_HEADERS = {
    AlertKind.PRICE_ERROR: "POSIBLE ERROR DE PRECIO: verifica antes de pagar",
    AlertKind.TARGET_REACHED: "PRECIO OBJETIVO ALCANZADO",
    AlertKind.GOOD_DEAL: "BUENA OFERTA",
    AlertKind.CATALOG_DROP: "BAJÓ DE PRECIO",
    AlertKind.COMMUNITY_DEAL: (
        "OFERTA DE LA COMUNIDAD (Slickdeals), sin histórico propio suficiente"
    ),
}
HISTORIC_FULL_DAYS = 60

_FLAG_TEXT = {
    Flag.INFLATED_LIST: "Tachado nunca cobrado en 90 d",
    Flag.ADVERTISED_GAP: "El descuento anunciado es muy distinto del real",
    Flag.PRE_INFLATED: "Subió de precio antes de la oferta",
    Flag.FLASH_SEEN: "Hubo precios relámpago en el histórico",
    Flag.GAP_BEFORE: "Hubo un hueco sin datos antes de este precio",
    Flag.SHORT_HISTORY: "Histórico corto: tómalo con cautela",
    Flag.MARKETPLACE: "Vendido por un tercero (marketplace)",
    Flag.TARGET_ABOVE_TYPICAL: "Tu objetivo está por encima del precio habitual",
    Flag.REF_ANTIGUA: "Referencia de más de 90 días",
    Flag.UNIT_CHANGED: "Cambió la presentación o la unidad",
    Flag.STORM: "Muchas bajadas a la vez en esta tienda",
    Flag.TARGET_ALSO: "También alcanza tu precio objetivo",
    Flag.PERSISTENCE_ONLY: "Confirmado por persistencia, sin segunda ruta",
    Flag.HOT: "Precio en movimiento: revisado con prioridad",
    Flag.COMMUNITY: "Reportada por la comunidad",
    Flag.UNVERIFIED: "Sin verificar con la tienda",
}
_HEALTH_TEXT = {"healthy": "sana", "degraded": "degradada", "blocked": "bloqueo", "broken": "rota"}
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    url: str | None = None
    callback_data: str | None = None  # ≤ 64 bytes


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    html: str
    buttons: list[list[Button]] = field(default_factory=list)
    silent: bool = False


@dataclass(frozen=True, slots=True)
class FlashSeen:
    price: Money
    at: datetime
    duration: timedelta


@dataclass(frozen=True, slots=True)
class AlertView:
    listing_id: int
    watch_id: int | None  # None = listing descubierto (botón «Seguir»)
    kind: AlertKind
    category: str
    source: str
    title: str
    url: str
    price: Money
    before: Money | None  # `listing.last_price_minor` antes de actualizarse
    reference: Money | None
    reference_label: str | None
    real_saving_bp: int | None
    advertised_saving_bp: int | None
    list_price: Money | None
    history_min: Money | None
    history_median: Money | None
    history_days: int
    flags: frozenset[Flag]
    observed_at: datetime
    flash: FlashSeen | None = None


@dataclass(frozen=True, slots=True)
class DailyView:
    cycles: int
    last_cycle_at: datetime | None
    alerts: int
    source_notes: Sequence[str]
    db_bytes: int
    by_trigger: Mapping[str, int] = field(default_factory=dict)  # trigger_kind → ciclos
    warnings: Sequence[str] = ()
    catalog: CatalogDaily | None = None


_TRIGGERS = (
    ("workflow_dispatch", "por disparo externo"),
    ("schedule", "por schedule"),
    ("local", "locales"),
)


def visible_length(body: str) -> int:
    """Caracteres que ve David: sin etiquetas y con las entidades resueltas."""
    return len(html.unescape(_TAG_RE.sub("", body)))


def reply_markup(buttons: Sequence[Sequence[Button]]) -> dict[str, Any]:
    rows = [
        [
            {
                k: v
                for k, v in (("text", b.text), ("url", b.url), ("callback_data", b.callback_data))
                if v
            }
            for b in row
        ]
        for row in buttons
    ]
    return {"inline_keyboard": rows}


def source_name(code: str) -> str:
    return SOURCE_NAMES.get(code, code)


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pct(bp: int) -> str:
    """Puntos básicos a porcentaje con un decimal truncado: `2307` → `23,0 %`."""
    sign = "-" if bp < 0 else ""
    tenths = abs(bp) // 10
    return f"{sign}{tenths // 10},{tenths % 10} %"


def local(dt: datetime) -> datetime:
    return dt.astimezone(BOGOTA)


def short_date(dt: datetime) -> str:
    d = local(dt)
    return f"{d.day}-{_MONTHS[d.month - 1]}"


def _duration(d: timedelta) -> str:
    minutes = int(d.total_seconds()) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def _header(a: AlertView) -> str:
    if a.kind is AlertKind.HISTORIC_LOW:
        if a.history_days < HISTORIC_FULL_DAYS:
            return f"MÍNIMO EN {a.history_days} DÍAS DE HISTÓRICO"
        return "MÍNIMO EN 90 DÍAS"
    return _HEADERS[a.kind]


def render_alert(a: AlertView) -> RenderedMessage:
    fm = format_money
    lines = [
        f"<b>{_header(a)}</b> · #{a.category} · {esc(source_name(a.source))}",
        f"<b>{esc(truncate(a.title, MAX_TITLE))}</b>",
        "",
    ]
    price_line = f"<b>{fm(a.price)}</b>"
    if a.before is not None and a.before != a.price:
        price_line += f"  antes <s>{fm(a.before)}</s>"
    lines.append(price_line)
    if a.kind is AlertKind.PRICE_ERROR:
        lines.append("<i>La tienda puede anular el pedido.</i>")
    if a.real_saving_bp is not None and a.reference is not None:
        label = f" ({esc(a.reference_label)})" if a.reference_label else ""
        lines.append(
            f"Ahorro real: <b>{pct(a.real_saving_bp)}</b> frente a {fm(a.reference)}{label}"
        )
    shown = set()
    if a.advertised_saving_bp is not None and a.list_price is not None:
        line = f"La tienda anuncia {pct(a.advertised_saving_bp)} (tachado {fm(a.list_price)})"
        if Flag.INFLATED_LIST in a.flags:
            line += ": tachado nunca cobrado en 90 d"
            shown.add(Flag.INFLATED_LIST)
        lines.append(line)
    history = []
    if a.history_min is not None:
        history.append(f"mín {fm(a.history_min)}")
    if a.history_median is not None:
        history.append(f"mediana 90 d {fm(a.history_median)}")
    if a.history_days:
        history.append(f"{a.history_days} días con datos")
    if history:
        lines.append("Histórico: " + " · ".join(history))
    if a.flash is not None:
        lines.append(
            f"Relámpago visto: {fm(a.flash.price)} el {short_date(a.flash.at)} "
            f"(duró máx. {_duration(a.flash.duration)}), excluido del mínimo"
        )
        shown.add(Flag.FLASH_SEEN)
    lines.extend(f"<i>{_FLAG_TEXT[f]}</i>" for f in Flag if f in a.flags and f not in shown)
    if a.price.currency is Currency.USD:
        lines.append(CASILLERO)
    lines.append(f"<i>Visto {local(a.observed_at):%H:%M} (hora Col.) · L{a.listing_id}</i>")
    return RenderedMessage("\n".join(lines), _alert_buttons(a))


CAPTION_MAX = 1024  # límite de `caption` en sendPhoto
CAPTION_TITLE = 200


def render_catalog_alert(
    card: Mapping[str, Any], listing_id: int, topic_title: str
) -> RenderedMessage:
    """Pie de foto de una alerta del catálogo (spec del rastreo §5.3); cabe en 1.024."""
    kind = AlertKind(str(card["kind"]))
    currency = Currency(str(card["currency"]))
    price = Money(int(card["price_minor"]), currency)
    usual = Money(int(card["usual_minor"]), currency)
    store = str(card["store"])
    lines = [
        f"<b>{_HEADERS[kind]}</b> · #{card['topic']} · {esc(source_name(store))}",
        f"<b>{esc(truncate(str(card['title']), CAPTION_TITLE))}</b>",
        "",
        f"<b>{format_money(price)}</b>  antes <s>{format_money(usual)}</s>"
        f" (-{pct(int(card['drop_bp']))})",
    ]
    if kind is AlertKind.PRICE_ERROR:
        lines.append("<i>La tienda puede anular el pedido.</i>")
    else:
        lines.append(f"Mínimo en {int(card['min_days'])} días")
    if currency is Currency.USD:
        lines.append(CASILLERO)
    lines.append(f"<i>{esc(topic_title)} · L{listing_id}</i>")
    buttons = [
        [Button(f"Abrir en {source_name(store)}", url=str(card["url"]))],
        [
            Button(f"Silenciar {MUTE_BUTTON_DAYS} d", callback_data=f"mute:L{listing_id}:30"),
            Button("Seguir", callback_data=f"fol:L{listing_id}"),
        ],
    ]
    return RenderedMessage("\n".join(lines), buttons)


CHANNEL_TITLE = 120  # spec de canales §5


def _channel_post_url(post: ChannelPost) -> str:
    return f"https://t.me/{CHANNELS.get(post.channel, post.channel)}/{post.post_id}"


def channel_card(post: ChannelPost, topic: str) -> dict[str, Any]:
    """Datos de la tarjeta `community` de una oferta de canal (se guardan en `outbox.card`)."""
    return {
        "store": post.channel,
        "topic": topic,
        "title": post.title,
        "url": str(post.link),
        "image_url": str(post.image_url) if post.image_url is not None else None,
        "price_minor": post.price.minor,
        "currency": post.price.currency.value,
        "kind": AlertKind.COMMUNITY_DEAL.value,
        "coupon": post.coupon,
        "badge": f"{source_name(post.channel)} · sin verificar",
    }


def render_channel_deal(post: ChannelPost, topic: str) -> RenderedMessage:
    """Pie de foto de una oferta de canal (spec de canales §5); cabe en 1.024."""
    head = f"<b>OFERTA DE CANAL</b> · #{topic}"
    if post.store:
        head += f" · {esc(post.store)}"
    lines = [
        head,
        f"<b>{esc(truncate(post.title, CHANNEL_TITLE))}</b>",
        "",
        f"<b>{format_money(post.price)}</b>",
    ]
    if post.coupon:
        lines.append(f"Cupón <code>{esc(post.coupon)}</code>")
    lines.append(f"📣 Visto en {esc(source_name(post.channel))} · sin verificar")
    if post.price.currency is Currency.USD:
        lines.append(CASILLERO)
    in_channel = _channel_post_url(post)
    buttons = [[Button("Ver en el canal", url=in_channel)]]
    if str(post.link) != in_channel:
        buttons.insert(0, [Button("Ver oferta", url=str(post.link))])
    return RenderedMessage("\n".join(lines), buttons)


def _alert_buttons(a: AlertView) -> list[list[Button]]:
    second = (
        Button("Dejar de seguir", callback_data=f"unf:p{a.watch_id}")
        if a.watch_id is not None
        else Button("Seguir", callback_data=f"fol:L{a.listing_id}")
    )
    return [
        [Button(f"Abrir en {source_name(a.source)}", url=a.url)],
        [
            Button(f"Silenciar {MUTE_BUTTON_DAYS} d", callback_data=f"mute:L{a.listing_id}:30"),
            second,
        ],
    ]


def render_summary(items: Sequence[AlertView]) -> RenderedMessage:
    """Un solo mensaje silencioso por categoría (spec §5.2), recortado a 4.096 visibles."""
    if not items:
        raise ValueError("resumen vacío")
    lines = [f"<b>#{items[0].category} · {len(items)} buenas ofertas</b>"]
    reserve = len("\n… y 999 más")
    for i, a in enumerate(items, start=1):
        link = f'<a href="{esc(a.url)}">{esc(truncate(a.title, MAX_TITLE))}</a>'
        line = f"{i}. {link} · {format_money(a.price)}"
        if a.real_saving_bp is not None:
            line += f" · real {pct(a.real_saving_bp)}"
            if a.advertised_saving_bp is not None:
                line += f" (anuncia {pct(a.advertised_saving_bp)})"
        if a.price.currency is Currency.USD:
            line += f" · {CASILLERO}"
        if visible_length("\n".join([*lines, line])) + reserve > MAX_VISIBLE:
            lines.append(f"… y {len(items) - i + 1} más")
            break
        lines.append(line)
    return RenderedMessage("\n".join(lines), [], silent=True)


def render_list(
    watches: Sequence[WatchItem], searches: Sequence[SearchItem]
) -> list[RenderedMessage]:
    """`/lista`: agrupada por categoría, 15 ítems por mensaje (spec §5.3)."""
    if not watches and not searches:
        return [RenderedMessage("No sigues nada todavía. Usa /seguir &lt;url&gt; o /buscar.")]
    groups: dict[str, list[str]] = {}
    for w in watches:
        groups.setdefault(w.category, []).append(_watch_line(w))
    for s in searches:
        line = f"b{s.id} · {esc(s.source)} · «{esc(s.query)}»" + ("" if s.enabled else " · pausada")
        groups.setdefault(s.category, []).append(line)
    pages: list[list[str]] = []
    page: list[str] = []
    count = 0
    for category in sorted(groups):
        page.append(f"<b>#{category}</b>")
        for line in groups[category]:
            if count == LIST_PAGE:
                pages.append(page)
                page, count = [f"<b>#{category}</b>"], 0
            page.append(line)
            count += 1
    pages.append(page)
    return [RenderedMessage("\n".join(p)) for p in pages]


def _watch_line(w: WatchItem) -> str:
    ref = f"{'a' if w.auto else 'p'}{w.id}"
    parts = [ref, esc(truncate(w.title, LIST_TITLE)), esc(source_name(w.source))]
    if w.pending:
        parts.append("pendiente de primera lectura")
    else:
        parts.append(format_money(w.price) if w.price is not None else "sin precio")
        if w.target is not None:
            parts.append(f"obj {format_money(w.target)}")
    return " · ".join(parts)


def _mb(n: int) -> int:
    return n // (1024 * 1024)


def _span(d: timedelta) -> str:
    total = int(d.total_seconds())
    minutes, seconds = divmod(total, 60)
    return f"{minutes} m {seconds} s"


def render_health(s: StatusSnapshot, now: datetime) -> RenderedMessage:
    """`/estado` en `<pre>` (spec §5.3)."""
    n = local(now)
    lines = [f"discountZ · {n.day}-{_MONTHS[n.month - 1]} {n:%H:%M}"]
    if s.last_run_finished_at is None or s.last_run_started_at is None:
        lines.append("Último ciclo: ninguno")
    else:
        ago = int((now - s.last_run_finished_at).total_seconds()) // 60
        took = _span(s.last_run_finished_at - s.last_run_started_at)
        line = f"Último ciclo: hace {ago} min ({s.last_run_status}, {took})"
        if s.gap_p50_s is not None and s.gap_p95_s is not None:
            line += (
                f" · intervalo entre ciclos p50 {s.gap_p50_s // 60} min"
                f" / p95 {s.gap_p95_s // 60} min"
            )
        lines.append(line)
    lines.append(f"{'Fuente':<14}{'Salud':<11}{'Éxito 24h':<11}{'Uso 24h':<11}Breaker")
    for src in s.sources:
        if not src.enabled:
            lines.append(f"{src.code:<14}{'apagada':<11}{'—':<11}{'—':<11}—")
            continue
        if src.breaker == "open" and src.breaker_open_until is not None:
            if now >= src.breaker_open_until:
                breaker = "semiabierto"
            else:
                breaker = (
                    f"abierto hasta {local(src.breaker_open_until):%H:%M} ({src.breaker_trips}.º)"
                )
        else:
            breaker = "cerrado"
        health = _HEALTH_TEXT.get(src.health, src.health)
        ratio = f"{src.ok_24h}/{src.requests_24h}"
        lines.append(f"{src.code:<14}{health:<11}{ratio:<11}{src.requests_24h!s:<11}{breaker}")
    lines.append(f"BD {_mb(s.db_bytes)} MB de {DB_LIMIT_MB} · Atrasados > 1 h: {s.overdue}")
    return RenderedMessage("<pre>" + esc("\n".join(lines)) + "</pre>")


def render_daily(v: DailyView) -> RenderedMessage:
    """Resumen diario silencioso, que también funciona como latido (spec §5.5)."""
    last = f" (último {local(v.last_cycle_at):%H:%M})" if v.last_cycle_at is not None else ""
    parts = [f"discountZ: {v.cycles} ciclos en 24 h{last}"]
    by = ", ".join(f"{v.by_trigger[k]} {label}" for k, label in _TRIGGERS if v.by_trigger.get(k))
    if by:
        parts.append(by)
    parts.append(f"{v.alerts} alertas")
    if v.source_notes:
        parts.append(esc(", ".join(v.source_notes)))
    parts.append(f"BD {_mb(v.db_bytes)} MB de {DB_LIMIT_MB}")
    parts.extend(esc(w) for w in v.warnings)
    text = " · ".join(parts)
    if v.catalog is not None and v.catalog.items_by_source:
        text += "\n" + _catalog_line(v.catalog)
    return RenderedMessage(text, [], silent=True)


def _thousands(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _catalog_line(c: CatalogDaily) -> str:
    """Productos activos y última vuelta por tienda, cambios del día y alertas por tema."""
    stores = []
    for code in sorted(c.items_by_source, key=source_name):
        part = f"{esc(source_name(code))} {_thousands(c.items_by_source[code])}"
        sweep = c.last_sweep_s_by_source.get(code)
        if sweep is not None:
            hours, minutes = divmod(sweep // 60, 60)
            part += f" (vuelta {hours} h {minutes} min)"
        stores.append(part)
    line = f"Catálogo: {', '.join(stores)} · {_thousands(c.changes_today)} cambios hoy"
    if c.alerts_by_topic:
        topics = ", ".join(f"{esc(t)} {n}" for t, n in sorted(c.alerts_by_topic.items()))
        line += f" · alertas por tema: {topics}"
    return line
