"""Acceso a Postgres: conexión y funciones SQL (Repository como módulo de funciones)."""

from __future__ import annotations

import psycopg

SCHEMA_VERSION = 1
CONNECT_TIMEOUT_S = 10


def connect(url: str, *, application_name: str = "discountz") -> psycopg.Connection:
    """Conexión en autocommit: cada escritura va en `with conn.transaction()` explícita."""
    conn = psycopg.connect(
        url,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_S,
        application_name=application_name,
    )
    conn.execute("SET TIME ZONE 'UTC'")
    return conn
