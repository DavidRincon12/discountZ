# Estado de discountZ

## Estado actual
- Dominio puro completo (Tasks 4-11), BD con migración `0001_init`, lease y repositorio (12-13), HTTP con `PacedClient` (14), contrato de fuentes y VTEX ×4 con fixtures reales (15-16).
- Telegram (17): cliente, render HTML con golden y bandeja idempotente; configuración validada y `check-config` (18).
- Ciclo (19-20): `python -m discountz run` con lease, bandeja, plan, recolección, confirmación independiente, composición, entrega, salud, mantenimiento diario y resumen de las 08:00.
- Despliegue (21): workflows `cycle`, `migrate` (environment `production-db`) y `keepalive`; guía de puesta en marcha en `README.md`.
- Amazon US (22): ficha (buybox), ruta independiente (`twister-plus-price-data-price`), búsqueda sin patrocinados, `a.co`/`amzn.to` y bloqueo Akamai; fixtures reales en `tests/fixtures/amazon_us/`.

## Último cambio
- Rama `claude/discountz-v1-plan-execution-ur365u`: Tasks 17-22 sobre las Tasks 1-16 ya revisadas. Las desviaciones de cada tarea están en el cuerpo de su commit.

## Siguiente paso
- Plan `docs/superpowers/plans/2026-10-13-discountz-v1.md` en modo **Native** (superpowers:executing-plans): continuar en la **Task 23** (adaptador Newegg); luego 24 (Slickdeals), 25 (Walmart y Best Buy) y 26 (cierre, con la revisión final de toda la rama).
- Criterio de cada tarea: su prueba en verde y el comando de verificación completo de `CLAUDE.md` (más `-m integration` con Postgres si toca la BD).
- No hay ledger local: el historial está en `git log` y las decisiones en los cuerpos de los commits.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase Postgres, Telegram vía getUpdates (ADR 0001); histórico por segmentos (ADR 0002); disparo externo y modo caliente (ADR 0003).
- Sin costo puesto ni TRM: las alertas USD dicen «🇺🇸 Compra por casillero».
- Si spec y plan difieren, manda spec §0.2 y luego el plan.
- VTEX: lotes de hasta 50 SKUs (M2) y simulación anónima como ruta independiente en las 4 tiendas (M3); seguimiento cada ciclo (`watch_interval_min = 1`).
- `/seguir` responde al instante y el ciclo confirma tras la primera lectura (`watch.reply_to_message_id`).
- La confirmación exige ±1 %, stock, misma variante y mismo vendedor. Las búsquedas de Amazon marcan el vendedor `f` provisional: la ficha decide.

## Pendientes y gotchas
- Postgres en la nube: `service postgresql start` y, si pide clave, `ALTER USER postgres PASSWORD 'postgres'`.
- Amazon: desde la nube, `curl_cffi` (cliente del plan) recibió el intersticial de Akamai (`bm-verify`) y httpx la página real; las fixtures se grabaron con httpx. Medirlo en Actions (M4) y cambiar `client` si hace falta.
- Fechas del calendario de eventos 2026-2027 estimadas: David debe confirmarlas (Hot Sale y Cyber Lunes de Colombia, sobre todo).
- Falta: `setup-bot`, `pg_terminate_backend` tras 3 `skipped_locked`, descarte G10 contra `R_listing`, mensaje agrupado de tormenta, tareas `feed` (Task 24) y `config/supabase-ca.crt` para `sslmode=verify-full`.
- Una alerta de un ciclo que murió antes de componer sale con vista reducida (sin línea de histórico).
- ruff 0.17 formatea Markdown: `docs/` está excluido. La imagen `postgres:17` del CI no tiene digest fijado.
- El contrato «Adaptadores independientes» de import-linter crece con cada adaptador; la regla «Pack» vive en `sources/base.py` (`pack_unit_key`).
- Walmart y Best Buy siguen detrás de la puerta de activación.
