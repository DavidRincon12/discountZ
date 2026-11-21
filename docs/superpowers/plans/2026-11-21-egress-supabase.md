# Egress de Supabase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (Native). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** arreglar el mantenimiento y el resumen de la noche, leer menos de la BD y avisar en Telegram cuando el egress vaya a pasar del plan gratis.

**Architecture:** son cambios puntuales en `infra/db.py`, `app/render.py`, `app/channels.py`, `app/cycle.py` y `app/maintenance.py`, más la migración `0019`. El medidor usa `pg_stat_statements` y guarda la lectura anterior en `bot_state`.

**Tech Stack:** Python 3.13, psycopg 3, PostgreSQL 17 y pytest.

**Spec:** `docs/superpowers/specs/2026-11-21-egress-supabase-design.md`

## Global Constraints
- Ninguna consulta nueva lee el catálogo entero (`statement_timeout` de 15 s).
- Los logs solo llevan campos de `ALLOWED_FIELDS`; se agrega `egress_mb`.
- El texto en español y el dinero en enteros.

## Review Focus
- Alertas en `queued` cuya `outbox` se purgó: el mantenimiento termina y no se reenvían (Task 1).
- Una sola línea con un HTML muy largo: el mensaje no pasa de 8.000 (Task 2).
- Un ciclo sin posts nuevos no lee `channel_post` (Task 3).
- Sin `pg_stat_statements`, o con las estadísticas reiniciadas: no se estima nada ni se avisa (Task 4).
- La falla del mantenimiento no bloquea el resumen de la noche (Task 1).

---

### Task 1: Arreglar el mantenimiento y aislar los pasos diarios

**Files:**
- Create: `migrations/0019_egress.up.sql`, `migrations/0019_egress.down.sql`
- Modify: `src/discountz/app/cycle.py` (`_daily`), las pruebas que cuentan el esquema
- Test: `tests/integration/test_maintenance.py`

**Interfaces:**
- Produce: `bot_state.select_rows_total bigint` y `bot_state.select_rows_at timestamptz`, ambas `NULL` al inicio (las usa la Task 4).

- [ ] **Step 1:** escribir `test_retention_keeps_queued_alerts_whose_outbox_expired`: una alerta en `queued` con su `outbox` de hace 40 días. Después de `run_maintenance`, la `outbox` se borró, la alerta tiene `delivery = 'queued'` y `outbox_id IS NULL`, y no se lanza nada.
- [ ] **Step 2:** escribir `test_daily_sends_digest_even_if_maintenance_fails`: monkeypatch de `cycle.run_maintenance` para que lance; `_daily` a las 21:10 de Bogotá encola el `digest`.
- [ ] **Step 3:** correr las dos y ver que fallan (CheckViolation y sin `digest`).
- [ ] **Step 4:** escribir la migración: `DROP`/`ADD CONSTRAINT alert_queued_ck CHECK (outbox_id IS NULL OR delivery = 'queued')` y las dos columnas de `bot_state`. El down restaura el CHECK original (antes, `UPDATE alert SET delivery='suppressed' WHERE delivery='queued' AND outbox_id IS NULL`) y borra las columnas. Separar `_daily` en tres `try`, cada uno con `maintenance_failed` y su `kind` (`maintenance`, `summary`, `digest`).
- [ ] **Step 5:** correr `tests/integration/test_maintenance.py`, `tests/integration/test_migrate*.py` y las pruebas de versión del esquema hasta verlas en verde.
- [ ] **Step 6:** commit `fix(mantenimiento): la purga de la outbox ya no rompe el mantenimiento`.

### Task 2: El resumen de la noche respeta el límite de HTML

**Files:** Modify `src/discountz/app/render.py`; Test `tests/unit/test_render.py`

- [ ] **Step 1:** escribir `test_held_digest_bodies_fit_the_outbox_limit`: 120 `HeldOffer` con un `href` de 300 caracteres y un título corto. Cada cuerpo debe cumplir `len(body) <= 8000` y la suma de líneas debe conservarse.
- [ ] **Step 2:** correrla y ver que falla.
- [ ] **Step 3:** crear `MAX_BODY_HTML = 8000` y cortar el mensaje cuando el visible pase de `MAX_VISIBLE` o `len("\n".join(...))` pase de `MAX_BODY_HTML`.
- [ ] **Step 4:** correr `tests/unit/test_render.py` y verla en verde.
- [ ] **Step 5:** commit `fix(resumen): corta el resumen de la noche por el largo del HTML`.

### Task 3: Canales y rastreo leen menos

**Files:** Modify `src/discountz/app/channels.py`, `src/discountz/infra/db.py` (`load_catalog_states`); Test `tests/integration/test_channels.py`

- [ ] **Step 1:** escribir `test_channels_without_new_posts_do_not_read_recent_posts`: monkeypatch de `db.recent_channel_posts` que cuenta las llamadas; una pasada sin posts nuevos debe dar 0 llamadas.
- [ ] **Step 2:** correrla y ver que falla.
- [ ] **Step 3:** `_Step.recent` pasa a `list | None` (`None` = sin cargar) con `_recent(step) -> list[RecentPost]`, que la carga la primera vez. `_Step` deja de ser `frozen`, o se usa un contenedor mutable. `load_catalog_states` usa `conn.execute(..., binary=True)`.
- [ ] **Step 4:** correr `tests/integration/test_channels.py` y `tests/integration/test_catalog_db.py` y verlas en verde.
- [ ] **Step 5:** commit `perf(egress): los canales y el rastreo leen menos de la BD`.

### Task 4: Medidor de egress con aviso

**Files:** Modify `src/discountz/infra/db.py`, `src/discountz/app/maintenance.py`, `src/discountz/infra/log.py`; Test `tests/unit/test_egress.py` y `tests/integration/test_maintenance.py`

**Interfaces:**
- `db.select_rows_total(conn) -> int | None`: suma de `rows` de `pg_stat_statements` del `current_user` cuyas consultas empiezan con `SELECT` o `WITH`; `None` si la vista no existe o falla.
- `db.egress_mark(conn) -> tuple[int | None, datetime | None]` y `db.set_egress_mark(conn, total: int, at: datetime) -> None`.
- `maintenance.egress_mb_per_day(prev: int | None, prev_at: datetime | None, total: int, now: datetime) -> int | None`, con `BYTES_PER_ROW = 170`.
- `maintenance.egress_too_high(mb_day: int) -> bool`: `mb_day * 31 >= 0,8 × 5.120`.

- [ ] **Step 1:** escribir las pruebas unitarias:
  - 1.000.000 de filas en 24 h dan 162 MB al día.
  - Si no hay lectura previa, si el total bajó o si pasaron menos de 12 h, el resultado es `None`.
  - `egress_too_high(133)` es `True` y `egress_too_high(130)` es `False`.
- [ ] **Step 2:** escribir las pruebas de integración:
  - Sin la extensión, `select_rows_total` es `None` y el mantenimiento no avisa.
  - Con `select_rows_total` parcheado (0 y luego 2 M en 24 h), el mantenimiento encola un `system` con «Tráfico de la BD» y guarda la marca.
- [ ] **Step 3:** correrlas y ver que fallan.
- [ ] **Step 4:** implementar. `run_maintenance` mide al final y devuelve `egress_mb` en `MaintenanceReport`; `_daily` lo registra (`egress_mb=`). El aviso sale con prioridad 0 y `dedup egress:<fecha>`.
- [ ] **Step 5:** correr las pruebas y verlas en verde.
- [ ] **Step 6:** commit `feat(mantenimiento): estima el egress diario y avisa antes del tope`.

### Task 5: Docs y verificación completa
- [ ] Escribir el ADR `docs/decisions/0020-presupuesto-de-egress.md`, una línea de gotcha en `CLAUDE.md`, reescribir `docs/estado.md` y actualizar `docs/architecture.md` si nombra el mantenimiento.
- [ ] Verificación completa (`CLAUDE.md`) e integración en verde.
- [ ] Commit `docs(egress): ADR 0020, estado y gotchas` y PR.
