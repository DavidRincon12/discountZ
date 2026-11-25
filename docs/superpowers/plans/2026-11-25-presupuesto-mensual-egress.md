# Presupuesto mensual de egress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que el egress del mes de Supabase (del 28 al 28) nunca llegue al límite: el ritmo del rastreo reparte un presupuesto del 75 % del plan y se apaga al agotarlo.

**Architecture:** El mantenimiento diario ya mide las filas que devolvió el rol (ADR 0020) y ajusta `crawl_pace_bp` (ADR 0021). Ahora acumula esas filas por ciclo de facturación en `bot_state`, calcula la meta del día con lo que queda del presupuesto y permite ritmo 0 (rastreo apagado).

**Tech Stack:** Python 3.13, psycopg 3, PostgreSQL 17, pytest.

**Spec:** `docs/superpowers/specs/2026-11-24-presupuesto-mensual-egress-design.md`

## Global Constraints
- Plan gratis: `EGRESS_QUOTA_MB = 5 * 1024`; presupuesto `EGRESS_BUDGET_MB = EGRESS_QUOTA_MB * 3 // 4` (3.840 MB); ciclo desde el día `EGRESS_CYCLE_DAY = 28` (fecha de Bogotá, `local(now).date()`).
- Meta diaria = `min(EGRESS_BUDGET_MB // 31, max(0, EGRESS_BUDGET_MB − usado) // días hasta el próximo 28)` (123 MB con el mes limpio; nunca más que eso, así el despliegue a mitad de ciclo no acelera el rastreo).
- Ritmo entre 0 y 10.000 bp; 0 solo cuando la meta es 0. Piso de 1.000 bp mientras haya presupuesto.
- Textos en español; logs con lista blanca; sin `float` para dinero (aquí no hay dinero).

## Review Focus
- Ciclo nuevo con el ritmo en 0: debe volver a arrancar (al menos 1.000 bp), no quedarse apagado para siempre.
- Estadísticas de `pg_stat_statements` reiniciadas (total < anterior): suma 0, no negativo.
- Mantenimiento del día 28 con el acumulado del ciclo anterior: reinicia el acumulado.
- Despliegue a mitad de ciclo: la migración siembra el ciclo 2026-10-28 con lo que ya se gastó (11,97 GB ⇒ 75.600.000 filas a 170 B), así el rastreo queda apagado hasta el 28-nov.
- Aviso de corte y del 90 % una sola vez por ciclo (`dedup_key` con la fecha de inicio del ciclo).

---

### Task 1: Migración 0021, accesos a la BD y ADR 0022

**Files:**
- Create: `migrations/0021_presupuesto_egress.up.sql`, `migrations/0021_presupuesto_egress.down.sql`, `docs/decisions/0022-presupuesto-mensual-de-egress.md`
- Modify: `src/discountz/infra/db.py` (`SCHEMA_VERSION = 21`; `egress_cycle`, `set_egress_cycle`), `tests/integration/test_migrations.py`

**Interfaces:**
- Produces: `db.egress_cycle(conn) -> tuple[date | None, int]`; `db.set_egress_cycle(conn, on: date, rows: int) -> None`.

- [ ] **Step 1:** `test_0021_up_down`: tras `head`, `bot_state.egress_cycle_on == date(2026, 10, 28)` y `egress_cycle_rows == 75_600_000`; `UPDATE … crawl_pace_bp = 0` pasa y `= 10001` da `CheckViolation`; `migrate(db, 20) == [21]` quita las columnas y deja el ritmo ≥ 1.000; `migrate(db, "head") == [21]`. Ajustar las listas de `migrate` de las pruebas anteriores (agregan 21) y `test_0020_up_down` (prueba 10001, no 999).
- [ ] **Step 2:** correr `uv run pytest tests/integration/test_migrations.py -q -m integration` ⇒ FAIL.
- [ ] **Step 3:** up: `ADD COLUMN egress_cycle_on date`, `ADD COLUMN egress_cycle_rows bigint NOT NULL DEFAULT 0 CHECK (egress_cycle_rows >= 0)`, cambiar el `CHECK` de `crawl_pace_bp` a `BETWEEN 0 AND 10000`, sembrar el ciclo. down: `crawl_pace_bp = greatest(crawl_pace_bp, 1000)`, volver al `CHECK` 1000-10000, quitar columnas. Accesos en `db.py`. ADR 0022.
- [ ] **Step 4:** la misma corrida ⇒ PASS. **Step 5:** commit `feat(egress): migración 0021 con el acumulado del ciclo de facturación`.

### Task 2: Meta del día con el presupuesto del ciclo

**Files:**
- Modify: `src/discountz/app/maintenance.py`, `src/discountz/domain/scheduling.py` (`paced_cap`), `tests/unit/test_egress.py`

**Interfaces:**
- Produces: `billing_cycle_start(day: date) -> date`; `next_billing_cycle(day: date) -> date`; `cycle_target_mb(used_mb: int, day: date) -> int`; `next_pace(pace_bp: int, mb_day: int | None, target_mb: int) -> int`.

- [ ] **Step 1:** pruebas: `billing_cycle_start(date(2026, 11, 25)) == date(2026, 10, 28)`, `(2026, 11, 28) → (2026, 11, 28)`, `(2026, 1, 5) → (2025, 12, 28)`; `cycle_target_mb(0, date(2026, 11, 28)) == 123`; `cycle_target_mb(3000, date(2026, 12, 20)) == 105`; `cycle_target_mb(3840, …) == 0` y `(5000, …) == 0`; `next_pace(5000, 200, 0) == 0`; `next_pace(0, 50, 123) == 2460`; `next_pace(0, None, 123) == 1000`; `paced_cap(40, 0) == 0`; las de `next_pace` existentes pasan `target_mb=132` y no cambian.
- [ ] **Step 2:** `uv run pytest tests/unit/test_egress.py -q` ⇒ FAIL.
- [ ] **Step 3:** implementar; `paced_cap` devuelve 0 si `cap <= 0 or pace_bp <= 0`.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat(egress): la meta diaria sale del presupuesto del ciclo`.

### Task 3: El mantenimiento acumula, avisa y apaga el rastreo

**Files:**
- Modify: `src/discountz/app/maintenance.py` (`_measure_egress`, `MaintenanceReport.egress_cycle_mb`), `src/discountz/app/cycle.py` (log `egress_cycle_mb=`), `src/discountz/infra/log.py`, `tests/integration/test_maintenance.py`, `tests/integration/test_crawl.py`

- [ ] **Step 1:** pruebas de integración (T = 2026-10-13, ciclo 2026-09-28, 15 días al 28): la de egress alto pasa a ritmo 3796 y «37 %», y el aviso dice «GB del ciclo»; acumulado en el mismo ciclo (sumado al anterior) e informado en `report.egress_cycle_mb`; ciclo viejo ⇒ el acumulado vuelve a empezar; presupuesto agotado ⇒ `crawl_pace_bp == 0` y un aviso «apago el rastreo» que no se repite al día siguiente; ≥ 90 % del plan ⇒ aviso «90 %»; `run_crawl` con ritmo 0 no hace peticiones.
- [ ] **Step 2:** `uv run pytest tests/integration/test_maintenance.py tests/integration/test_crawl.py -q -m integration -k "egress or pace"` ⇒ FAIL.
- [ ] **Step 3:** implementar (suma `total − anterior` solo si hay anterior y no bajó).
- [ ] **Step 4:** PASS. **Step 5:** commit `feat(egress): el mantenimiento reparte el presupuesto del mes y apaga el rastreo al agotarlo`.

### Task 4: Docs y verificación completa
- [ ] `CLAUDE.md` (línea del ADR 0021), `docs/architecture.md`, `docs/estado.md` reescrito.
- [ ] Verificación completa del `CLAUDE.md` en verde (unitarias e integración). Commit `docs(egress): estado y arquitectura con el presupuesto mensual`.
