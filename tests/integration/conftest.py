"""BD temporal para pruebas de integración (`DISCOUNTZ_TEST_DATABASE_URL`)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from discountz.infra.db import connect
from discountz.infra.migrate import migrate


def _admin_url() -> str:
    url = os.environ.get("DISCOUNTZ_TEST_DATABASE_URL")
    if not url:
        pytest.skip("DISCOUNTZ_TEST_DATABASE_URL no está definida")
    return url


@pytest.fixture
def empty_db_url() -> Iterator[str]:
    """URL de una base vacía creada para la prueba y borrada al final."""
    admin = _admin_url()
    name = f"dz_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(admin)
    params["dbname"] = name
    try:
        yield make_conninfo(**{k: str(v) for k, v in params.items() if v is not None})
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def db(empty_db_url: str) -> Iterator[psycopg.Connection]:
    """Conexión a una base temporal con todas las migraciones aplicadas."""
    with connect(empty_db_url) as conn:
        migrate(conn, "head")
        yield conn


@pytest.fixture
def connect_extra(empty_db_url: str) -> Iterator[Callable[[], psycopg.Connection]]:
    """Abre conexiones adicionales a la misma base temporal (se cierran al final)."""
    opened: list[psycopg.Connection] = []

    def factory() -> psycopg.Connection:
        conn = connect(empty_db_url)
        opened.append(conn)
        return conn

    yield factory
    for conn in opened:
        conn.close()
