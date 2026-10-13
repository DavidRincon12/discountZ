# discountZ

Monitor personal de ofertas: sigue productos y búsquedas en tiendas de Colombia (Éxito, Carulla, Olímpica, Panamericana) y de EE. UU. (Amazon, Newegg, Walmart, Best Buy, más el feed de Slickdeals), guarda un histórico de precios propio, descarta descuentos inflados y avisa por Telegram.

Corre en GitHub Actions con una base de datos Supabase Postgres gratuita: costo operativo $0.

> Borrador: la guía de puesta en marcha (bot de Telegram, Supabase, secretos y disparo externo) llega con los workflows de despliegue.

## Requisitos

- Python 3.13
- [uv](https://docs.astral.sh/uv/) (versión fijada en `pyproject.toml`)
- PostgreSQL 17 para las pruebas de integración

## Comandos

```bash
uv sync                                   # instala dependencias
uv run pytest -q                          # pruebas unitarias
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports
uv run python -m discountz --help         # CLI
```

Pruebas de integración:

```bash
DISCOUNTZ_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
  uv run pytest -q -m integration
```

## Documentación

- Arquitectura: `docs/architecture.md`
- Decisiones: `docs/decisions/`
- Diseño de la v1: `docs/superpowers/specs/2026-10-13-discountz-v1-design.md`
