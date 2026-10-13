# Estado de discountZ

## Estado actual
- Proyecto Python 3.13 con uv, ruff, mypy strict, import-linter, pre-commit y CI (`.github/workflows/ci.yml`).
- CLI `python -m discountz` con subcomandos stub (`run`, `migrate`, `check-config`, `record-fixture`).
- ADRs 0001-0003 y `docs/architecture.md` registrados; `CLAUDE.md`, reglas de Python y plantilla de PR listos.
- Aún no hay lógica de dominio, BD ni adaptadores.

## Último cambio
- Rama `claude/discountz-v1-plan-execution-ur365u`: Tasks 1-3 del plan (ADRs, scaffolding y tooling, convenciones).

## Siguiente paso
- Plan `docs/superpowers/plans/2026-10-13-discountz-v1.md` en modo **Native** (superpowers:executing-plans): continuar en la **Task 4** (`Money` y parseo de precios).
- Criterio de cada tarea: su prueba en verde y el comando de verificación completo de `CLAUDE.md`.
- Ledger local de ejecución: `.superpowers/sdd/2026-10-13-discountz-v1/progress.md` (no se versiona; si se pierde, reconstruir desde `git log`).

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions (repo público), Supabase Postgres, Telegram con comandos vía getUpdates (ADR 0001).
- Histórico por segmentos y estadísticos ponderados por tiempo (ADR 0002).
- Disparo externo cada 5 min y modo caliente (ADR 0003).
- Sin costo puesto/TRM: las alertas USD dicen «🇺🇸 Compra por casillero».
- Si spec y plan difieren, manda spec §0.2 y luego el plan.

## Pendientes y gotchas
- ruff 0.17 formatea Markdown: `docs/` está excluido en `pyproject.toml`.
- La imagen `postgres:17` del CI no tiene digest fijado (Docker Hub no respondió desde la sesión).
- Mercado Libre responde 403 sin OAuth (SP-1). Alkosto/Ktronix/Falabella necesitan su JSON interno (SP-2/SP-3).
- En el spike, Walmart y Best Buy dieron precios dudosos (24,00 y 49,99): quedan detrás de una puerta de activación.
- Confirmar las fechas del calendario de eventos en la Task 18.
