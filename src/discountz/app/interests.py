"""Intereses activos: la semilla y luego los personales de /interes (spec del perfil §3.1)."""

from __future__ import annotations

import psycopg

from discountz.config import InterestsConfig
from discountz.domain.interests import Interest, compile_interest
from discountz.infra import db


def active_interests(conn: psycopg.Connection, cfg: InterestsConfig) -> tuple[Interest, ...]:
    """En el orden del desempate: la semilla como está en el archivo y luego los personales."""
    personal = tuple(
        compile_interest(r.name, r.topic, r.terms, personal_id=r.id)
        for r in db.list_interests(conn)
    )
    return (*cfg.seeds, *personal)
