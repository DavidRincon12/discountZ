"""Componer (spec §1.3, paso 11): alertas `new` → outbox, solas o en resumen por categoría.

Lee las alertas `new` de la BD (también las de un ciclo que murió antes de componer) y las
marca `queued` en la MISMA transacción que encola su mensaje: nunca hay dos mensajes por alerta.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta

import psycopg

from discountz.app.render import (
    REFERENCE_LABEL,
    AlertView,
    RenderedMessage,
    local,
    render_alert,
    render_summary,
    reply_markup,
)
from discountz.config import AppParams
from discountz.domain.decide import PendingAlert, group_summaries
from discountz.domain.model import AlertKind, Flag
from discountz.domain.money import Money
from discountz.infra import db

ALERT_TTL = timedelta(hours=12)
PRIORITY = {
    AlertKind.PRICE_ERROR: 0,
    AlertKind.TARGET_REACHED: 1,
    AlertKind.HISTORIC_LOW: 2,
    AlertKind.GOOD_DEAL: 3,
    AlertKind.CATALOG_DROP: 3,
    AlertKind.COMMUNITY_DEAL: 3,
}
_FLAGS = {f.value: f for f in Flag}


def is_quiet(now: datetime, start: time, end: time) -> bool:
    t = local(now).time()
    return t >= start or t < end if start > end else start <= t < end


def reduced_view(a: db.UnqueuedAlert) -> AlertView:
    """Vista sin histórico, para alertas de un ciclo que murió antes de componer."""
    cur = a.currency
    return AlertView(
        listing_id=a.listing_id,
        watch_id=a.watch_id,
        kind=a.kind,
        category=a.category,
        source=a.source,
        title=a.title,
        url=a.url,
        price=Money(a.price_minor, cur),
        before=None,
        reference=Money(a.reference_minor, cur) if a.reference_minor else None,
        reference_label=REFERENCE_LABEL if a.reference_minor else None,
        real_saving_bp=a.real_saving_bp,
        advertised_saving_bp=a.advertised_saving_bp,
        list_price=Money(a.list_price_minor, cur) if a.list_price_minor else None,
        history_min=None,
        history_median=None,
        history_days=0,
        flags=frozenset(_FLAGS[f] for f in a.flags if f in _FLAGS),
        observed_at=a.observed_at,
    )


def compose_alerts(
    conn: psycopg.Connection, views: Mapping[int, AlertView], now: datetime, params: AppParams
) -> int:
    """Encola las alertas pendientes; devuelve cuántos mensajes creó."""
    rows = {a.id: a for a in db.unqueued_alerts(conn)}
    pending = [PendingAlert(a.id, a.listing_id, a.kind, a.category) for a in rows.values()]
    quiet = is_quiet(now, params.quiet_start, params.quiet_end)
    created = 0
    for group in group_summaries(pending):
        ids = [p.alert_id for p in group]
        items = [views.get(i) or reduced_view(rows[i]) for i in ids]
        msg: RenderedMessage = render_alert(items[0]) if len(items) == 1 else render_summary(items)
        kind = group[0].kind
        silent = msg.silent or (quiet and kind is not AlertKind.PRICE_ERROR)
        key = f"alert:{ids[0]}" if len(ids) == 1 else f"digest:{ids[0]}:{len(ids)}"
        with conn.transaction():
            outbox_id = db.enqueue(
                conn,
                "alert" if len(ids) == 1 else "digest",
                msg.html,
                priority=PRIORITY[kind],
                dedup_key=key,
                now=now,
                ttl=ALERT_TTL,
                reply_markup=reply_markup(msg.buttons) if msg.buttons else None,
                silent=silent,
            )
            db.mark_alerts_queued(conn, ids, outbox_id)
        created += 1
    return created
