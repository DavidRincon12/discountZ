# Mercado Libre Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (Native, el modo por defecto de David) or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** seguir productos del catálogo de Mercado Libre Colombia hallados por los EAN de las tiendas colombianas y alertar sus bajadas de precio de vendedores confiables.

**Architecture:** adaptador `sources/mercadolibre.py` que habla solo con la API (token de app en memoria) e implementa el protocolo nuevo `EanCatalogSource`. Para esas fuentes, el rastreo (`app/crawl.py`) cambia la capa caliente y la vuelta por la relectura de los seguidos (`catalog_item.checked_at`) y una capa de semillas por EAN (`ean_lookup`). Migración `0017`, ADR 0018.

**Tech Stack:** Python 3.13, pydantic v2, `json` con `parse_float=Decimal`, psycopg 3, pytest + hypothesis, fixtures en `tests/fixtures/mercadolibre/`.

**Spec:** `docs/superpowers/specs/2026-10-29-mercadolibre-design.md`.

## Global Constraints

- Dinero en enteros COP (`Money(…, Currency.COP)`, `to_minor` de `domain/money.py`); nunca `float`; JSON con `parse_float=Decimal`. `original_price` nunca se lee.
- Solo `https://api.mercadolibre.com`; ninguna petición a la web y ningún bloqueo se sortea.
- El token y el secreto nunca van a logs, `repr`, BD ni fixtures. Logs solo con `infra.log.event` y campos de `ALLOWED_FIELDS`.
- Valores del spec: semillas de temas `card`, en COP, con precio ≥ `min_price_cop` (100.000) y del más caro al más barato; `ean_lookup` por 30 días; relectura pasadas 6 h; a lo sumo 3 consultas de `/users` por producto; `[crawl.mercadolibre]` = 0.5 / 0.5 / 2000 / 20000 / 7000; `[mercadolibre] max_items = 12000`; `[sources.mercadolibre]` = 1 / 1 / 10 / 1000 / 120 / 1440 / 1.
- Todo texto redactado en español; commits `tipo(alcance): descripción` a nombre de David, sin atribución a IA; `bash .claude/hooks/git-guard.sh check` antes de cada push.
- Verificación completa: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest -q`. Integración local: `sudo pg_ctlcluster 16 main start`, contraseña `postgres` para el usuario `postgres` y `DISCOUNTZ_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest -q -m integration`.

## Review Focus

- Dos EAN semilla que llevan al mismo producto en un lote: una sola fila y una sola lectura de vendedores (Task 8).
- Corrida que para a mitad de un lote (presupuesto, plazo o bloqueo): lo ya buscado queda anotado y lo leído, guardado (Task 8).
- Producto hallado sin vendedores (404): queda en `ean_lookup` con su id, no entra al catálogo y no se busca otra vez en 30 días (Task 8).
- Precio con decimales: se redondea a pesos y el vendedor sigue contando (Task 4).
- `fetch_card_facts` sin una confirmación previa en memoria: vuelve a leer el producto y los vendedores (Task 5).

---

### Task 1: Plan, ADR 0018 y arquitectura

**Files:**
- Create: `docs/decisions/0018-mercadolibre-por-su-catalogo-sembrado-con-ean.md`
- Modify: `docs/architecture.md` (Contexto: la API de Mercado Libre; Modelo de datos: párrafo de `0017`; Flujos clave: rastreo sembrado por EAN)
- Add: este plan

- [ ] **Step 1:** ADR con la plantilla de `arquitectura-y-datos`. Decisión: M1 a M6 del spec, resumidas. Alternativas: búsqueda por palabras (10 a 24 % con vendedores, corte en 300), recorrido por categorías (no existe), `/highlights` y búsqueda general (403), la web (302 de verificación; no se evade). Cuándo revisar: un 429, el tope de 12 mil lleno con relectura de más de un día, la BD en ≥ 450 MB o un 403 en `/products/{id}/items`.
- [ ] **Step 2:** commit `docs(mercadolibre): plan, ADR 0018 y arquitectura`.

### Task 2: Fixtures reales con un workflow temporal

**Files:**
- Create (temporal): `.github/workflows/tmp-fixtures-meli.yml`
- Create (las commitea el workflow): `tests/fixtures/mercadolibre/{search-ean,search-ean-no-zeros,search-empty,items,items-none,product,user-leader}.{json.gz,meta.json}`

- [ ] **Step 1:** workflow como `tmp-fixtures.yml` de `e2ce821^`: push a `claude/project-thread-xsztn5` con `paths` en el propio archivo, `permissions: contents: write`, `uv sync --locked --no-dev`, identidad de David, commit y push de las fixtures. Un script Python en línea pide el token (POST de formulario con `MELI_CLIENT_ID` y `MELI_CLIENT_SECRET` del entorno; nunca se imprime ni se guarda), hace cada GET con `Authorization: Bearer`, 0,5 s entre peticiones, recorta el cuerpo y lo guarda con `save_fixture(FIXTURES_DIR, "mercadolibre", nombre, resp, recorded_at=hoy)`, con una clave `"recorte"` en el `.meta.json`:
  - `search-ean`, `search-ean-no-zeros` y `search-empty`: `/products/search?status=active&site_id=MCO&product_identifier=` con `0740617344806`, `740617344806` y `0195950637151`. Quedan `paging` y `results[].{id, name, domain_id, status}`.
  - `items`: `/products/MCO18924972/items`, cada resultado solo con `seller_id, price, original_price, currency_id, condition, official_store_id, international_delivery_mode, listing_type_id, shipping, tags, deal_ids` (sin `seller_address`).
  - `items-none`: `/products/MCO24785005/items` (se espera 404).
  - `product`: `/products/MCO18924972` con `id, name, status, domain_id` y las 3 primeras `pictures`.
  - `user-leader`: `/users/<primer seller_id de items sin tienda oficial>` con `id, user_type, status` y `seller_reputation.{level_id, power_seller_status}`.
- [ ] **Step 2:** commit `ci(tmp): graba fixtures de Mercado Libre desde Actions`, `git-guard check` y push; esperar el commit del workflow y `git pull`.
- [ ] **Step 3:** comprobar que existen los 7 pares, que cada `.json.gz` es JSON y que `zgrep -c access_token tests/fixtures/mercadolibre/*.gz` da 0 en todos.
- [ ] **Step 4:** borrar el workflow; commit `ci(tmp): quita el workflow de fixtures de Mercado Libre` y push.

### Task 3: GTIN válido de las semillas

**Files:**
- Create: `src/discountz/domain/gtin.py`
- Test: `tests/unit/test_gtin.py`

**Interfaces:**
- Produces: `gtin_ok(code: str) -> bool`.

- [ ] **Step 1: pruebas que fallan.**
  - `test_gtin_ok_accepts_valid_codes`: `7702004003508`, `0740617344806`, `850049670821`, `96385074`, `00740617344806` y `9780306406157` ⇒ `True`.
  - `test_gtin_ok_rejects_bad_check_digit`: `6921732899395` ⇒ `False`.
  - `test_gtin_ok_rejects_restricted_circulation` (todos con dígito de control correcto): `2001234567893`, `2900000000018`, `0200000000011`, `0400000000015`, `200000000004`, `400000000008`, `00000017` y `20000011` ⇒ `False`.
  - `test_gtin_ok_rejects_other_shapes`: `""`, `"123456"`, `"12345678901"`, `"77020040035O8"` y `"17702004003505"` (14 dígitos sin 0 adelante) ⇒ `False`.
  - `test_gtin_ok_holds_for_any_computed_check_digit` (hypothesis): 12 dígitos cuyo primero no es 0 ni 2, más su dígito calculado ⇒ `True`.
- [ ] **Step 2:** `uv run pytest tests/unit/test_gtin.py -q` falla por importación.
- [ ] **Step 3: implementar.** Solo dígitos y largos 8, 12, 13 o 14; al de 14 se le quita el 0 de adelante. Dígito de control GS1: pesos 3 y 1 desde la derecha. Restringido: 8 dígitos que empiezan por 0 o 2; en los demás, llevados a 13 con ceros a la izquierda, los que empiezan por `02`, `04` o `2`.
- [ ] **Step 4:** en verde, `uv run lint-imports` (dominio puro) y commit `feat(mercadolibre): valida GTIN de las semillas`.

### Task 4: Lectura pura de las respuestas de Mercado Libre

**Files:**
- Create: `src/discountz/sources/mercadolibre.py`
- Test: `tests/sources/test_mercadolibre.py`

**Interfaces:**
- Produces, como dataclasses congeladas:
  - `MeliSeller(seller_id: int, price: Money, official: bool, free_shipping: bool)` y `MeliProduct(name: str, image_url: str | None)`.
  - `parse_search(body: bytes) -> str | None`: el `id` del primer resultado con forma `MCO\d{1,12}`.
  - `parse_sellers(body: bytes) -> list[MeliSeller]`: nuevos, con `international_delivery_mode == "none"`, en COP y con precio > 0, de menor a mayor precio (empate por `seller_id`).
  - `parse_product(body: bytes) -> MeliProduct`.
  - `parse_leader(body: bytes) -> str | None`: `"platinum"` o `"gold"` si `level_id == "5_green"`; si no, `None`.

- [ ] **Step 1: pruebas que fallan.** Usan un helper `_edit(name, change)` que carga la fixture como dict, la cambia y la vuelve a serializar, como en `test_homecenter.py`.
  - `test_fixtures_are_recorded_without_token`: los 7 `.json.gz` existen, son JSON y no contienen `access_token`.
  - `test_parse_search_returns_first_product_id`: la fixture de búsqueda con resultados ⇒ su `results[0].id`; `search-empty` ⇒ `None`.
  - `test_parse_sellers_orders_eligible_by_price`: precios ascendentes, todos en COP, y el primero es el menor `price` de los elegibles de `items`.
  - `test_parse_sellers_skips_used_international_and_other_currency`: con `condition="used"`, `international_delivery_mode="CBT"` y `currency_id="USD"` en tres vendedores, ninguno de los tres sale.
  - `test_parse_sellers_skips_an_invalid_entry`: un vendedor sin `seller_id` y otro con `price` de texto se descartan; los demás salen.
  - `test_parse_sellers_rounds_decimal_prices`: `price = 457424.5` ⇒ `Money(457425, Currency.COP)`.
  - `test_parse_sellers_rejects_a_body_without_results`: `b"{}"` y `b"<html>"` ⇒ `ParseError` con código `bad_items`.
  - `test_parse_product_reads_name_and_first_cdn_picture`: nombre no vacío y foto `https://http2.mlstatic.com/…`; con la foto en otro host o en `http://`, `image_url is None`; sin `name`, `ParseError` con código `bad_product`.
  - `test_parse_leader_only_platinum_or_gold_in_green`: (platinum, 5_green) ⇒ `"platinum"`; (gold, 5_green) ⇒ `"gold"`; (silver, 5_green), (platinum, 4_light_green) y (null, null) ⇒ `None`.
- [ ] **Step 2:** `uv run pytest tests/sources/test_mercadolibre.py -q` falla por importación.
- [ ] **Step 3: implementar** con modelos pydantic estrictos (`extra="ignore"`, `hide_input_in_errors=True`), validando cada vendedor por separado para descartar solo el inválido, y con `to_minor(Decimal(...), Currency.COP)`.
- [ ] **Step 4:** en verde y commit `feat(mercadolibre): lee búsqueda, vendedores, producto y reputación`.

### Task 5: `MercadoLibreSource` con token, regla de vendedor y contrato

**Files:**
- Modify: `src/discountz/sources/mercadolibre.py`, `src/discountz/sources/base.py`, `src/discountz/sources/registry.py` y la matriz de `.github/workflows/crawl.yml`
- Test: `tests/sources/test_mercadolibre.py`, `tests/sources/test_registry.py`

**Interfaces:**
- Consumes: Task 4.
- Produces:
  - En `sources/base.py`: `@runtime_checkable class EanCatalogSource(CrawlSource, Protocol)` con `find_by_ean(self, ctx: SourceContext, ean: str) -> str | None`.
  - `MercadoLibreSource()`, registrada como `mercadolibre` y con el descriptor del spec §1.1. Métodos: `find_by_ean`, `fetch_catalog_items(ctx, ids) -> list[CatalogObservation]` (con `category_path=""` y `ean=None`), `fetch_catalog_listings` y `fetch_listings(ctx, refs) -> list[ListingOutcome]`, `confirm(ctx, offer) -> Offer | None`, `fetch_card_facts(ctx, ids) -> dict[str, CardFacts]`, `parse_url` y `detect_block`. `search`, `catalog_partitions`, `fetch_catalog_page`, `fetch_changed` y `fetch_leads` devuelven vacío, y `resolve` lanza `ParseError("no_resolution")`.
  - Constantes: `CODE = "mercadolibre"`, `API = "https://api.mercadolibre.com"`, `WEB = "https://www.mercadolibre.com.co"`, `RENEW_BEFORE = timedelta(minutes=10)` y `MAX_SELLER_LOOKUPS = 3`.

- [ ] **Step 1: pruebas que fallan.** Usan los fakes de `test_ebay.py`: `Api` responde por método y ruta, `paced(api, src)` envuelve en `PacedClient` con el `detect_block` de la fuente y `Creds` da `public_id("mercadolibre") == "ID-APP"` y `bearer("mercadolibre") == SecretStr("SECRETO")`. Las respuestas se arman con `_edit` sobre las fixtures.
  - `test_descriptor_follows_the_spec`: los valores del spec §1.1 e `isinstance(src, EanCatalogSource)`.
  - `test_token_is_fetched_once_and_sent_as_bearer`: dos lecturas hacen un solo `POST /oauth/token`, con `grant_type=client_credentials`, `client_id=ID-APP` y `client_secret=SECRETO`; cada GET lleva `Authorization: Bearer TKN-uno`, y `repr(src)` no contiene el token.
  - `test_token_renews_ten_minutes_before_expiry`: con `ctx.now` a 9 min del vencimiento, otro POST.
  - `test_401_renews_once_then_bad_key`: un 401 renueva el token y la lectura sigue; dos 401 seguidos ⇒ `CredentialUnavailable` con código `bad_key`.
  - `test_find_by_ean_retries_without_leading_zeros`: `0740617344806` vacío y `740617344806` con resultado ⇒ el id; `6921732899394` vacío ⇒ `None` con una sola búsqueda.
  - `test_find_by_ean_400_is_parse_error`: ⇒ `ParseError` con código `bad_request`.
  - `test_official_store_wins_without_user_lookup`: si el elegible más barato es tienda oficial, la observación lleva su precio, `FIRST_PARTY`, `category_path == ""` y `ean is None`, y no hay llamadas a `/users/`.
  - `test_green_leader_is_marketplace`: si el más barato no es oficial y su `/users` es platinum verde, sale su precio con `MARKETPLACE`.
  - `test_red_or_silver_seller_is_skipped`: si el más barato es rojo, gana el siguiente elegible.
  - `test_seller_lookups_stop_at_three_per_product`: con cuatro no oficiales sin reputación válida antes de una tienda oficial, hay 3 llamadas a `/users/` y gana la oficial.
  - `test_seller_reputation_is_cached`: el mismo vendedor en dos productos ⇒ una sola llamada a `/users/{id}`.
  - `test_user_403_or_404_skips_the_seller_without_blocking`: el vendedor no cuenta y `PacedClient.blocked is None`.
  - `test_items_404_gives_no_observation`: `items-none` ⇒ `[]`.
  - `test_policy_403_on_items_is_hard_block`: un 403 en `/products/{id}/items` ⇒ `HardBlock`.
  - `test_catalog_listings_found_gone_failed`: con un elegible, `ListingFound` con `title` igual al nombre, `url_path == "/p/MCO18924972"`, `list_price is None`, `in_stock` y `unit_key == "un|1"`; sin elegibles, `ListingGone`; con un cuerpo inválido, `ListingFailed` con `ParseError`.
  - `test_card_facts_reuse_the_confirmation`: tras `fetch_catalog_listings`, `fetch_card_facts` no pide nada y da el título, `https://www.mercadolibre.com.co/p/MCO18924972`, la foto del CDN y la nota `"Tienda oficial · Envío gratis"`; para un líder sin envío gratis, `"MercadoLíder Platinum"`.
  - `test_card_facts_without_confirmation_reads_again`: sin lectura previa, pide el producto y los vendedores.
  - `test_parse_url_reads_product_links`: `https://www.mercadolibre.com.co/taladro-dewalt/p/MCO18924972` y `…/p/MCO18924972?pdp_filters=x` ⇒ `ListingRef("mercadolibre", "MCO18924972")`; `https://www.mercadolibre.com.co/ofertas` ⇒ `None`.
  - En `test_registry.py`: `test_route_mercadolibre_product_url` con `route_url` y `load_all()`.
- [ ] **Step 2:** ver el fallo (`uv run pytest tests/sources -q`).
- [ ] **Step 3: implementar.**
  - Token y `_get` como en `ebay.py`, con POST de formulario; un 400 o 401 del token ⇒ `CredentialUnavailable("bad_key")`.
  - `_choose(ctx, product_id)` recorre `parse_sellers`: la tienda oficial se elige sin más; si no, `parse_leader` de `/users/{id}`, con caché `dict[int, str | None]` y tope de 3 consultas por producto. Un `NotFound` o un `Unavailable` con 403 ⇒ `None`.
  - `detect_block` devuelve `None` en las rutas `/users/…` y, en las demás, `detect_block_common`.
  - En `fetch_*listings`, `ParseError`, `NotFound` y `Unavailable` ⇒ `ListingFailed`; `HardBlock` y `CredentialUnavailable` suben.
  - Caché en memoria de la última elección y del producto por id, para la tarjeta.
  - Agregar `discountz.sources.mercadolibre` a `ADAPTER_MODULES` y `mercadolibre` a la matriz de `crawl.yml`.
- [ ] **Step 4:** en verde (`tests/sources` y `tests/arch/test_smoke.py`) y commit `feat(mercadolibre): adaptador con token, regla de vendedor y tarjeta`.

### Task 6: Credenciales, configuración y CLI

**Files:**
- Modify: `src/discountz/config.py`, `src/discountz/infra/credentials.py`, `config/discountz.toml`, `.github/workflows/crawl.yml`, `.github/workflows/cycle.yml`, `src/discountz/app/crawl.py` (`crawl_max_items`) y `src/discountz/__main__.py`
- Test: `tests/unit/test_credentials.py`, `tests/unit/test_config.py`, `tests/arch/test_smoke.py`

**Interfaces:**
- Produces:
  - `Settings.meli_client_id: str | None` y `Settings.meli_client_secret: SecretStr | None`. Vacío ⇒ `None`; uno sin el otro ⇒ error `meli_incomplete`.
  - `EnvCredentials.public_id("mercadolibre")` y `EnvCredentials.bearer("mercadolibre")`.
  - `MeliParams(max_items: int = 12_000)` en `AppParams.meli`, desde la sección `[mercadolibre]`.
  - `crawl_max_items("mercadolibre", params) == params.meli.max_items`.

- [ ] **Step 1: pruebas que fallan.**
  - En `test_credentials.py`: `test_meli_id_and_secret_from_env` y `test_meli_missing_raises_credential_unavailable`; `test_repr_hides_secrets` también con las variables de Mercado Libre.
  - En `test_config.py`:
    - `test_meli_credentials_both_or_none`.
    - `mercadolibre` está en `p.budgets` y en `c` (crawl) con `(0.5, 0.5, 2000, 20000, 7000)`.
    - `[sources.mercadolibre]` tiene `(1, 1, 10, 1000, 120 min, 1440 min, 1)`.
    - `load_params().meli.max_items == 12000`.
    - `mercadolibre` está en los eventos Hot Sale y Black Days (`test_co_events_include_the_new_vtex_stores`).
    - `crawl_max_items("mercadolibre", p) == 12000` y `crawl_max_items("exito", p) is None`.
  - En `test_smoke.py`: `crawl.yml` y `cycle.yml` tienen `DISCOUNTZ_MELI_CLIENT_ID: ${{ secrets.MELI_CLIENT_ID }}` y `DISCOUNTZ_MELI_CLIENT_SECRET: ${{ secrets.MELI_CLIENT_SECRET }}`; `crawl --source mercadolibre` sin esas variables ⇒ `EXIT_OK` sin abrir la BD.
- [ ] **Step 2:** ver el fallo (`uv run pytest tests/unit tests/arch -q`).
- [ ] **Step 3: implementar** como eBay en cada archivo. En `test_config.py`, la aserción de `hot_share_bp == 2500` deja fuera a `mercadolibre`.
- [ ] **Step 4:** en verde y commit `feat(mercadolibre): credenciales, presupuestos y eventos`.

### Task 7: Migración 0017 y consultas de semillas y relectura

**Files:**
- Create: `migrations/0017_mercadolibre.up.sql`, `migrations/0017_mercadolibre.down.sql`
- Modify: `src/discountz/infra/db.py`, `src/discountz/app/maintenance.py`
- Test: `tests/integration/test_migrations.py`, `tests/integration/test_channels_db.py` (si cuenta fuentes), `tests/integration/test_ean_db.py` (nuevo) y `tests/integration/test_maintenance.py`

**Interfaces:**
- Produces, en `infra/db.py`:
  - `SCHEMA_VERSION = 17`.
  - `EanSeed(ean: str, topic: str)` y `TrackedItem(external_id: str, ean: str | None, topic: str)`, congeladas y con `slots`.
  - `ean_seeds(conn, source: str, topics: Sequence[str], min_minor: int, limit: int) -> list[EanSeed]`.
  - `record_ean_lookups(conn, source: str, results: Sequence[tuple[str, str | None]], now: datetime) -> None`: upsert de `checked_at` y `external_id`.
  - `catalog_refresh_items(conn, source: str, before: datetime, limit: int, skip_topics: Sequence[str] = ()) -> list[TrackedItem]`.
  - `mark_catalog_checked(conn, source: str, external_ids: Sequence[str], now: datetime) -> None`.
  - `delete_old_ean_lookups(conn, before: datetime) -> int`, por lotes con `_delete_batched`.
- Y en `app/maintenance.py`: `EAN_LOOKUP_KEEP = timedelta(days=30)`, llamado en la retención.

- [ ] **Step 1: pruebas que fallan.**
  - `test_up_down_up`: las listas llegan a 17, `SCHEMA_VERSION == 17` y hay 25 fuentes. `test_seed_sources_and_gates` suma `"mercadolibre": True`.
  - `test_0017_up_down`: la fuente es `("store", "COP", True)` y existen `ean_lookup` y `catalog_item.checked_at`. Con una fila de catálogo y una de `ean_lookup` de Mercado Libre, bajar a 16 borra la fuente, la tabla, la columna y esas filas; subir otra vez vuelve a 17.
  - `test_ean_seeds_take_card_topics_cop_and_min_price`: Éxito `tecnologia` a 2.000.000 con el EAN A, Éxito `belleza` (off) con B, Alkosto `tecnologia` a 50.000 con C y eBay en USD con D ⇒ solo A.
  - `test_ean_seeds_skip_tracked_and_looked_up`: A en `catalog_item` de `mercadolibre` y B en `ean_lookup` ⇒ ninguno de los dos.
  - `test_ean_seeds_use_most_recent_row_and_price_desc`: E en Éxito (`hogar`, visto ayer, 300.000) y en Jumbo (`electro`, visto hoy, 400.000), y F en Alkosto (`tecnologia`, 900.000) ⇒ `[EanSeed(F, "tecnologia"), EanSeed(E, "electro")]`.
  - `test_record_ean_lookups_upserts`: anotar dos veces el mismo EAN cambia `checked_at` y `external_id`.
  - `test_catalog_refresh_items_oldest_check_first`: filas de `mercadolibre` con `checked_at` de hace 8 h, sin `checked_at` y vista hace 7 h, con `checked_at` de hace 2 h, y una de tema `belleza`. Con `before = now - 6 h` y `skip_topics=("belleza",)` salen la de 8 h y luego la de 7 h, con su EAN y su tema.
  - `test_mark_catalog_checked_keeps_last_seen_at`.
  - `test_maintenance_deletes_ean_lookups_after_30_days` (en `test_maintenance.py`).
- [ ] **Step 2:** ver el fallo con Postgres local.
- [ ] **Step 3: implementar** la migración (permisos como `0010`, bajada como `0016`, y `ALTER TABLE catalog_item ADD COLUMN checked_at timestamptz`) y las consultas. La de semillas:

```sql
SELECT ean, topic FROM (
  SELECT DISTINCT ON (c.ean) c.ean, c.topic, c.price_minor
    FROM discountz.catalog_item c
   WHERE c.source <> %(src)s AND c.currency = 'COP' AND c.ean IS NOT NULL
     AND c.topic = ANY(%(topics)s) AND c.price_minor >= %(min)s
     AND NOT EXISTS (SELECT 1 FROM discountz.catalog_item m
                      WHERE m.source = %(src)s AND m.ean = c.ean)
     AND NOT EXISTS (SELECT 1 FROM discountz.ean_lookup l
                      WHERE l.source = %(src)s AND l.ean = c.ean)
   ORDER BY c.ean, c.last_seen_at DESC) s
 ORDER BY price_minor DESC, ean
 LIMIT %(limit)s
```

  La de relectura ordena por `coalesce(checked_at, last_seen_at), external_id` y filtra `coalesce(checked_at, last_seen_at) < before` y `topic <> ALL(skip_topics)`.
- [ ] **Step 4:** en verde (unitarias e integración) y commit `feat(mercadolibre): migración 0017 con búsquedas por EAN y relectura`.

### Task 8: Rastreo sembrado por EAN

**Files:**
- Modify: `src/discountz/app/crawl.py` y `src/discountz/infra/log.py` (`"found"` en `ALLOWED_FIELDS`)
- Test: `tests/integration/test_crawl_seeds.py` (nuevo)

**Interfaces:**
- Consumes: `EanCatalogSource` (Task 5); `ean_seeds`, `record_ean_lookups`, `catalog_refresh_items` y `mark_catalog_checked` (Task 7), con `count_catalog_items` y `bot_state`; `gtin_ok` (Task 3); `crawl_max_items` (Task 6).
- Produces:
  - `_Run.process(items, topics: Mapping[str, str] | None = None)`.
  - Constantes `REFRESH_AFTER = timedelta(hours=6)`, `SEED_CHUNK = 20` y `SEED_QUERY = 200`.
  - Un log `crawl_seeds` con `source`, `count` (EAN buscados) y `found` (productos nuevos).

- [ ] **Step 1: pruebas que fallan.** Usan `FakeEanCatalog`, una subclase de `FakeCatalog` de `tests/integration/test_crawl.py` con `code="mercadolibre"`, `max_batch=1`, `by_ean: dict[str, str]` y un `find_by_ean` que hace una petición falsa a `https://fake/search/<ean>`. Las filas semilla de Éxito se guardan con `repo.save_catalog_page`.
  - `test_seeds_track_found_products_with_seed_ean_and_topic`: el EAN válido A (`tecnologia`, 2.000.000) lleva a `MCO1`, con `hot["MCO1"]` ⇒ la fila `(mercadolibre, MCO1)` queda con `ean = A` y `topic = "tecnologia"`, y en `ean_lookup` quedan `(A, "MCO1")` y `(B, NULL)` para un EAN válido sin producto.
  - `test_invalid_gtin_is_recorded_without_requests`: `6921732899395` queda anotado sin pedir `https://fake/search/…`.
  - `test_two_seeds_of_the_same_product_track_it_once`: A y A' llevan a `MCO1` ⇒ una fila y una sola lectura de `items`.
  - `test_found_product_without_sellers_is_not_searched_again`: `MCO2` sin observación no entra al catálogo, queda `(C, "MCO2")` en `ean_lookup`, y una segunda corrida no vuelve a buscar C.
  - `test_refresh_reads_tracked_items_and_marks_checked`: las filas de `mercadolibre` vistas hace 7 h se releen con el tema y el EAN de la fila, aunque la observación traiga `""` y `None`, y quedan con `checked_at = T`. Una sin observación queda con `checked_at = T` y el mismo `last_seen_at`.
  - `test_seeds_stop_at_max_items_and_when_discovery_paused`: con `max_items` igual a las filas de la fuente, o con `discovery_paused`, no se pide `https://fake/search/…`.
  - `test_run_cut_mid_chunk_keeps_what_was_done`: con un `max_per_run` que alcanza para 3 de 5 semillas, las 3 quedan en `ean_lookup` y su producto, en el catálogo.
  - `test_drop_after_warmup_alerts_with_card`: semilla en T, relecturas en T+1 d y T+2 d al mismo precio y una en T+4 d un 20 % más barata ⇒ alerta encolada con la tarjeta de `mercadolibre` en el tema de la semilla.
- [ ] **Step 2:** ver el fallo con Postgres local.
- [ ] **Step 3: implementar** en `_Run`:
  - `run()` delega en `seeded(src)` si `isinstance(src, EanCatalogSource)`. `seeded` hace `refresh(cap × hot_share_bp ÷ 10.000)`, luego `seed(src)` si `seeds_allowed()`, y al final `refresh(cap)` con lo que quede.
  - `refresh(limit)` pide una vez `catalog_refresh_items(…, before = now − REFRESH_AFTER, skip_topics = temas off)` y lee de a un producto por `guarded`. Cada observación lleva el EAN y el tema de la fila, y se procesa y marca por lotes de `SEED_CHUNK`; las lecturas con `ParseError` no se marcan.
  - `seed(src)` pide `ean_seeds(…, topics = temas card, min_minor = glob.min_price[COP], limit = SEED_QUERY)` hasta que se vacíe o se acabe el cap. Por cada lote de `SEED_CHUNK`: un EAN que no pasa `gtin_ok` se anota sin pedir; `find_by_ean` va por `guarded`, y `None` o `ParseError` lo anotan sin producto; solo se leen ids nuevos (ni en `load_catalog_states` ni ya vistos en el lote), con el EAN y el tema de la semilla.
  - Un `try/finally` anota y procesa lo hecho aunque la corrida pare.
  - `process` usa `topics[o.external_id]` cuando el mapa lo trae.
- [ ] **Step 4:** en verde, con `tests/integration/test_crawl.py` sin cambios, y commit `feat(mercadolibre): rastreo sembrado por EAN con relectura de los seguidos`.

### Task 9: Punta a punta, documentación y PR

**Files:**
- Create: `tests/integration/test_crawl_mercadolibre.py`
- Modify: `CLAUDE.md`, `README.md`, `docs/estado.md`

- [ ] **Step 1:** `test_mercadolibre_crawl_tracks_a_seeded_product`: `run_crawl` con `MercadoLibreSource()`, credenciales falsas y un transporte que responde un token sintético (`{"access_token": "TKN", "expires_in": 21600}`) y las fixtures `search-ean`, `items`, `user-leader` y `product`. Con una fila de Éxito cuyo EAN es el de `search-ean`, la corrida termina en `ok`, guarda el producto con ese EAN y deja su fila en `ean_lookup`. Es la prueba de integración del conjunto: debe pasar sin cambios de código; si falla, se depura con superpowers:systematic-debugging.
- [ ] **Step 2:** documentación:
  - Una línea de gotcha en `CLAUDE.md`.
  - En el README, Mercado Libre en la lista de tiendas y una sección «21. Mercado Libre» con los secretos `MELI_CLIENT_ID` y `MELI_CLIENT_SECRET` (ya subidos) y `/fuente off mercadolibre`.
  - `docs/estado.md` reescrito.
- [ ] **Step 3:** verificación completa más la integración con Postgres local; commit `docs(mercadolibre): estado, gotcha y README`.
- [ ] **Step 4:** `bash .claude/hooks/git-guard.sh check`, push y PR con `.github/pull_request_template.md`, en español y sin pie de IA; suscribirse al PR. El PR pide correr `migrate` (esquema 17) al fusionar, antes del primer rastreo de `mercadolibre`.
