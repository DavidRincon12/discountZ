# Rastreo de catálogo y alertas con imagen: plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** discountZ recorre solo el catálogo completo de Éxito, Carulla, Olímpica, Panamericana y Best Buy, detecta bajadas reales contra su propio histórico y las publica como imagen en temas de un grupo de Telegram; Walmart queda lista para activar por API y `/seguir` gana cobertura automática.

**Architecture:** Un caso de uso nuevo `crawl` (CLI y workflow en matriz, un job por tienda) que reanuda una vuelta del catálogo desde un cursor en Postgres, guarda solo números por producto, evalúa con reglas puras en `domain/catalog.py`, confirma releyendo la ficha y encola en la `outbox` de la v1 con datos de tarjeta. El ciclo de 5 min de la v1 entrega: genera la tarjeta PNG con Pillow y la sube con `sendPhoto` al tema que toca. Regla de capas intacta: `app → sources → infra → domain`.

**Tech Stack:** Python 3.13, uv, httpx, pydantic v2, psycopg 3, Pillow, cryptography, pytest + hypothesis, ruff, mypy strict, import-linter, PostgreSQL 17.

**Spec:** `docs/superpowers/specs/2026-10-14-rastreo-catalogo-design.md` (y la v1 `docs/superpowers/specs/2026-10-13-discountz-v1-design.md` donde este calla). ADR: `docs/decisions/0004-rastreo-de-catalogo-completo.md`. Cada tarea cita la sección que implementa; léela completa antes de empezar. Si el spec y el plan difieren, manda §0.2 del spec (E1-E8).

## Global Constraints

- Todas las de `CLAUDE.md` y `.claude/rules/python.md`: dinero en `Money` de unidad menor, porcentajes en `_bp`, datetimes UTC, reloj solo en `__main__.py` e `infra/http.py`, logs con lista blanca, textos en español e identificadores en inglés.
- Dependencias nuevas fijadas con `==`: `pillow`, `cryptography`. `domain/` no importa ninguna de las dos (contrato «Dominio puro»: agregar `PIL` y `cryptography` a `forbidden_modules`).
- Nunca sortear CAPTCHA ni detección de bots (E6). Un bloqueo detiene la corrida de esa tienda vía circuit breaker.
- Nunca usar el `ListPrice` de la tienda para decidir una alerta del catálogo (spec §3.2).
- La clave de Best Buy viaja en la query (`apiKey`): ningún log, error ni fixture puede contenerla (el *scrubber* de `record-fixture` la redacta).
- Commits `tipo(alcance): descripción` en español, a nombre de David, sin atribución a IA; `bash .claude/hooks/git-guard.sh check` antes de cada push. Solo la rama de la sesión.
- Verificación completa al cerrar cada tarea: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest -q`; las tareas con BD además `DISCOUNTZ_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest -q -m integration`.

## Review Focus

1. **Vendedor por defecto agotado y otro vendedor con stock** (VTEX marketplace): el producto entra con el menor precio > 0 con stock, no se descarta ni entra con precio 0 → `test_catalog_page_takes_cheapest_seller_in_stock` (Task 4).
2. **Hoja de categoría con más de 2.500 productos**: se divide por rangos de precio hasta caber; ningún producto queda fuera por el tope de paginación → `test_partitions_split_large_leaf_by_price` (Task 4).
3. **Corrida cortada a mitad de página** (plazo, bloqueo o caída del job): la siguiente corrida reanuda en la misma partición y página, sin duplicar cambios de precio ni alertas → `test_crawl_resumes_from_cursor_without_duplicates` (Task 8).
4. **Foto que no carga, corrupta, enorme o de otro host**: la tarjeta sale igual con el recuadro de la tienda y nunca se descarga de un host fuera de `image_hosts` → `test_card_without_photo_*` (Task 9) y `test_deliver_skips_photo_from_foreign_host` (Task 11).
5. **Tema borrado en Telegram** (`message thread not found`): la alerta sale en General y se avisa una vez que corra `/temas` → `test_deliver_falls_back_to_general_when_topic_missing` (Task 11).

---

### Task 1: Temas y asignación de categorías

Spec §6 y §5.1 (mapeo de categorías de la v1).

**Files:**
- Create: `src/discountz/domain/topics.py`, `config/topics.toml`
- Modify: `src/discountz/config.py` (cargar `topics.toml`), `pyproject.toml` (dependencias `pillow`, `cryptography`; `PIL` y `cryptography` en «Dominio puro»), `uv.lock`
- Create: `tests/fixtures/vtex/<tienda>-category-tree.json` (×4, grabadas con `record-fixture`), `tests/unit/test_topics.py`

**Interfaces:**
- Produces:
  - `TOPIC_CODES: tuple[str, ...]` en el orden de spec §6.
  - `@dataclass(frozen=True, slots=True) class TopicParams: code: str; title: str; t_bp: int | None; g_bp: int | None; category: str` (`category` = categoría de la v1 para `listing.category`).
  - `@dataclass(frozen=True, slots=True) class TopicRule: source: str; prefix: str; topic: str` (`source` = código o `"*"`; `prefix` normalizado con `normalize_path`).
  - `normalize_path(path: str) -> str` (minúsculas, sin tildes, `/` final).
  - `assign_topic(rules: Sequence[TopicRule], source: str, category_path: str) -> str` (gana el prefijo más largo; fuente exacta antes que `"*"` a igual largo; sin coincidencia ⇒ `"otros"`).
  - `CATEGORY_TOPIC: Mapping[str, str]` (`tecnologia`/`componentes` → `tecnologia`, `hogar` → `hogar`, `ropa` → `moda`, `otros` → `otros`).
  - `config.load_topics(path: Path = TOPICS_PATH) -> Topics` con `Topics(params: Mapping[str, TopicParams], rules: tuple[TopicRule, ...], intentional_otros: frozenset[str])`.

- [ ] **Step 1: Grabar los árboles de categorías** de las 4 tiendas VTEX: `uv run python -m discountz record-fixture --source <code> --kind category-tree` (agregar el `kind` a `record-fixture`: `GET {api_base}/catalog_system/pub/category/tree/1`).
- [ ] **Step 2: Escribir las pruebas que fallan** en `tests/unit/test_topics.py`:
  - `test_longest_prefix_wins`: reglas `("*", "/tecnologia/", "tecnologia")` y `("*", "/tecnologia/electrodomesticos/", "electro")` ⇒ `assign_topic(..., "exito", "/Tecnología/Electrodomésticos/Neveras/") == "electro"`.
  - `test_source_rule_beats_wildcard_at_same_length` y `test_unknown_path_goes_to_otros`.
  - `test_every_first_level_category_is_mapped` (parametrizada por tienda): cada hoja de primer nivel de la fixture cae en un tema distinto de `otros`, salvo las listadas en `intentional_otros`.
  - `test_topics_toml_matches_spec`: los 14 códigos de spec §6 en orden, con los `t_bp`/`g_bp` de la tabla (`tecnologia` 1200/5000, `moda` 3000/7500, `mercado` 2500/7000, `errores` y `comunidad_us` sin umbrales).
- [ ] **Step 3: Verificar que fallan** — `uv run pytest tests/unit/test_topics.py -q` → FAIL (`No module named discountz.domain.topics`).
- [ ] **Step 4: Implementar** `domain/topics.py`, `config/topics.toml` (temas + reglas por tienda construidas desde las fixtures de Step 1) y `load_topics` (pydantic estricto, como `load_params`). Agregar `pillow` y `cryptography` con `uv add` fijadas.
- [ ] **Step 5: Verificación completa** en verde.
- [ ] **Step 6: Commit** — `feat(temas): temas por categoría y asignación de rutas de tienda`

---

### Task 2: Detección pura del catálogo

Spec §3.

**Files:**
- Create: `src/discountz/domain/catalog.py`, `tests/unit/test_catalog.py`
- Modify: `src/discountz/domain/model.py` (`AlertKind.CATALOG_DROP = "catalog_drop"`, rango 2)

**Interfaces:**
- Consumes: `TopicParams` (Task 1), `GlobalParams` de la v1 (`min_abs_cop`, `min_abs_usd`, `improve_bp`).
- Produces:
  - `@dataclass(frozen=True, slots=True) class CatalogObservation: external_id: str; price: Money; category_path: str; ean: str | None; seller_class: SellerClass`
  - `@dataclass(frozen=True, slots=True) class CatalogState: price_minor: int; usual_minor: int | None; min_seen_minor: int | None; first_seen_at: datetime; stable_since: datetime; reads_at_price: int; last_changed_at: datetime | None; last_alert_at: datetime | None; last_alert_minor: int | None`
  - `@dataclass(frozen=True, slots=True) class CatalogCandidate: kind: AlertKind; price: Money; usual: Money; drop_bp: int; min_days: int`
  - `observe(state: CatalogState | None, obs: CatalogObservation, now: datetime, topic: TopicParams, glob: GlobalParams) -> tuple[CatalogState, CatalogCandidate | None, bool]` (el `bool` dice si cambió el precio y hay que registrar `catalog_price_change`).
  - `STABLE_FOR = timedelta(hours=24)`, `MIN_READS = 2`, `WARMUP = timedelta(days=3)`, `COOLDOWN = timedelta(hours=72)`.

- [ ] **Step 1: Escribir las pruebas que fallan**: los casos C01-C06 y C08 de spec §3.3 como `test_c01_drop_after_warmup` … `test_c08_small_saving_ignored`, con los valores exactos de la tabla, y además:
  - `test_first_observation_never_alerts` y `test_price_rise_updates_usual_only_after_stable`.
  - `test_list_price_is_ignored` (la observación no tiene `ListPrice`: el tipo no lo admite; la prueba documenta C04).
  - `test_never_alerts_without_usual` con hypothesis: para cualquier secuencia de precios con menos de `MIN_READS` lecturas estables, `observe` nunca devuelve candidato.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar `observe`.** `usual` pasa al precio actual cuando ese precio lleva `STABLE_FOR` y `MIN_READS` lecturas; `min_seen` solo baja con precios que cumplieron lo mismo (C05); `PRICE_ERROR` si `drop_bp ≥ g_bp` y existe `usual`; `CATALOG_DROP` además exige `WARMUP`, `drop_bp ≥ t_bp`, `price < min_seen` y ahorro absoluto mínimo por moneda; cooldown de 72 h salvo mejora ≥ `improve_bp` (C06). `min_days` = días desde `first_seen_at`, tope 90.
- [ ] **Step 4: Verificación completa.**
- [ ] **Step 5: Commit** — `feat(dominio): reglas de bajada de precio del catálogo`

---

### Task 3: Migración 0002 y repositorio del catálogo

Spec §7.1, §4.4 (columnas de seguimiento automático).

**Files:**
- Create: `migrations/0002_catalogo.up.sql`, `migrations/0002_catalogo.down.sql`, `tests/integration/test_catalog_db.py`
- Modify: `src/discountz/infra/db.py`, `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: `CatalogState`, `CatalogObservation` (Task 2).
- Produces (en `infra/db.py`):
  - `load_catalog_states(conn, source: str, external_ids: Sequence[str]) -> dict[str, CatalogState]`
  - `save_catalog_page(conn, source: str, rows: Sequence[CatalogRow], now: datetime) -> None` con `CatalogRow(external_id, ean, topic, currency, state: CatalogState, price_changed: bool)`: `INSERT … ON CONFLICT (source, external_id) DO UPDATE` en lote y `catalog_price_change` para los cambiados.
  - `hot_catalog_ids(conn, source: str, since: datetime, limit: int) -> list[str]` (por `last_changed_at DESC`).
  - `get_cursor(conn, source) -> CrawlCursor` y `save_cursor(conn, cursor: CrawlCursor) -> None` con `CrawlCursor(source, sweep_no, partition_idx, page, sweep_started_at, last_sweep_s, last_run_at, changed_since)`.
  - `acquire_crawl_lease(conn, source, holder, now, ttl) -> bool`, `release_crawl_lease(conn, source, holder) -> None`.
  - `recent_alert_for_ean(conn, ean: str, currency: str, price_minor: int, since: datetime) -> bool` (±1 %).
  - `mark_catalog_alert(conn, source, external_id, price_minor, now) -> None`.
  - `topic_threads(conn) -> dict[str, int]`, `save_topic(conn, code, title, thread_id) -> None`.
  - `enqueue(...)` gana `topic: str | None = None` y `card: Mapping[str, Any] | None = None`; `OutboxRow` gana `topic` y `card`.
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_migration_0002_up_down_up` (en `test_migrations.py`); en `test_catalog_db.py`: `test_save_page_upserts_and_logs_only_changes`, `test_hot_ids_ordered_by_recent_change`, `test_cursor_roundtrip`, `test_crawl_lease_is_exclusive_per_source` (dos fuentes distintas sí pueden tomarlo a la vez), `test_recent_alert_for_ean_tolerance` (99.000 vs 100.000 ⇒ sí; 98.000 ⇒ no), `test_enqueue_with_card_and_topic`, `test_watch_origin_defaults_manual`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Escribir la migración** exactamente como spec §7.1, más: `alert.kind` acepta `catalog_drop`; triggers `set_updated_at` en las tablas nuevas que tengan `updated_at`; `GRANT` a `discountz_app` en las tablas y secuencias nuevas (mismo bloque `DO` que 0001); `CHECK (topic ~ '^[a-z_]{2,20}$')` en `catalog_item.topic` y `outbox.topic`. El `.down.sql` revierte todo.
- [ ] **Step 4: Implementar las funciones de `infra/db.py`.**
- [ ] **Step 5: Verificación completa + integración.**
- [ ] **Step 6: Commit** — `feat(bd): migración 0002 con catálogo, cursores y temas`

---

### Task 4: Contrato de catálogo y catálogo VTEX

Spec §2.1 (VTEX), §0.3.

**Files:**
- Modify: `src/discountz/sources/base.py`, `src/discountz/sources/vtex.py`, `src/discountz/__main__.py` (`record-fixture --kind catalog`)
- Create: `tests/fixtures/vtex/<tienda>-catalog.json` + `.meta.json` (×4, reales, grabadas desde la nube), `tests/sources/test_vtex_catalog.py`

**Interfaces:**
- Produces (en `sources/base.py`):
  - `Capability.CATALOG = "catalog"`; `SourceDescriptor.image_hosts: frozenset[str] = frozenset()` y `SourceDescriptor.supports_changed: bool = False`.
  - `@dataclass(frozen=True, slots=True) class CatalogPage: items: Sequence[CatalogObservation]; has_more: bool`
  - `class CatalogSource(Protocol)`: `catalog_partitions(ctx) -> Sequence[str]`, `fetch_catalog_page(ctx, partition: str, page: int) -> CatalogPage`, `fetch_changed(ctx, since: datetime, page: int) -> CatalogPage` (VTEX lanza `NotImplementedError`; `app/crawl` solo lo llama si `descriptor.supports_changed`).
  - `@dataclass(frozen=True, slots=True) class CardFacts: title: str; url: str; image_url: str | None` y `fetch_card_facts(ctx, external_ids: Sequence[str]) -> dict[str, CardFacts]` en `CatalogSource` (VTEX: el mismo lote `fq=skuId:`).
- `VtexSource`: partición = `"C:/<id>/"` o `"C:/<id>/|P:[<a> TO <b>]"`; página de 50 con `fq=isAvailablePerSalesChannel_1:1`; `image_hosts = {"<cuenta>.vtexassets.com"}` (`exitocol`, `carulla`, `olimpica`, `panamericana`).
- [ ] **Step 1: Grabar fixtures reales** de una página de catálogo por tienda (`record-fixture --kind catalog --partition C:/<id>/`), más una cabecera `resources` con total > 2.500 para la prueba de partición (guardar en `.meta.json` las cabeceras).
- [ ] **Step 2: Escribir las pruebas que fallan**: `test_catalog_page_parses_real_fixture` (por tienda: > 0 ítems, todos con precio > 0 y `category_path` que empieza por `/`), `test_catalog_page_takes_cheapest_seller_in_stock` (fixture sintética mínima: vendedor por defecto con precio 0 y otro con 59.900 en stock ⇒ 59.900), `test_partitions_split_large_leaf_by_price` (`FakeHttp`: hoja con total 6.000 ⇒ rangos hasta que cada uno ≤ 2.500), `test_card_facts_from_batch`, `test_catalog_has_more_from_resources_header`.
- [ ] **Step 3: Verificar que fallan.**
- [ ] **Step 4: Implementar** el contrato y `VtexSource` (`catalog_partitions` lee `category/tree/3` y hace el *split* con peticiones `_from=0&_to=0` para leer el total).
- [ ] **Step 5: Verificación completa.**
- [ ] **Step 6: Commit** — `feat(vtex): recorrido del catálogo por hojas de categoría`

---

### Task 5: Credenciales por entorno

Spec §2.1 (clave de Best Buy), §4.5 (Walmart).

**Files:**
- Create: `src/discountz/infra/credentials.py`, `tests/unit/test_credentials.py`
- Modify: `src/discountz/sources/base.py` (`CredentialStore.signed_headers`), `src/discountz/config.py` (`Settings`), `src/discountz/__main__.py` (`check-config`), `.env.example`

**Interfaces:**
- Produces:
  - `CredentialStore` gana `signed_headers(self, provider: str, now: datetime) -> Mapping[str, str]`; `NoCredentials` lanza `CredentialUnavailable`.
  - `Settings`: `bestbuy_api_key: SecretStr | None`, `walmart_consumer_id: str | None`, `walmart_key_version: str | None`, `walmart_private_key: SecretStr | None`, `walmart_publisher_id: str | None` (con prefijo `DISCOUNTZ_`).
  - `EnvCredentials(settings: Settings)`: `bearer("bestbuy")` devuelve la clave; `signed_headers("walmart", now)` devuelve `WM_CONSUMER.ID`, `WM_CONSUMER.INTIMESTAMP` (ms), `WM_SEC.KEY_VERSION`, `WM_SEC.AUTH_SIGNATURE` (RSA PKCS#1 v1.5 + SHA-256 sobre `"{consumer_id}\n{ts}\n{key_version}\n"`, Base64).
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_walmart_signature_verifies` (clave RSA generada en la prueba; se verifica la firma con la pública), `test_walmart_requires_all_four_or_none` (`check-config` falla con 1-3 de 4), `test_bestbuy_missing_raises_credential_unavailable`, `test_repr_hides_secrets`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar.** `app/collect.py` pasa a usar `EnvCredentials` en `ctx()` (inyectado por `CycleDeps.credentials`).
- [ ] **Step 4: Verificación completa.**
- [ ] **Step 5: Commit** — `feat(config): credenciales de Best Buy y Walmart por entorno`

---

### Task 6: Best Buy por API oficial

Spec §0.4, §2.1 (Best Buy), §4.4 (Best Buy por API).

**Files:**
- Modify: `src/discountz/sources/bestbuy.py` (reemplaza el HTML por la API; conserva `parse_url`), `tests/sources/test_bestbuy.py`, `config/discountz.toml` (`[sources.bestbuy_us]` y `[crawl.bestbuy_us]`)
- Create: `tests/fixtures/bestbuy_us/api-products.json`, `api-catalog.json`, `api-changed.json` + `.meta.json` con `"nota"` (sintéticas desde la documentación hasta grabar con la clave de David)
- Delete: fixtures HTML de `tests/fixtures/bestbuy_us/` que ya no usa ninguna prueba

**Interfaces:**
- Consumes: `CatalogSource`, `CardFacts`, `Capability.CATALOG` (Task 4), `CredentialStore.bearer("bestbuy")` (Task 5).
- Produces: `bestbuy_us` con `auth="api_key"`, `client="httpx"`, capacidades `DETAIL`, `SEARCH`, `CONFIRM`, `CATALOG`, `supports_changed=True`, `max_batch=100`, `image_hosts={"pisces.bbystatic.com"}`. Base `https://api.bestbuy.com/v1`. `fetch_listings`: `products(sku in(…))`; `search`: `products(search=<palabra>&…)`; `confirm`: `products/<sku>.json`; catálogo: `products(active=true&orderable!=NotAvailable)` con `sort=sku.asc`, `pageSize=100`, `show=sku,salePrice,regularPrice,categoryPath.name,upc,onlineAvailability`; `fetch_changed`: `products(priceUpdateDate>{since ISO})`.
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_parse_products_api` (precio USD exacto en centavos, `in_stock` desde `onlineAvailability`), `test_catalog_path_joins_category_names`, `test_changed_uses_price_update_date`, `test_api_key_never_in_errors` (un 403 con la clave en la URL produce un `SourceError` cuyo `str` no la contiene), `test_parse_url_unchanged` (las URLs `/site/…/<sku>.p?skuId=` de la v1 siguen resolviendo).
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar** con modelos pydantic estrictos; `detect_block`: 403 con cuerpo de cuota ⇒ `SourceError("quota")`, no bloqueo.
- [ ] **Step 4: Verificación completa.**
- [ ] **Step 5: Commit** — `feat(bestbuy): lectura por la API oficial con catálogo y cambios`
- [ ] **Step 6 (cuando David tenga la clave):** David corre `record-fixture --source bestbuy_us --kind catalog` en su PC; se reemplazan las sintéticas y se quita la `"nota"`. Commit `test(bestbuy): fixtures reales de la API`.

---

### Task 7: Walmart lista para activar por API

Spec §4.5.

**Files:**
- Modify: `src/discountz/sources/walmart.py`, `tests/sources/test_walmart.py`
- Create: `tests/fixtures/walmart_us/api-items.json` + `.meta.json` con `"nota"`

**Interfaces:**
- Consumes: `CredentialStore.signed_headers("walmart", now)` (Task 5).
- Produces: `WalmartSource.fetch_listings` y `search` usan `https://developer.api.walmart.com/api-proxy/service/affil/product/v2/items?ids=…` (≤ 20) y `/search?query=…` cuando `signed_headers` no lanza `CredentialUnavailable`; si lanza, el HTML de la v1 sin cambios. `image_hosts={"i5.walmartimages.com"}`.
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_api_mode_when_credentials` (`FakeHttp` recibe la URL de la API y las 4 cabeceras), `test_html_mode_without_credentials` (las pruebas de HTML de la v1 siguen pasando), `test_parse_api_items` (`salePrice` 129.99 ⇒ 12999 USD, `stock: "Available"` ⇒ en stock, `publisherId` en la query).
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Verificación completa.**
- [ ] **Step 5: Commit** — `feat(walmart): modo API de afiliados listo para activar`

---

### Task 8: Caso de uso `crawl`

Spec §2.2, §2.3, §3.2 (duplicados), §4.1-§4.3, §8.

**Files:**
- Create: `src/discountz/app/crawl.py`, `tests/integration/test_crawl.py`
- Modify: `src/discountz/config.py` (`[crawl.<code>]`: `min_pause_s`, `jitter_s`, `max_per_24h`, `max_per_run`, `hot_share_bp = 2500`), `config/discountz.toml` (valores de spec §2.3)

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces:
  - `@dataclass(frozen=True) class CrawlDeps: conn; source: CatalogSource & Source; http: HttpClient; credentials: CredentialStore; topics: Topics; params: AppParams; budget: CrawlBudget; holder: str; monotonic; sleep; rnd`
  - `run_crawl(deps: CrawlDeps, now: datetime, minutes: int) -> CrawlResult` con `CrawlResult(status: Literal["ok","partial","blocked","skipped_locked","disabled"], pages: int, items: int, changes: int, alerts: int, requests: int)`.
  - Alerta confirmada ⇒ `upsert_listing` + `write_sample` (v1), `insert_alert(kind=price_error|catalog_drop)` y `enqueue(purpose="alert", topic=<tema>, card={…}, priority=…, ttl=24 h)`; `PRICE_ERROR` encola además una línea de texto en el tema de su categoría (`dedup_key=f"pe-line:{alert_id}"`). Prioridad: `PRICE_ERROR` 0; `CATALOG_DROP` `max(2, 9 − drop_bp // 1000)`.
  - `card` = `{"store": código, "topic": código, "title", "url", "image_url", "price_minor", "usual_minor", "currency", "drop_bp", "min_days", "kind"}`.
- [ ] **Step 1: Escribir las pruebas que fallan** (fuente falsa con páginas programadas):
  - `test_crawl_resumes_from_cursor_without_duplicates` (corte por plazo en la página 2 de 4; la segunda corrida empieza en la 2 y el total de `catalog_price_change` es el esperado).
  - `test_crawl_hot_tier_first_and_capped` (≤ 25 % de las peticiones de la corrida).
  - `test_crawl_hot_tier_uses_fetch_changed_when_supported` (Best Buy: `fetch_changed(since=cursor.changed_since)` y luego `changed_since` = inicio de la corrida).
  - `test_crawl_three_parse_errors_in_a_row_stop` (una página con error se salta; tres seguidas detienen la corrida con `partial`).
  - `test_crawl_confirmation_mismatch_drops_candidate` (la relectura da +2 % ⇒ sin alerta ni outbox).
  - `test_crawl_ean_duplicate_is_skipped` (C07).
  - `test_crawl_block_trips_breaker_and_stops` (403 ⇒ `status="blocked"`, breaker abierto, cursor guardado).
  - `test_crawl_disabled_source_does_nothing` y `test_crawl_lease_held_skips`.
  - `test_crawl_enqueues_card_with_topic` (campos de `card` exactos para C01).
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar** `run_crawl` con los pasos de spec §2.2; contar peticiones en `source_hourly_stat` de la v1 para el tope de 24 h.
- [ ] **Step 4: Verificación completa + integración.**
- [ ] **Step 5: Commit** — `feat(rastreo): caso de uso de rastreo del catálogo`

---

### Task 9: Tarjeta PNG

Spec §5.2 (maqueta `/mnt/project-files/rastreo-catalogo/maqueta-tarjeta.png`).

**Files:**
- Create: `src/discountz/infra/card.py`, `assets/fonts/Inter-{Regular,SemiBold,Bold,ExtraBold,Black}.otf`, `assets/fonts/OFL.txt`, `tests/unit/test_card.py`, `tests/golden/card-*.png`
- Modify: `pyproject.toml` (incluir `assets/` en el paquete o resolverlo desde la raíz como `config/`)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, slots=True) class CardData: store_name: str; store_color: str; topic_title: str; price: Money; usual: Money; drop_bp: int; min_days: int; kind: AlertKind`
  - `render_card(data: CardData, photo: bytes | None) -> bytes` (PNG 1080×1080).
  - `validate_photo(raw: bytes) -> bytes | None` (≤ 5 MB, JPEG/PNG/WebP, `Image.verify`; si no, `None`).
  - `STORE_COLORS: Mapping[str, str]`.
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_card_is_1080_png`, `test_card_without_photo_draws_store_box`, `test_card_without_photo_on_corrupt_bytes`, `test_card_without_photo_on_huge_file`, `test_card_usd_shows_casillero_line`, `test_card_price_error_banner`, `test_card_matches_golden` (hash perceptual `average_hash` propio de 8×8 con distancia ≤ 5 contra `tests/golden/card-c01.png`).
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar** siguiendo la maqueta: píldora de tienda, `#Tema`, recuadro blanco con la foto escalada sin deformar, insignia `-NN%` «vs. su precio», precio en verde, «Antes» tachado (la línea cruza solo el monto), «Mínimo en N días» o «POSIBLE ERROR DE PRECIO», «🇺🇸 Compra por casillero» en USD, marca `discountZ`.
- [ ] **Step 4: Verificación completa.**
- [ ] **Step 5: Commit** — `feat(tarjeta): imagen de alerta con foto y precio`

---

### Task 10: Telegram con fotos y temas

Spec §5.1, §5.3.

**Files:**
- Modify: `src/discountz/infra/http.py` (`files` y `data` en `request` de `HttpClient`, `HttpxClient`, `CurlClient`, `PacedClient`, `FakeHttp`), `src/discountz/infra/telegram.py`, `tests/unit/test_telegram.py`, `tests/unit/test_http.py`

**Interfaces:**
- Produces:
  - `HttpClient.request(..., data: Mapping[str, str] | None = None, files: Mapping[str, tuple[str, bytes, str]] | None = None)`.
  - `TelegramClient.send_message(..., thread_id: int | None = None)`.
  - `TelegramClient.send_photo(chat_id: int, png: bytes, caption_html: str, reply_markup: Mapping[str, Any] | None, *, silent: bool, thread_id: int | None) -> int`.
  - `TelegramClient.create_forum_topic(chat_id: int, name: str) -> int` (devuelve `message_thread_id`).
  - Error nuevo `TelegramThreadNotFound(TelegramBadRequest)` cuando la descripción contiene `message thread not found`.
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_send_photo_multipart` (`FakeHttp` recibe `files={"photo": ("alerta.png", …, "image/png")}` y `data` con `chat_id`, `caption`, `parse_mode`, `message_thread_id`, `reply_markup` en JSON), `test_send_message_with_thread`, `test_create_forum_topic_returns_thread_id`, `test_thread_not_found_error`, `test_paced_client_passes_files`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Verificación completa.**
- [ ] **Step 5: Commit** — `feat(telegram): fotos, temas y subida multipart`

---

### Task 11: Entrega con tarjeta y tema

Spec §5.1, §5.3, §5.4.

**Files:**
- Modify: `src/discountz/app/deliver.py`, `src/discountz/app/compose.py` (tema de las alertas de la v1 vía `CATEGORY_TOPIC`; Slickdeals ⇒ `comunidad_us`), `src/discountz/app/cycle.py` (pasa `http` de fotos y `sources` a `deliver`), `tests/integration/test_deliver.py`

**Interfaces:**
- Consumes: `render_card`, `validate_photo` (Task 9), `send_photo`, `TelegramThreadNotFound` (Task 10), `topic_threads` (Task 3), `SourceDescriptor.image_hosts` (Task 4).
- Produces: `deliver(conn, tg, chat_id, now, *, sleep, monotonic, deadline, photos: PhotoFetcher, limit=15)` con `PhotoFetcher = Callable[[str, str], bytes | None]` (`(source_code, image_url)`; `None` si el host no está en `image_hosts`, si falla o si no es imagen).
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_deliver_card_row_sends_photo_to_topic`, `test_deliver_text_row_goes_to_topic_thread`, `test_deliver_falls_back_to_general_when_topic_missing` (se reenvía sin `thread_id` y se encola una sola vez el aviso «Corre /temas»), `test_deliver_skips_photo_from_foreign_host` (`photos` nunca llama a la red con un host ajeno; la tarjeta sale sin foto), `test_compose_assigns_topic_from_category`, `test_deliver_order_price_error_first`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar.** El caption reutiliza `render.esc`/`truncate` (≤ 1.024 caracteres) y los botones de la v1 (`Abrir en <tienda>`, `Seguir`, `Silenciar 30 d`).
- [ ] **Step 4: Verificación completa + integración.**
- [ ] **Step 5: Commit** — `feat(entrega): alertas con imagen en el tema de su categoría`

---

### Task 12: Comandos `/temas`, `/seguir` en lote y seguimientos automáticos en `/lista`

Spec §5.1 (`/temas`), §4.4.

**Files:**
- Modify: `src/discountz/domain/commands.py` (`MAX_URLS = 20`; `Topics()`; `/dejar a12` y `/dejar auto`), `src/discountz/app/inbox.py`, `src/discountz/app/render.py` (`/lista` con `a12`), `src/discountz/infra/db.py` (`drop_auto_watches`), `tests/unit/test_commands.py`, `tests/integration/test_inbox.py`

**Interfaces:**
- Consumes: `create_forum_topic` (Task 10), `save_topic`/`topic_threads` (Task 3), `TOPIC_CODES`/`TopicParams.title` (Task 1).
- Produces: `parse_command("/temas") -> Topics`; `drop_auto_watches(conn) -> int`; `db.drop_ref` acepta `a<id>`.
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_follow_accepts_20_urls_rejects_21`, `test_temas_creates_missing_topics_once` (segunda vez no llama a `create_forum_topic`), `test_list_marks_auto_watches`, `test_drop_auto_removes_only_auto`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar.** `/temas` llama a `createForumTopic` en el momento (no por la outbox) y responde con los temas creados; las respuestas del bot van a General (sin `thread_id`).
- [ ] **Step 4: Verificación completa + integración.**
- [ ] **Step 5: Commit** — `feat(comandos): /temas, /seguir en lote y seguimientos automáticos`

---

### Task 13: Seguimiento automático desde Slickdeals

Spec §4.4.

**Files:**
- Modify: `src/discountz/app/collect.py` (`_feed`), `src/discountz/infra/db.py` (`create_auto_watch`, `extend_auto_watch`, `count_auto_watches`), `src/discountz/app/maintenance.py` (vencer automáticos), `src/discountz/config.py` + `config/discountz.toml` (`max_auto_watches`, Amazon 300; las demás 0 salvo Newegg 100), `tests/integration/test_auto_watch.py`

**Interfaces:**
- Produces: `create_auto_watch(conn, source: str, ref: ListingRef, now: datetime, ttl: timedelta) -> int | None` (`None` si ya existe un watch para ese ref o se llegó al tope); `extend_auto_watch(conn, listing_id: int, until: datetime) -> None` (al crear una alerta); `expire_auto_watches(conn, now) -> int`.
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_every_routable_lead_creates_auto_watch` (aunque no coincida con ninguna búsqueda), `test_auto_watch_cap_keeps_newest`, `test_alert_extends_auto_watch_30_days`, `test_maintenance_expires_old_auto_watches`, `test_manual_watch_not_overwritten_by_auto`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar.** El seguimiento automático usa `pending_ref = "id:<external_id>"` y el resto de la maquinaria de la v1 sin cambios.
- [ ] **Step 4: Verificación completa + integración.**
- [ ] **Step 5: Commit** — `feat(seguimiento): seguir solos los productos de Slickdeals`

---

### Task 14: Mantenimiento y resumen diario del catálogo

Spec §7.2, §8.

**Files:**
- Modify: `src/discountz/app/maintenance.py`, `src/discountz/infra/db.py`, `src/discountz/app/render.py` (`render_daily`), `tests/integration/test_maintenance.py`, `tests/unit/test_render.py`

**Interfaces:**
- Produces: `delete_old_catalog_changes(conn, cutoff) -> int`, `delete_unseen_catalog_items(conn, cutoff) -> int`, `catalog_daily_stats(conn, now) -> CatalogDaily` (`items_by_source`, `changes_today`, `alerts_by_topic`, `last_sweep_s_by_source`).
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_catalog_retention_90d_and_unseen_30d`, `test_size_guard_400_shortens_retention_to_45d`, `test_size_guard_450_pauses_exito_carulla_crawl` (`crawl_cursor` o `bot_state` marca pausa y `run_crawl` responde `disabled`), `test_daily_summary_has_catalog_lines`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Verificación completa + integración.**
- [ ] **Step 5: Commit** — `feat(mantenimiento): retención y resumen del catálogo`

---

### Task 15: CLI `crawl`, workflow y documentación de puesta en marcha

Spec §1.2, §2.4, §10.

**Files:**
- Modify: `src/discountz/__main__.py` (`crawl --source <code> [--minutes 45]`), `tests/arch/test_smoke.py`, `README.md`, `docs/estado.md`, `CLAUDE.md`, `docs/architecture.md`, `.github/workflows/cycle.yml` (secretos nuevos)
- Create: `.github/workflows/crawl.yml`, `/mnt/project-files/rastreo-catalogo/pasos-telegram.md` (guía de copiar y pegar para David, fuera del repo)

**Interfaces:**
- Consumes: `run_crawl` (Task 8), `EnvCredentials` (Task 5).
- Produces: `crawl.yml` con `workflow_dispatch` + `schedule: '17 */3 * * *'`, `if: vars.DISCOUNTZ_RASTREO == 'true'`, matriz `source: [exito, carulla, olimpica, panamericana, bestbuy_us]`, `fail-fast: false`, `timeout-minutes: 55`, `concurrency: discountz-crawl-${{ matrix.source }}`, acciones fijadas por SHA como `cycle.yml`, secretos `DATABASE_URL`, `BESTBUY_API_KEY` (y los 4 de Walmart en `cycle.yml`). Código de salida: 0 en `ok`/`partial`/`skipped_locked`/`disabled`, 3 en `blocked` (para verlo en Actions sin reintentos).
- [ ] **Step 1: Escribir las pruebas que fallan**: `test_cli_help_lists_crawl`, `test_crawl_unknown_source_exits_config` (código 2), `test_crawl_workflow_matrix_matches_catalog_sources` (lee `crawl.yml` y exige las fuentes con `Capability.CATALOG`).
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar** la CLI y el workflow.
- [ ] **Step 4: Documentar**: README (pasos de spec §10 de copiar y pegar: BotFather `/setjoingroups`, grupo con temas, id del grupo con `getUpdates`, `/temas`, clave de Best Buy y `/fuente on bestbuy_us`, secretos de Walmart cuando aprueben, `DISCOUNTZ_RASTREO=true`, migración 0002 y el disparo horario en cron-job.org); `docs/estado.md` reescrito; `CLAUDE.md` (estructura: `crawl`, `assets/`, `config/topics.toml`; gotchas: `ListPrice` inflado, `vtexassets` bloqueado desde la nube de la sesión); `docs/architecture.md` (caso de uso `crawl` y tablas nuevas).
- [ ] **Step 5: Verificación completa + integración.**
- [ ] **Step 6: Commit** — `feat(rastreo): comando crawl, workflow horario y guía de activación`
- [ ] **Step 7: PR** con la plantilla del repo, a nombre de David, en español; Squash and merge cuando David lo apruebe.
