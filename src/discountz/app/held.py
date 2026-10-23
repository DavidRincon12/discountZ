"""Tope de tarjetas por fuente al día, una por promoción y resumen de la noche (ADR 0012).

Del rastreo y de los canales salen en tarjeta las primeras `daily_cards` ofertas de cada fuente
en el día de Bogotá; los errores de precio no cuentan. Una bajada o un error de precio de la
misma tienda, tema y clase, con el ahorro a ±0,5 % de otro que ya salió en tarjeta en 24 h, es
la misma promoción (tallas, colores, tonos) y tampoco sale en tarjeta. Lo que saca un interés
no usa el tope de la fuente sino el suyo: `per_interest` tarjetas al día (spec del perfil §3.3).
Lo retenido llega en el primer ciclo después de las 21:00 en un resumen con una línea por oferta.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta

import psycopg

from discountz.app.compose import is_quiet
from discountz.app.render import BOGOTA, local, render_held_digest
from discountz.config import AppParams
from discountz.domain.model import AlertKind
from discountz.infra import db
from discountz.infra.log import event

log = logging.getLogger(__name__)

PROMO_WINDOW = timedelta(hours=24)
PROMO_TOL_BP = 50
# Comparan una publicación con su propio historial; MODEL_LOW compara vendedores distintos.
PROMO_KINDS = frozenset({AlertKind.CATALOG_DROP, AlertKind.PRICE_ERROR})
DIGEST_AT = time(21, 0)  # hora de Bogotá, antes de las horas de silencio
DIGEST_PRIORITY = 9  # después de las tarjetas del mismo ciclo
DIGEST_TTL = timedelta(hours=12)


@dataclass(frozen=True, slots=True)
class Slot:
    """Una oferta del rastreo o de un canal frente al tope del día."""

    source: str
    topic: str
    kind: AlertKind
    drop_bp: int | None  # ahorro frente al «antes»; None en los canales
    interest: str | None = None  # el interés que la saca: su tope reemplaza al de la fuente


def admit(
    conn: psycopg.Connection,
    slot: Slot,
    now: datetime,
    daily_cards: int,
    line: str,
    per_interest: int = 3,
) -> bool:
    """¿Sale en tarjeta? La registra: en tarjeta, o retenida con `line` para el resumen."""
    reason = _hold_reason(conn, slot, now, daily_cards, per_interest)
    entry = db.OfferEntry(
        slot.source,
        local(now).date(),
        slot.topic,
        slot.kind.value,
        slot.drop_bp,
        None if reason is None else line,
        slot.interest,
    )
    db.log_offer(conn, entry, now)
    if reason is not None:
        event(log, "offer_held", source=slot.source, code=reason)
    return reason is None


def _hold_reason(
    conn: psycopg.Connection, slot: Slot, now: datetime, daily_cards: int, per_interest: int
) -> str | None:
    if (
        slot.kind in PROMO_KINDS
        and slot.drop_bp is not None
        and db.carded_promotion(
            conn,
            slot.source,
            slot.topic,
            slot.kind.value,
            slot.drop_bp,
            now - PROMO_WINDOW,
            PROMO_TOL_BP,
        )
    ):
        return "promotion"
    if slot.kind is AlertKind.PRICE_ERROR:
        return None
    day = local(now).date()
    if slot.interest is not None:
        if db.count_interest_cards(conn, slot.interest, day) >= per_interest:
            return "interest_cap"
        return None
    if db.count_cards(conn, slot.source, day) >= daily_cards:
        return "daily_cap"
    return None


def _cutoff(now: datetime) -> datetime:
    """Las 21:00 de hoy en Bogotá: lo retenido después espera al resumen de mañana."""
    return datetime.combine(local(now).date(), DIGEST_AT, tzinfo=BOGOTA)


def digest_due(conn: psycopg.Connection, now: datetime) -> bool:
    return local(now).time() >= DIGEST_AT and db.has_held_offers(conn, _cutoff(now))


def send_digest(conn: psycopg.Connection, now: datetime, params: AppParams) -> int:
    """Encola el resumen con lo retenido antes de las 21:00 de hoy; devuelve cuántos mensajes.

    Suena solo el primer mensaje, y ninguno en las horas de silencio.
    """
    rows = db.held_offers(conn, _cutoff(now))
    bodies = render_held_digest(rows, params.daily_cards)
    quiet = is_quiet(now, params.quiet_start, params.quiet_end)
    # La primera retenida distingue un segundo resumen del mismo día: una corrida del rastreo
    # que empezó antes de las 21:00 puede guardar lo suyo después del primero.
    batch = f"{local(now).date().isoformat()}:{min((r.id for r in rows), default=0)}"
    with conn.transaction():
        for i, body in enumerate(bodies):
            db.enqueue(
                conn,
                "digest",
                body,
                priority=DIGEST_PRIORITY,
                dedup_key=f"held:{batch}:{i}",
                now=now,
                ttl=DIGEST_TTL,
                silent=quiet or i > 0,
            )
        db.mark_offers_digested(conn, [r.id for r in rows], now)
    if rows:
        event(log, "digest_sent", count=len(rows), sent=len(bodies))
    return len(bodies)
