"""Fase C: confirmación independiente al terminar las tareas de una fuente (spec §3.6).

Una ruta independiente es otro endpoint con otro parser (VTEX: la simulación de checkout).
Lo confirmado pasa a `g` (error de precio) u `o` y crea la alerta; lo que no cuadra abre un
segmento `o` con la lectura de confirmación y suma `confirm_mismatch`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta

from discountz.app.collect import Candidate, Collector, SourceRun
from discountz.domain.errors import BudgetExhausted, ParseError, SourceError
from discountz.domain.guards import check_storm
from discountz.domain.model import AlertKind, Flag, Offer
from discountz.domain.segments import Observation, SegmentStatus, gap_h
from discountz.infra import db
from discountz.infra.log import event

log = logging.getLogger(__name__)


def confirm_candidates(c: Collector, r: SourceRun) -> None:
    candidates, r.candidates = r.candidates, []
    errors = sum(1 for x in candidates if x.evaluation.verdict is AlertKind.PRICE_ERROR)
    storm = check_storm(errors)  # G12
    for cand in candidates:
        if r.stopped or c.monotonic() >= c.deadline:
            _defer(c, cand)
            continue
        try:
            reading = r.source.confirm(c.ctx(r), cand.offer)
        except BudgetExhausted:
            r.stopped = True
            _defer(c, cand)
            continue
        except (SourceError, ParseError) as exc:
            c.on_error(r, exc)
            _defer(c, cand)
            continue
        if _matches(cand.offer, reading, c.params.glob.confirm_tol_bp):
            _confirmed(c, cand, storm)
        else:
            _mismatch(c, r, cand, reading)


def _matches(offer: Offer, reading: Offer | None, tol_bp: int) -> bool:
    if reading is None or reading.price is None or offer.price is None or not reading.in_stock:
        return False
    if reading.ref != offer.ref:
        return False
    p, q = offer.price.minor, reading.price.minor
    return abs(p - q) * 10_000 <= tol_bp * p


def _confirmed(c: Collector, cand: Candidate, storm: bool) -> None:
    verdict = cand.evaluation.verdict
    status = SegmentStatus.GLITCH if verdict is AlertKind.PRICE_ERROR else SegmentStatus.OPEN
    extra = frozenset({Flag.STORM}) if storm else frozenset()
    with c.conn.transaction():
        db.set_segment_status(c.conn, cand.listing_id, cand.segment.started_at, status)
        db.set_pending_confirmation(c.conn, cand.listing_id, None)
        c.finalize(replace(cand, segment=replace(cand.segment, status=status)), extra, tx=False)
    event(log, "confirmed", source=cand.offer.ref.source, ref=f"L{cand.listing_id}")


def _mismatch(c: Collector, r: SourceRun, cand: Candidate, reading: Offer | None) -> None:
    r.counters.confirm_mismatch += 1
    with c.conn.transaction():
        db.set_pending_confirmation(c.conn, cand.listing_id, None)
    if reading is not None and reading.price is not None:
        obs = Observation(
            # Mismo `now` del ciclo que la lectura sospechosa: va 1 s después para no ignorarse.
            max(reading.observed_at, cand.segment.last_seen_at + timedelta(seconds=1)),
            reading.price.minor,
            reading.list_price.minor if reading.list_price is not None else None,
            reading.in_stock,
            reading.seller_class,
            reading.unit_key,
        )
        # La lectura de confirmación es la muestra siguiente: cierra el 's' y abre un 'o'.
        h = gap_h(r.budget.watch_interval)
        db.write_sample(c.conn, cand.listing_id, obs, SegmentStatus.OPEN, h)
    event(log, "confirm_mismatch", source=r.code, ref=f"L{cand.listing_id}")


def _defer(c: Collector, cand: Candidate) -> None:
    with c.conn.transaction():
        db.set_pending_confirmation(c.conn, cand.listing_id, c.now)
