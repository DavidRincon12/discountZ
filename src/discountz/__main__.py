"""Composition root y CLI de discountZ."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

_SUBCOMMANDS: dict[str, str] = {
    "run": "Ejecuta un ciclo completo.",
    "migrate": "Aplica o revierte migraciones de la base de datos.",
    "check-config": "Valida la configuración y las variables de entorno.",
    "record-fixture": "Graba una respuesta real como fixture de pruebas (solo local).",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="discountz", description="Monitor personal de ofertas.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{run,migrate,...}")
    for name, help_text in _SUBCOMMANDS.items():
        sub.add_parser(name, help=help_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"{args.command}: no implementado", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
