"""Composition root y CLI de discountZ."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

import psycopg

from discountz.config import load_canaries, load_params, load_settings
from discountz.domain.errors import ConfigError, LeaseBusy
from discountz.infra.db import connect
from discountz.infra.http import CurlClient, HttpxClient
from discountz.infra.migrate import MigrationError, migrate_with_lease
from discountz.sources.fixtures import FIXTURES_DIR, save_fixture
from discountz.sources.registry import load_all

EXIT_OK, EXIT_BUG, EXIT_CONFIG, EXIT_DB = 0, 1, 2, 3
_DURATION_RE = re.compile(r"^(\d+)([smh])$")
_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(text: str) -> int:
    """`20m` → 1200 segundos; acepta `s`, `m` y `h`."""
    m = _DURATION_RE.fullmatch(text)
    if m is None:
        raise argparse.ArgumentTypeError("duración inválida (ejemplos: 90s, 20m, 1h)")
    return int(m.group(1)) * _UNITS[m.group(2)]


def _target(text: str) -> int | Literal["head"]:
    if text == "head":
        return "head"
    if text.isdigit():
        return int(text)
    raise argparse.ArgumentTypeError("versión destino inválida: un entero o 'head'")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="discountz", description="Monitor personal de ofertas.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{run,migrate,...}")
    sub.add_parser("run", help="Ejecuta un ciclo completo.")
    mig = sub.add_parser("migrate", help="Aplica o revierte migraciones de la base de datos.")
    mig.add_argument("--to", type=_target, required=True, help="Versión destino o 'head'.")
    mig.add_argument("--wait-lease", type=parse_duration, default=0, help="Espera máxima.")
    sub.add_parser("check-config", help="Valida la configuración y las variables de entorno.")
    rec = sub.add_parser("record-fixture", help="Graba una respuesta real como fixture (local).")
    rec.add_argument("source", help="Código de la fuente (p. ej. exito).")
    rec.add_argument("name", help="Nombre de la fixture (p. ej. busqueda_ssd).")
    rec.add_argument("url", help="URL a pedir (consultas genéricas, nunca datos personales).")
    rec.add_argument("--folder", default=None, help="Carpeta dentro de tests/fixtures.")
    rec.add_argument("--post-json", default=None, help="Cuerpo JSON para un POST.")
    return parser


def _cmd_migrate(args: argparse.Namespace) -> int:
    url = os.environ.get("DISCOUNTZ_MIGRATOR_DATABASE_URL") or os.environ.get(
        "DISCOUNTZ_DATABASE_URL"
    )
    if not url:
        print("migrate: falta DISCOUNTZ_MIGRATOR_DATABASE_URL", file=sys.stderr)
        return EXIT_CONFIG
    holder = f"migrate:{os.environ.get('DISCOUNTZ_GH_RUN_ID', 'local')}"
    try:
        with connect(url, application_name="discountz-migrate") as conn:
            touched = migrate_with_lease(
                conn,
                args.to,
                holder=holder,
                wait_s=args.wait_lease,
                sleep=time.sleep,
                monotonic=time.monotonic,
            )
    except psycopg.OperationalError:
        print("migrate: base de datos inaccesible", file=sys.stderr)
        return EXIT_DB
    except LeaseBusy:
        print("migrate: el lease sigue ocupado", file=sys.stderr)
        return EXIT_BUG
    except MigrationError as exc:
        print(f"migrate: {exc}", file=sys.stderr)
        return EXIT_BUG
    print(f"migrate: versiones {touched or 'sin cambios'}")
    return EXIT_OK


def _cmd_record_fixture(args: argparse.Namespace) -> int:
    source = load_all().get(args.source)
    if source is None:
        print(f"record-fixture: fuente desconocida: {args.source}", file=sys.stderr)
        return EXIT_CONFIG
    client: HttpxClient | CurlClient = (
        CurlClient(args.source) if source.descriptor.client == "curl" else HttpxClient(args.source)
    )
    try:
        body = json.loads(args.post_json) if args.post_json else None
        resp = client.request("POST" if body is not None else "GET", args.url, json=body)
    finally:
        client.close()
    folder = args.folder or args.source
    today = datetime.now(UTC).date().isoformat()
    path = save_fixture(FIXTURES_DIR, folder, args.name, resp, recorded_at=today)
    print(f"record-fixture: {resp.status} → {path.relative_to(FIXTURES_DIR)}")
    return EXIT_OK


def _cmd_check_config() -> int:
    try:
        load_settings()
        params = load_params()
        canaries = load_canaries()
    except ConfigError as exc:
        print(f"check-config: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(
        f"check-config: OK ({len(params.budgets)} fuentes, {len(params.events)} eventos, "
        f"canarios en {len(canaries)} tiendas)"
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return _cmd_migrate(args)
    if args.command == "check-config":
        return _cmd_check_config()
    if args.command == "record-fixture":
        return _cmd_record_fixture(args)
    print(f"{args.command}: no implementado", file=sys.stderr)
    return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
