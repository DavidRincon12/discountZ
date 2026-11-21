# BD que se cuida sola Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (Native). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** que la BD y el egress se regulen solos sin apagar servicios, y apagar el tema ferretería.

**Architecture:** cambios en `config/topics.toml`, `infra/db.py` (`save_catalog_page`, `bot_state`), `app/maintenance.py`, `app/crawl.py` y la migración `0020`.

**Tech Stack:** Python 3.13, psycopg 3, PostgreSQL 17 y pytest.

**Spec:** `docs/superpowers/specs/2026-11-21-bd-autorregulada-design.md`

## Global Constraints
- Ninguna consulta nueva lee el catálogo entero (`statement_timeout` de 15 s).
- Texto en español; logs solo con campos de `ALLOWED_FIELDS`.
- Ritmo en puntos básicos: `crawl_pace_bp` entre 1000 y 10000.

## Review Focus
- Un producto leído dos veces el mismo día: la segunda lectura no mueve `last_seen_at` (Task 2).
- Un producto nuevo guarda el `last_seen_at` de su primera lectura (Task 2).
- Sin estimación de egress (`None`): el ritmo no cambia (Task 4).
- Un ritmo ya en el piso con egress alto sigue en 1000, nunca 0 (Task 4).
- Un tope de corrida pequeño con ritmo bajo da al menos 1 petición si el tope era > 0 (Task 4).

---

### Task 1: Apagar ferretería
**Files:** `config/topics.toml`, `tests/unit/test_config.py` (`test_load_topics_reads_profile_modes`)
- [ ] Cambiar la prueba de modos a `ferreteria: "off"` y verla fallar.
- [ ] `mode = "off"` en `[topics.ferreteria]`; la prueba en verde junto con `tests/unit/test_topic_precision.py`.
- [ ] Commit `feat(temas): apaga ferretería y autos`.

### Task 2: «Visto» una vez al día
**Files:** `src/discountz/infra/db.py` (`save_catalog_page`); Test `tests/integration/test_catalog_db.py`
**Interfaces:** `CATALOG_SEEN_STEP = timedelta(days=1)` en `infra/db.py`.
- [ ] `test_save_catalog_page_moves_last_seen_once_a_day`: guardar a T, a T+3 h (queda T) y a T+25 h (queda T+25 h).
- [ ] Verla fallar; implementar con `CASE WHEN EXCLUDED.last_seen_at >= catalog_item.last_seen_at + interval '1 day'`; en verde con `tests/integration/test_catalog_db.py` y `tests/integration/test_crawl*.py`.
- [ ] Commit `perf(catalogo): anota «visto» una vez al día para no hinchar los índices`.

### Task 3: Migración 0020
**Files:** `migrations/0020_indices_y_ritmo.{up,down}.sql`, `SCHEMA_VERSION = 20`, listas de versiones en las pruebas
**Interfaces:** `bot_state.crawl_pace_bp smallint NOT NULL DEFAULT 10000 CHECK (crawl_pace_bp BETWEEN 1000 AND 10000)`.
- [ ] `test_0020_up_down` (columna presente tras up, ausente tras down) y verla fallar.
- [ ] Up: `SET LOCAL statement_timeout = '10min'`, `REINDEX INDEX` de los 7 índices de `catalog_item` y la columna. Down: borra la columna.
- [ ] `tests/integration/test_migrat*.py` en verde. Commit `feat(bd): reconstruye los índices del catálogo y agrega el ritmo del rastreo`.

### Task 4: Escalones y ritmo
**Files:** `app/maintenance.py`, `infra/db.py` (`BotState.crawl_pace_bp`, `set_crawl_pace`), `app/crawl.py` (tope de la corrida); Tests `tests/unit/test_egress.py`, `tests/integration/test_maintenance.py`
**Interfaces:**
- `maintenance.next_pace(pace_bp: int, mb_day: int | None) -> int`: `None` → igual; si no, `pace_bp * EGRESS_TARGET_MB // max(mb_day, 1)` acotado a [1000, 10000], con `EGRESS_TARGET_MB = EGRESS_QUOTA_MB * 8 // 10 // 31` (132).
- `domain.scheduling.paced_cap(cap: int, pace_bp: int) -> int`: `0` si `cap == 0`; si no, `max(1, cap * pace_bp // 10000)`.
- [ ] Pruebas unitarias: `next_pace(10000, 264) == 5000`, `next_pace(5000, 66) == 10000`, `next_pace(1000, 900) == 1000`, `next_pace(7000, None) == 7000`; `paced_cap(40, 5000) == 20`, `paced_cap(3, 1000) == 1`, `paced_cap(0, 5000) == 0`.
- [ ] Integración: con 420 MB simulados se borra el catálogo no visto en 15 días (y no el de 10); con 455 MB ya no pausa; con 485 pausa; con egress de 2 M filas en 24 h el ritmo baja y el aviso dice el ritmo.
- [ ] Verlas fallar; implementar (`CATALOG_UNSEEN_TIGHT = 14 d` desde `PRUNE_BYTES`, `PAUSE_BYTES = 480 MB`, `_measure_egress` guarda el ritmo; `crawl.py` aplica `paced_cap` al tope).
- [ ] Pruebas en verde. Commit `feat(mantenimiento): escalones de limpieza y ritmo del rastreo según el egress`.

### Task 5: Docs y verificación
- [ ] ADR `0021`, `CLAUDE.md` (gotcha), `docs/estado.md`.
- [ ] Verificación completa e integración en verde; commit `docs(bd): ADR 0021, estado y gotchas`; PR.
