"""Composition root y CLI de discountZ."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Sequence
from typing import Literal

import psycopg

from discountz.domain.errors import LeaseBusy
from discountz.infra.db import connect
from discountz.infra.migrate import MigrationError, migrate_with_lease

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
    sub.add_parser("record-fixture", help="Graba una respuesta real como fixture (solo local).")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return _cmd_migrate(args)
    print(f"{args.command}: no implementado", file=sys.stderr)
    return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
