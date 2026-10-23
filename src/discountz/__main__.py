"""Composition root y CLI de discountZ."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import socket
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal, cast

import psycopg

from discountz.app.crawl import (
    DB_RECONNECT_PAUSE_S,
    DB_RECONNECTS,
    CrawlDeps,
    crawl_max_items,
    run_crawl_reconnecting,
)
from discountz.app.cycle import CycleDeps, run_cycle
from discountz.app.interests import active_interests
from discountz.config import (
    InterestsConfig,
    Settings,
    load_canaries,
    load_interests,
    load_params,
    load_settings,
    load_topics,
)
from discountz.domain.commands import BOT_MENU
from discountz.domain.errors import ConfigError, LeaseBusy, TelegramError
from discountz.domain.interests import Interest
from discountz.infra.credentials import EnvCredentials
from discountz.infra.db import SCHEMA_VERSION, connect, schema_version
from discountz.infra.healthchecks import Healthchecks
from discountz.infra.http import CATALOG_MAX_BODY_BYTES, CurlClient, HttpClient, HttpxClient
from discountz.infra.log import configure_logging, event, install_excepthook
from discountz.infra.migrate import MigrationError, migrate_with_lease
from discountz.infra.telegram import TelegramClient
from discountz.sources.base import Capability, CrawlSource
from discountz.sources.fixtures import FIXTURES_DIR, save_fixture
from discountz.sources.registry import load_all

EXIT_OK, EXIT_BUG, EXIT_CONFIG, EXIT_DB = 0, 1, 2, 3
EXIT_BLOCKED = 4  # rastreo bloqueado: rojo en Actions, sin reintentos (3 es la BD)
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
    crawl = sub.add_parser("crawl", help="Rastrea el catálogo de una tienda (spec del rastreo).")
    crawl.add_argument("--source", required=True, help="Código de la tienda (p. ej. exito).")
    crawl.add_argument("--minutes", type=int, default=45, help="Minutos máximos de la corrida.")
    sub.add_parser("check-config", help="Valida la configuración y las variables de entorno.")
    sub.add_parser("setup-bot", help="Quita el webhook y pone el menú de comandos del bot.")
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
        topics = load_topics()
        interests = load_interests()
    except ConfigError as exc:
        print(f"check-config: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(
        f"check-config: OK ({len(params.budgets)} fuentes, {len(params.events)} eventos, "
        f"{len(topics.params)} temas, {len(interests.seeds)} intereses, "
        f"canarios en {len(canaries)} tiendas)"
    )
    return EXIT_OK


CONNECT_ATTEMPTS = 3


def _connect_with_retry(settings: Settings, rnd: random.Random) -> psycopg.Connection:
    """3 intentos con espera U(0, 2·2^n) s (spec §1.3, paso 1)."""
    name = f"discountz-{settings.gh_run_id or 'local'}"
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            return connect(settings.database_url.get_secret_value(), application_name=name)
        except psycopg.OperationalError:
            if attempt == CONNECT_ATTEMPTS - 1:
                raise
            time.sleep(rnd.uniform(0, 2 * 2**attempt))
    raise AssertionError("inalcanzable")


def _crawl_interests(conn: psycopg.Connection, cfg: InterestsConfig) -> tuple[Interest, ...]:
    """Con el esquema anterior no hay tabla de intereses: la corrida termina en `skipped_schema`."""
    if schema_version(conn) != SCHEMA_VERSION:
        return ()
    return active_interests(conn, cfg)


def _connect_crawl(settings: Settings, rnd: random.Random, source: str) -> psycopg.Connection:
    """Arranque del rastreo con la misma paciencia que la reconexión a mitad de corrida.

    Con la matriz entera arrancando a la vez, el pooler de Supabase a veces rechaza las
    conexiones unos segundos (Éxito y eBay salían con código 3 a las 13:00Z).
    """
    for attempt in range(1, DB_RECONNECTS + 2):
        try:
            return _connect_with_retry(settings, rnd)
        except psycopg.OperationalError:
            if attempt > DB_RECONNECTS:
                raise
            event(
                logging.getLogger("discountz"),
                "db_reconnect",
                logging.WARNING,
                source=source,
                count=attempt,
            )
            time.sleep(DB_RECONNECT_PAUSE_S)
    raise AssertionError("inalcanzable")


def _cmd_setup_bot() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"setup-bot: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    api = HttpxClient("telegram")
    try:
        telegram = TelegramClient(settings.telegram_bot_token, api)
        telegram.delete_webhook()
        telegram.set_my_commands(settings.telegram_chat_id, BOT_MENU)
    except TelegramError as exc:
        print(f"setup-bot: Telegram respondió {exc}", file=sys.stderr)
        return EXIT_CONFIG
    finally:
        api.close()
    print(f"setup-bot: menú con {len(BOT_MENU)} comandos")
    return EXIT_OK


def _cmd_run() -> int:
    configure_logging()
    install_excepthook()
    now = datetime.now(UTC)
    try:
        settings = load_settings()
        params = load_params()
        canaries = load_canaries()
        topics = load_topics()
    except ConfigError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    rnd = random.Random()
    sources = load_all()
    clients: dict[str, HttpxClient | CurlClient] = {
        code: CurlClient(code) if s.descriptor.client == "curl" else HttpxClient(code)
        for code, s in sources.items()
    }
    api = HttpxClient("telegram")
    pics = HttpxClient("fotos")
    health = Healthchecks(settings.healthchecks_url, api)
    holder = (
        f"gh:{settings.gh_run_id}:{settings.gh_run_attempt or 1}"
        if settings.gh_run_id is not None
        else f"local:{socket.gethostname()[:60]}"
    )
    try:
        conn = _connect_with_retry(settings, rnd)
    except psycopg.OperationalError:
        event(logging.getLogger("discountz"), "db_unavailable", logging.ERROR)
        health.fail()
        return EXIT_DB
    http: dict[str, HttpClient] = dict(clients)
    try:
        with conn:
            result = run_cycle(
                CycleDeps(
                    conn=conn,
                    sources=sources,
                    http=http,
                    telegram=TelegramClient(settings.telegram_bot_token, api),
                    params=params,
                    canaries=canaries,
                    settings=settings,
                    holder=holder,
                    monotonic=time.monotonic,
                    sleep=time.sleep,
                    rnd=rnd,
                    healthchecks=health,
                    credentials=EnvCredentials(settings),
                    photos=pics,
                    topics=topics,
                ),
                now,
            )
    except psycopg.OperationalError:
        event(logging.getLogger("discountz"), "db_unavailable", logging.ERROR)
        health.fail()
        return EXIT_DB
    finally:
        for client in clients.values():
            client.close()
        api.close()
        pics.close()
    return result.exit_code


def _cmd_crawl(args: argparse.Namespace) -> int:
    configure_logging()
    install_excepthook()
    now = datetime.now(UTC)
    try:
        settings = load_settings()
        params = load_params()
        topics = load_topics()
        interests = load_interests()
    except ConfigError as exc:
        print(f"crawl: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.source == "ebay_us" and settings.ebay_cert_id is None:
        print(f"crawl: {args.source} sin credenciales; se omite")
        return EXIT_OK
    source = load_all().get(args.source)
    budget = params.crawl.get(args.source)
    if source is None or budget is None or Capability.CATALOG not in source.descriptor.capabilities:
        print(f"crawl: {args.source} no tiene rastreo de catálogo", file=sys.stderr)
        return EXIT_CONFIG
    if args.source == "bestbuy_us" and settings.bestbuy_api_key is None:
        print(f"crawl: {args.source} sin clave de API; se omite")
        return EXIT_OK
    rnd = random.Random()
    client: HttpxClient | CurlClient = (
        CurlClient(args.source)
        if source.descriptor.client == "curl"
        else HttpxClient(args.source, max_body=CATALOG_MAX_BODY_BYTES)
    )
    try:
        conn = _connect_crawl(settings, rnd, args.source)
    except psycopg.OperationalError:
        event(logging.getLogger("discountz"), "db_unavailable", logging.ERROR)
        client.close()
        return EXIT_DB
    holder = (
        f"gh:{settings.gh_run_id}:{settings.gh_run_attempt or 1}"
        if settings.gh_run_id is not None
        else f"local:{socket.gethostname()[:60]}"
    )
    try:
        with conn:
            result = run_crawl_reconnecting(
                CrawlDeps(
                    conn=conn,
                    source=cast(CrawlSource, source),  # CATALOG ⇒ implementa CatalogSource
                    http=client,
                    credentials=EnvCredentials(settings),
                    topics=topics,
                    params=params,
                    budget=budget,
                    holder=holder,
                    monotonic=time.monotonic,
                    sleep=time.sleep,
                    rnd=rnd,
                    max_items=crawl_max_items(args.source, params),
                    interests=_crawl_interests(conn, interests),
                    interest_cap=interests.daily_cards_per_interest,
                ),
                now,
                args.minutes,
                lambda: _connect_with_retry(settings, rnd),
            )
    except psycopg.OperationalError:
        event(logging.getLogger("discountz"), "db_unavailable", logging.ERROR)
        return EXIT_DB
    finally:
        client.close()
    return EXIT_BLOCKED if result.status == "blocked" else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return _cmd_migrate(args)
    if args.command == "run":
        return _cmd_run()
    if args.command == "crawl":
        return _cmd_crawl(args)
    if args.command == "check-config":
        return _cmd_check_config()
    if args.command == "setup-bot":
        return _cmd_setup_bot()
    if args.command == "record-fixture":
        return _cmd_record_fixture(args)
    print(f"{args.command}: no implementado", file=sys.stderr)
    return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
