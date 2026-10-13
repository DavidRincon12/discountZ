"""Ejecutor de migraciones SQL up/down con sha256 por archivo (spec §7.6)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg

from discountz.domain.errors import InfrastructureError, LeaseBusy
from discountz.infra.db import acquire_lease, release_lease

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]{1,60})\.up\.sql$")

_BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS discountz;
CREATE TABLE IF NOT EXISTS discountz.schema_migrations (
  version    integer PRIMARY KEY CHECK (version > 0),
  name       text NOT NULL CHECK (name ~ '^[a-z0-9_]{1,60}$'),
  checksum   char(64) NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
"""


class MigrationError(InfrastructureError):
    """Migraciones inconsistentes (checksum distinto, archivo faltante o versión inválida)."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    up: Path
    down: Path

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.up.read_bytes()).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(directory.glob("*.up.sql")):
        m = _FILE_RE.fullmatch(path.name)
        if m is None:
            raise MigrationError("bad_migration_name")
        down = path.with_name(path.name.replace(".up.sql", ".down.sql"))
        if not down.exists():
            raise MigrationError("missing_down")
        found.append(Migration(int(m.group(1)), m.group(2), path, down))
    return found


def current_version(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT to_regclass('discountz.schema_migrations') IS NOT NULL").fetchone()
    if row is None or not row[0]:
        return 0
    got = conn.execute("SELECT coalesce(max(version), 0) FROM discountz.schema_migrations")
    value = got.fetchone()
    return int(value[0]) if value else 0


def migrate(
    conn: psycopg.Connection,
    to: int | Literal["head"],
    *,
    directory: Path = MIGRATIONS_DIR,
) -> list[int]:
    """Aplica o revierte hasta `to`; devuelve las versiones tocadas, en orden de ejecución."""
    migrations = discover(directory)
    by_version = {m.version: m for m in migrations}
    target = max(by_version, default=0) if to == "head" else to
    if target != 0 and target not in by_version:
        raise MigrationError("unknown_version")
    with conn.transaction():
        conn.execute(_BOOTSTRAP)
    applied: dict[int, str] = dict(
        conn.execute("SELECT version, checksum FROM discountz.schema_migrations").fetchall()
    )
    for version, checksum in applied.items():
        m = by_version.get(version)
        if m is None:
            raise MigrationError("missing_file")
        if m.checksum != checksum:
            raise MigrationError("checksum_mismatch")
    touched: list[int] = []
    for m in migrations:
        if m.version <= target and m.version not in applied:
            with conn.transaction():
                _timeouts(conn)
                conn.execute(m.up.read_text())
                conn.execute(
                    "INSERT INTO discountz.schema_migrations (version, name, checksum)"
                    " VALUES (%s, %s, %s)",
                    (m.version, m.name, m.checksum),
                )
            touched.append(m.version)
    for m in reversed(migrations):
        if m.version > target and m.version in applied:
            with conn.transaction():
                _timeouts(conn)
                conn.execute(m.down.read_text())
                conn.execute(
                    "DELETE FROM discountz.schema_migrations WHERE version = %s", (m.version,)
                )
            touched.append(m.version)
    return touched


def _timeouts(conn: psycopg.Connection) -> None:
    conn.execute("SET LOCAL lock_timeout = '5s'")
    conn.execute("SET LOCAL statement_timeout = '60s'")


def migrate_with_lease(
    conn: psycopg.Connection,
    to: int | Literal["head"],
    *,
    holder: str,
    wait_s: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    directory: Path = MIGRATIONS_DIR,
) -> list[int]:
    """Migra bajo el lease del ciclo (si ya existe la tabla), esperando hasta `wait_s`."""
    if not _lease_table_exists(conn):
        return migrate(conn, to, directory=directory)
    deadline = monotonic() + wait_s
    while not acquire_lease(conn, holder):
        if monotonic() >= deadline:
            raise LeaseBusy("lease_busy")
        sleep(5.0)
    try:
        return migrate(conn, to, directory=directory)
    finally:
        if _lease_table_exists(conn):
            release_lease(conn, holder)


def _lease_table_exists(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT to_regclass('discountz.run_lease') IS NOT NULL").fetchone()
    return bool(row and row[0])
