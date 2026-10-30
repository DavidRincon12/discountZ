# Semillas por ventanas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (Native, el modo por defecto de David) or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** que el rastreo de Mercado Libre elija sus semillas sin leer el catálogo entero, para que la consulta deje de pasar del `statement_timeout` de 15 s de `discountz_app` (las 11 corridas desde la fusión del PR #51 fallaron con `db_unavailable` y `crawl_seeds count=0 found=0`).

**Architecture:** índice parcial `catalog_item_seed_ix (price_minor, id)` solo con filas en pesos, con EAN y desde 100.000; `db.ean_seed_window` lee 200 filas de ese índice, del más caro al más barato, después de una marca `(price_minor, id)` que el rastreo guarda en `crawl_cursor` al terminar cada ventana. Migración `0018`, ADR 0019.

**Tech Stack:** Python 3.13, psycopg 3, PostgreSQL 17 (CI) / Supabase (Nano: 250 IOPS de base), pytest.

**Spec:** el diseño corto del hilo «Revisar estado de Mercado Libre» (2026-10-30), resumido en el ADR 0019 de la Task 1; parte de `docs/superpowers/specs/2026-10-29-mercadolibre-design.md` §1.3, que ya preveía un índice parcial si la consulta tardaba más de 2 s.

## Global Constraints

- Ninguna consulta de las semillas lee el catálogo entero: cada una toca a lo sumo `SEED_WINDOW = 200` filas del índice parcial; a lo sumo `SEED_WINDOWS = 25` ventanas por corrida.
- `SEED_FLOOR_MINOR = 100_000` (COP) es el piso del índice: las semillas usan `max(min_price_cop, SEED_FLOOR_MINOR)` y el piso va como literal en el SQL, para que el planificador pueda usar el índice parcial con cualquier plan.
- Orden: precio de mayor a menor y, a igual precio, `id` de mayor a menor. Si un EAN está en varias tiendas, manda su fila más cara de un tema en tarjeta (antes: la vista más recientemente).
- La marca se guarda solo con la ventana terminada; una ventana cortada (plazo, tope, pausa o tope de seguidos) se vuelve a leer y lo ya buscado se salta por `ean_lookup`. Al acabarse el catálogo, la marca vuelve a `NULL` (la próxima corrida empieza por el más caro) y esa corrida no siembra más.
- Migración en una transacción con `SET LOCAL statement_timeout = '10min'`; `SCHEMA_VERSION = 18`.
- Todo texto redactado en español; commits `tipo(alcance): descripción` a nombre de David, sin atribución a IA; `bash .claude/hooks/git-guard.sh check` antes del push.
- Verificación completa: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest -q` e integración con `DISCOUNTZ_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest -q -m integration`.

## Review Focus

- `min_price_cop` por debajo de 100.000: la consulta sigue usando el índice (Task 3, `test_ean_seed_window_uses_the_seed_index_below_its_floor`).
- Corrida cortada a mitad de una ventana: la siguiente la relee sin repetir búsquedas (Task 4, `test_seeds_go_on_after_the_last_finished_window`).
- Producto caro que aparece por encima de la marca: se busca cuando la vuelta termina y empieza otra (Task 4, mismo test).
- Ventanas enteras ya buscadas: la marca avanza y la corrida no lee más de `SEED_WINDOWS` (Task 4, `test_seeds_read_at_most_seed_windows_per_run`).
- Un EAN en dos tiendas dentro de una ventana: una sola semilla, con el tema de la fila más cara (Task 3, `test_ean_seed_window_keeps_the_most_expensive_row_of_each_ean`).

---

### Task 1: Plan y ADR 0019

**Files:**
- Create: `docs/decisions/0019-semillas-por-ventanas-del-catalogo.md`
- Add: este plan

- [ ] **Step 1:** ADR con la plantilla de `arquitectura-y-datos`. Contexto: la consulta de `ean_seeds` hace `Seq Scan` del catálogo y ordena en disco (local, 895 mil filas: 34 mil páginas y 13 MB de orden; en Supabase Nano, 250 IOPS y ~5 MB/s de base, pasa de 15 s). Decisión: la de *Architecture* y *Global Constraints*. Alternativas: subir el `statement_timeout` de esa consulta (lee ~270 MB cada hora), una cola diaria de semillas (una lectura completa al día y una tabla más), índice sin piso (25 MB) o con `ean` en la clave (11 MB, marca no única). Spike: «¿cada ventana toca menos de 1.000 páginas?» · medido 458 en frío (EXPLAIN BUFFERS, 895 mil filas, 30 mil EAN buscados y 12 mil seguidos) · sí. Cuándo revisar: la BD en ≥ 450 MB, `db_reconnect … error=QueryCanceled` en `mercadolibre`, o un catálogo en pesos de más de 2 millones de filas.
- [ ] **Step 2:** commit `docs(mercadolibre): plan y ADR 0019 de las semillas por ventanas`.

### Task 2: Migración 0018 y la marca del cursor

**Files:**
- Create: `migrations/0018_semillas_por_ventanas.up.sql`, `migrations/0018_semillas_por_ventanas.down.sql`
- Modify: `src/discountz/infra/db.py` (`SCHEMA_VERSION`, `SeedMark`, `CrawlCursor.seed_after`, `get_cursor`, `save_cursor`)
- Test: `tests/integration/test_migrations.py`, `tests/integration/test_catalog_db.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True, slots=True) class SeedMark: price_minor: int; item_id: int` y `CrawlCursor.seed_after: SeedMark | None = None` (último campo); columnas `crawl_cursor.seed_price_minor money_minor` y `crawl_cursor.seed_item_id bigint`, las dos nulas o las dos con valor.

- [ ] **Step 1: pruebas que fallan.** En `test_migrations.py`, las listas de versiones llegan a 18 y `SCHEMA_VERSION == 18`; `test_0018_up_down`: tras `migrate(db, 17) == [18]` no existen `catalog_item_seed_ix` ni las dos columnas, y con `migrate(db, "head") == [18]` vuelven; una marca a medias (`seed_price_minor` sin `seed_item_id`) da `CheckViolation`. En `test_catalog_db.py`, `test_cursor_roundtrip` guarda `seed_after=SeedMark(2_000_000, 41)` y lo lee igual; el cursor vacío trae `seed_after is None`.
- [ ] **Step 2:** `uv run pytest tests/integration/test_migrations.py tests/integration/test_catalog_db.py -q -m integration` falla (versión 17, `SeedMark` no existe).
- [ ] **Step 3:** migración: `CREATE INDEX catalog_item_seed_ix ON catalog_item (price_minor, id) WHERE currency = 'COP' AND ean IS NOT NULL AND price_minor >= 100000`, las dos columnas y `CHECK ((seed_price_minor IS NULL) = (seed_item_id IS NULL))`; la bajada los quita. `SCHEMA_VERSION = 18`; `get_cursor`/`save_cursor` leen y escriben las dos columnas.
- [ ] **Step 4:** las mismas pruebas pasan.
- [ ] **Step 5:** commit `feat(mercadolibre): migración 0018 con el índice de semillas y su marca`.

### Task 3: `db.ean_seed_window`

**Files:**
- Modify: `src/discountz/infra/db.py`
- Test: `tests/integration/test_ean_db.py`

**Interfaces:**
- Consumes: `SeedMark` (Task 2).
- Produces: `SEED_FLOOR_MINOR = 100_000`; `@dataclass(frozen=True, slots=True) class SeedWindow: seeds: tuple[EanSeed, ...]; after: SeedMark | None`; `ean_seed_window(conn, source: str, topics: Sequence[str], min_minor: int, after: SeedMark | None, limit: int) -> SeedWindow`. `after` del resultado: la marca de la última fila leída si la ventana vino llena; `None` si vino con menos de `limit` filas (se acabó el catálogo).

- [ ] **Step 1: pruebas que fallan** (helper `_window(conn, after=None, limit=10)` con `ML`, `CARDS`, `MIN`):
  - `test_ean_seed_window_takes_card_topics_cop_and_min_price`: los mismos datos de `test_ean_seeds_take_card_topics_cop_and_min_price` → `seeds == (EanSeed(A, "tecnologia"),)` y `after is None`.
  - `test_ean_seed_window_skips_tracked_and_looked_up`: los datos de `test_ean_seeds_skip_tracked_and_looked_up` → `(EanSeed(C, "tecnologia"),)`.
  - `test_ean_seed_window_keeps_the_most_expensive_row_of_each_ean`: E en `exito` (hogar, 500.000, visto hace 24 h) y en `jumbo` (electro, 400.000, ahora), F en `alkosto` a 900.000 → `(EanSeed(F, "tecnologia"), EanSeed(E, "hogar"))`.
  - `test_ean_seed_window_goes_on_after_the_mark`: A..D a 2,0 / 1,9 / 1,8 / 1,7 millones; con `limit=2`: primero `(A, B)` y `after == SeedMark(1_900_000, id de B)`; con ese `after`: `(C, D)` y `after` de D; con el de D: `()` y `after is None`.
  - `test_ean_seed_window_counts_skipped_rows`: A y B ya buscados, `limit=2` → `seeds == ()` y `after == SeedMark(1_900_000, id de B)`.
  - `test_ean_seed_window_uses_the_seed_index_below_its_floor`: con `SET enable_seqscan = off`, `ean_seed_window(…, min_minor=50_000, …)` sube `idx_scan` de `catalog_item_seed_ix` en `pg_stat_user_indexes` (tras `SELECT pg_stat_force_next_flush()`); una fila a 80.000 no sale.
- [ ] **Step 2:** `uv run pytest tests/integration/test_ean_db.py -q -m integration` falla (`ean_seed_window` no existe).
- [ ] **Step 3:** CTE `MATERIALIZED` con la ventana (`ORDER BY price_minor DESC, id DESC LIMIT %(limit)s`, `(price_minor, id) < (%(price)s, %(item)s)` solo con `after`) y por fila un `CASE` en este orden: tema fuera de `topics` → false; `EXISTS` en `ean_lookup` → false; si no, `NOT EXISTS` de una fila de `source` con ese EAN. Python quita los EAN repetidos conservando el orden.
- [ ] **Step 4:** pasan las pruebas nuevas y las viejas de `ean_seeds`.
- [ ] **Step 5:** commit `fix(mercadolibre): las semillas se leen por ventanas del índice parcial`.

### Task 4: El rastreo siembra por ventanas desde la marca

**Files:**
- Modify: `src/discountz/app/crawl.py` (`SEED_WINDOW = 200`, `SEED_WINDOWS = 25`, `_Run.seed`; se va `SEED_QUERY`), `src/discountz/infra/db.py` (se va `ean_seeds`)
- Test: `tests/integration/test_crawl_seeds.py`, `tests/integration/test_ean_db.py` (se van las tres pruebas de `ean_seeds`)

**Interfaces:**
- Consumes: `ean_seed_window`, `SeedWindow`, `SEED_FLOOR_MINOR` (Task 3); `CrawlCursor.seed_after` (Task 2).

- [ ] **Step 1: pruebas que fallan** (con `monkeypatch.setattr(crawl, "SEED_WINDOW", 2)`; las búsquedas se leen de `transport.urls`):
  - `test_seeds_go_on_after_the_last_finished_window`: A..D sin producto; corrida 1 con `max_per_run=3` busca A, B y C, y deja `seed_after` en B; se agrega Z a 3 millones; corrida 2 busca solo D (no repite C ni toma Z) y deja `seed_after is None`; corrida 3 busca Z.
  - `test_seeds_read_at_most_seed_windows_per_run`: `SEED_WINDOW = 1`, `SEED_WINDOWS = 2`, A..D ya buscados → ninguna búsqueda y `seed_after` queda en B.
- [ ] **Step 2:** `uv run pytest tests/integration/test_crawl_seeds.py -q -m integration` falla (la corrida 2 vuelve a leer desde arriba; `seed_after` no se guarda).
- [ ] **Step 3:** `_Run.seed` pide hasta `SEED_WINDOWS` ventanas desde `self.cursor.seed_after`, procesa sus semillas en lotes de `SEED_CHUNK` como hoy y, si al terminar la ventana `time_left()` y `seeds_allowed()` siguen, guarda `seed_after = window.after` con `save_cursor()`; con `after is None`, para. El piso es `max(min_price_cop, db.SEED_FLOOR_MINOR)`.
- [ ] **Step 4:** pasan todas las de `test_crawl_seeds.py` y `test_ean_db.py`.
- [ ] **Step 5:** commit `fix(mercadolibre): el rastreo siembra por ventanas desde donde quedó`.

### Task 5: El aviso de reconexión dice el error

**Files:**
- Modify: `src/discountz/app/crawl.py` (`run_crawl_reconnecting`)
- Test: `tests/integration/test_crawl.py`

- [ ] **Step 1: prueba que falla:** en la prueba que ya busca `"db_reconnect source=exito count=1"`, el log trae `error=` con el nombre de la clase de la excepción (la conexión cortada da `AdminShutdown` u `OperationalError`).
- [ ] **Step 2:** falla (el campo no se registra).
- [ ] **Step 3:** `event(… "db_reconnect" …, error=type(exc).__name__)`.
- [ ] **Step 4:** pasa.
- [ ] **Step 5:** commit `fix(rastreo): el aviso de reconexión dice el tipo de error`.

### Task 6: Documentación y estado

**Files:**
- Modify: `docs/architecture.md` (párrafo de `0018` y el flujo del rastreo sembrado), `CLAUDE.md` (viñeta de Mercado Libre y el gotcha del `statement_timeout` en Supabase Nano), `config/discountz.toml` (comentario de `min_price_cop`), `docs/estado.md` (reescrito)

- [ ] **Step 1:** documentar; en `docs/estado.md`, «Al fusionar: *Actions → migrate* (`to = head`, esquema 18)» y, en pendientes, vigilar la relectura y el conteo de Mercado Libre (con 12 mil seguidos leen ~1.200 páginas: 5 s en frío a 250 IOPS).
- [ ] **Step 2:** verificación completa en verde.
- [ ] **Step 3:** commit `docs(mercadolibre): arquitectura, gotchas y estado tras las semillas por ventanas`.
