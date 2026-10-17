# Canales de ofertas de Telegram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (Native, por defecto de David) to implement this plan task-by-task. Antes de empezar, invoca `desarrollo-senior`, `arquitectura-y-datos` y `calidad-y-seguridad`. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Leer DescuentosTech, Clubgratis y cajitatech desde `t.me/s/<canal>` y reenviar al grupo cada oferta nueva, sin duplicados, como tarjeta «sin verificar» en el tema de su categoría.

**Architecture:** Un adaptador `kind="channel"` parametrizado por canal (`sources/telegram_channels.py`) convierte el HTML en `ChannelPost`. Las funciones puras de `domain/community.py` deciden si un post es duplicado y cuál es su tema. El paso `app/channels.py` corre dentro del ciclo de 5 minutos con el mismo `SourceRun`/`_close_source` de las tiendas, guarda cursor y posts recientes (migración `0003_canales`) y encola la tarjeta `community` en `outbox`.

**Tech Stack:** Python 3.13, selectolax, psycopg 3, Pillow, pytest + hypothesis.

**Spec:** `docs/superpowers/specs/2026-10-17-canales-telegram-design.md`

## Global Constraints

- Códigos de fuente: `tg_descuentostech`, `tg_clubgratis`, `tg_cajitatech`; canal en la URL: `DescuentosTech`, `Clubgratis`, `cajitatech`.
- Lectura: `GET https://t.me/s/<canal>?after=<last_id>`; como máximo 3 páginas por canal y ciclo; cada canal como máximo cada 10 min (`[channels] interval_min = 10`).
- Primera lectura (sin cursor): se guarda el id más alto y no se envía nada. Posts de más de 6 h: leídos sin enviar.
- Duplicado (24 h, entre canales): mismo cupón sin distinguir mayúsculas, o misma moneda, precio a ±10 % y Jaccard de tokens de `normalize_title` (sin tokens de 1-2 letras) ≥ 0,5.
- Título máx. 200 caracteres en `ChannelPost`, 120 en el mensaje. Foto solo `https://cdn1..cdn5.telesco.pe`.
- Etiqueta: «📣 Visto en <canal> · sin verificar»; USD lleva «🇺🇸 Compra por casillero». Botones «Ver oferta» y «Ver en el canal».
- `outbox`: `purpose="alert"`, prioridad 9 (después de las alertas propias), `dedup_key = "canal:<code>:<post_id>"`, `ttl = ALERT_TTL`, `topic`, `card` con `kind = "community_deal"`.
- `channel_post` se borra a los 7 días. `SCHEMA_VERSION = 3`.
- Logs con lista blanca: nunca texto del post, cupones ni URLs con query. Nada de sortear bloqueos (E6).
- Enlace «Ver oferta»: `SafeUrl.parse` con `LINK_HOSTS` del adaptador (`facebook.com`, `www.facebook.com`, `m.facebook.com`, `pccajita.link`, `joylink.io`, `amzn.to`, `amazon.com`, `www.amazon.com` y los `hosts` de las fuentes registradas). Si el enlace no pasa (otro host, `http`, `tg:`, `javascript:`), se usa `https://t.me/<canal>/<id>`. El programa nunca descarga ese enlace (sin SSRF); solo la foto, con su propia lista blanca.
- La BD es la última defensa: FKs a `source(code) ON DELETE CASCADE`, `CHECK (price_minor > 0)` y `CHECK (currency IN ('COP','USD'))`.
- Revisión final (executing-plans): el prompt del revisor lleva «Responde en español. Revisa también autorización por objeto (IDOR), validación en los bordes, secretos y datos personales, N+1 e índices, y migraciones reversibles.»
- Autoría David, commits `tipo(alcance): …` en español, sin trailers; un commit por tarea, verificación completa en verde antes de cada commit.

## Review Focus

1. Post con precio en el texto pero que no es oferta (sorteos, «reporta llegada» con «179 dolaritos»): no debe salir. → prueba en Task 3.
2. Clubgratis con precio USD sin decimales de 4 cifras (`1299`) y COP con `$` y punto (`$19.783`): moneda correcta. → prueba en Task 3.
3. El mismo post leído dos veces (ciclo que muere tras encolar y antes de guardar cursor): una sola tarjeta (`dedup_key` + cursor en la misma transacción). → prueba en Task 5.
4. Canal que devuelve 200 con página sin posts (cambio de HTML): cuenta como `PARSE_ERROR`, no como «sin novedades» para siempre. → prueba en Task 3 y Task 5.
5. Título con HTML o `&` (`Levi's & Co`): escapado en el mensaje y sin romper el pie de foto. → prueba en Task 4.

---

### Task 1: ADR, migración `0003_canales` y acceso a datos

**Files:**
- Create: `docs/decisions/0005-canales-de-telegram-como-fuente.md`
- Modify: `docs/architecture.md` (fuente `channel`, módulos nuevos y el flujo de los canales)
- Create: `migrations/0003_canales.up.sql`, `migrations/0003_canales.down.sql`
- Modify: `src/discountz/infra/db.py` (`SCHEMA_VERSION = 3`; funciones nuevas al final)
- Test: `tests/integration/test_channels_db.py`

**Interfaces:**
- Produces (en `infra/db.py`):
  - `@dataclass(frozen=True, slots=True) class ChannelCursor: last_id: int; updated_at: datetime`
  - `get_channel_cursor(conn, code: str) -> ChannelCursor | None`
  - `save_channel_cursor(conn, code: str, last_id: int, now: datetime) -> None` (upsert; `updated_at = now` aunque `last_id` no cambie)
  - `@dataclass(frozen=True, slots=True) class RecentPost: key: str; title_norm: str; price_minor: int; currency: str; coupon: str | None` (`key = "<code>:<post_id>"`)
  - `recent_channel_posts(conn, since: datetime) -> list[RecentPost]` (solo los que no son duplicado)
  - `insert_channel_post(conn, code: str, post_id: int, published_at: datetime, title_norm: str, price_minor: int, currency: str, coupon: str | None, topic: str, duplicate_of: str | None, now: datetime) -> bool` (`ON CONFLICT DO NOTHING`; `False` si ya existía)
  - `delete_old_channel_posts(conn, before: datetime) -> int`

- [ ] **Step 1: Escribir el ADR 0005 y actualizar `docs/architecture.md`.** El ADR sigue el formato de `0004`: contexto (Amazon bloqueado, canales públicos); decisión (F1-F4 del spec, `t.me/s` sin cuenta); alternativas (leer con la cuenta de David vía cliente de Telegram: riesgo para su cuenta; Amazon Creators API: exige ventas; Keepa: ~€49/mes; resolver los acortadores: 403 desde la nube); consecuencias («sin verificar», riesgo de cambio de formato); cuándo revisar (hoy ~180 publicaciones/día y ~1.300 filas; revisar si un canal da `PARSE_ERROR` en más de 2 ciclos seguidos, si las tarjetas de canales pasan de 100/día durante una semana o si `t.me/s` empieza a pedir inicio de sesión).
- [ ] **Step 2: Escribir las pruebas que fallan** en `tests/integration/test_channels_db.py` (`@pytest.mark.integration`, fixture `db` de `tests/integration/conftest.py`):
  - `test_migration_registers_channel_sources`: `SELECT code, kind, enabled FROM discountz.source WHERE kind='channel' ORDER BY code` → `[("tg_cajitatech","channel",True), ("tg_clubgratis","channel",True), ("tg_descuentostech","channel",True)]`.
  - `test_channel_cursor_roundtrip`: `None` sin cursor; tras `save_channel_cursor(db,"tg_clubgratis",59494,T)` y otro save con `59500` en `T+1h` → `ChannelCursor(59500, T+1h)`.
  - `test_insert_channel_post_is_idempotent`: primer insert `True`, segundo igual `False`.
  - `test_recent_posts_skip_duplicates_and_old`: tres posts (uno con `seen_at` hace 25 h, uno con `duplicate_of`), `recent_channel_posts(db, T-24h)` devuelve solo el restante con `key == "tg_cajitatech:88168"`.
  - `test_delete_old_channel_posts`: borra los de antes de `T-7d` y devuelve 1.
  - `test_channel_post_rejects_bad_values` (parametrizada): `price_minor = 0` y `currency = 'EUR'` → `psycopg.errors.CheckViolation`.
  - `test_migration_0003_down_restores_schema_2`: aplica `down` y la versión vuelve a 2 (patrón de la prueba de `0002`).
- [ ] **Step 3: Correr y verlas fallar.** Run: `DISCOUNTZ_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest tests/integration/test_channels_db.py -q -m integration` Expected: FAIL (tabla/función inexistente).
- [ ] **Step 4: Escribir `0003_canales.up.sql`** con el SQL de §6 del spec, más: `ALTER TABLE source` cambia el CHECK de `kind` a `('store','feed','channel')`; `INSERT INTO source (code, kind, currency, enabled, ramp_until)` los tres canales con `'USD', true, NULL`; FKs y CHECKs de Global Constraints; trigger `set_updated_at` no aplica (el código fija `updated_at`); grants a `discountz_app` como en `0002`. El `down` borra las tablas, los tres `source` (y sus `source_hourly_stat` en cascada) y restaura el CHECK.
- [ ] **Step 5: Implementar las funciones de `db.py`** con las firmas de Interfaces y `SCHEMA_VERSION = 3`.
- [ ] **Step 6: Correr la prueba y la verificación completa.** Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest -q -m "integration or not integration"` Expected: todo verde (las pruebas que comparan `SCHEMA_VERSION` con la última migración ya pasan con 3).
- [ ] **Step 7: Commit** `feat(canales): migración 0003 y acceso a datos de los canales`.

### Task 2: Dominio puro: duplicados y tema

**Files:**
- Create: `src/discountz/domain/community.py`
- Modify: `src/discountz/config.py` (`_ChannelRule`, `Topics.channel_rules`), `config/topics.toml` (`[[channel_rules]]` de §4.2 del spec), `config/discountz.toml` (`[channels] interval_min = 10`), `src/discountz/config.py` (`AppParams.channel_interval: timedelta`)
- Test: `tests/unit/test_community.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces (en `domain/community.py`):
  - `@dataclass(frozen=True, slots=True) class ChannelPost: channel: str; post_id: int; published_at: datetime; title: str; price: Money; coupon: str | None; store: str | None; hashtags: tuple[str, ...]; image_url: SafeUrl | None; link: SafeUrl` (`channel` es el código `tg_…`)
  - `@dataclass(frozen=True, slots=True) class ChannelRule: topic: str; words: frozenset[str]`
  - `title_tokens(title: str) -> frozenset[str]` (tokens de `normalize_title` con 3+ caracteres)
  - `find_duplicate(post: ChannelPost, recent: Sequence[RecentLike]) -> str | None` donde `RecentLike` es un `Protocol` con `key`, `title_norm`, `price_minor`, `currency`, `coupon` (lo cumple `db.RecentPost`); devuelve el `key` del primero que coincide.
  - `assign_channel_topic(post: ChannelPost, rules: Sequence[ChannelRule]) -> str` (`"otros"` sin coincidencias)
- Produces (en `config.py`): `Topics.channel_rules: tuple[ChannelRule, ...]`; `AppParams.channel_interval: timedelta` (por defecto 10 min).

- [ ] **Step 1: Escribir las pruebas que fallan** en `tests/unit/test_community.py`:
  - `test_same_coupon_is_duplicate_ignoring_case`: cupón `TVZE10` vs `tvze10`, títulos distintos → devuelve la clave del reciente.
  - `test_similar_title_and_price_is_duplicate`: «Armatus para PlayStation 5 juego de acción roguelike» USD 30,00 vs «Armatus - PlayStation 5» USD 31,50 → duplicado; con USD 40,81 (> 10 %) → `None`.
  - `test_other_currency_is_not_duplicate`: mismo título, COP vs USD → `None`.
  - `test_topic_by_hashtag_and_title`: hashtag `cuidadofacial` → `belleza`; título «Teclado mecánico retro» sin hashtags → `tecnologia`; «Set de 7 cuchillos de cocina» → `hogar`.
  - `test_topic_more_matching_words_wins`: «Reloj inteligente» → `tecnologia` (2 palabras) sobre `moda` (`reloj`, 1).
  - `test_topic_defaults_to_otros`: «Producto genérico xyz», sin hashtags y sin palabras de ninguna regla → `"otros"`.
  - En `tests/unit/test_config.py`: `test_channel_rules_point_to_existing_topics` (cada `rule.topic in topics.params`) y `test_channel_interval_is_ten_minutes`.
- [ ] **Step 2: Correr y verlas fallar.** Run: `uv run pytest tests/unit/test_community.py tests/unit/test_config.py -q` Expected: FAIL (`ImportError`).
- [ ] **Step 3: Implementar** `domain/community.py` (puro: solo `domain.*`), el modelo de config y las reglas en `topics.toml` con las palabras de §4.2 del spec (normalizadas sin tildes; `bañador` → `banador`). Puntuación del tema: número de palabras de la regla presentes en `hashtags ∪ title_tokens`; empate → la regla anterior en el archivo; una regla de varias palabras («reloj inteligente») se guarda como palabras sueltas y cuenta cada una.
- [ ] **Step 4: Correr y verlas pasar**, luego la verificación completa (comando de Task 1, Step 6). Expected: verde.
- [ ] **Step 5: Commit** `feat(canales): duplicados y tema de las ofertas de canales`.

### Task 3: Adaptador `telegram_channels` con fixtures reales

**Files:**
- Create: `src/discountz/sources/telegram_channels.py`
- Modify: `src/discountz/sources/base.py` (`kind: Literal["store", "feed", "channel"]`; `ChannelPage`; `ChannelSource` Protocol), `src/discountz/sources/registry.py` (`ADAPTER_MODULES` + `"discountz.sources.telegram_channels"`), `config/discountz.toml` (`[sources.tg_descuentostech]`, `[sources.tg_clubgratis]`, `[sources.tg_cajitatech]`: `group` = el código, `min_pause_s = 1`, `jitter_s = 1`, `max_per_run = 3`, `max_per_24h = 600`, `watch_interval_min = 10`, `search_interval_min = 10`, `max_batch = 1`), `src/discountz/app/render.py` (`SOURCE_NAMES`: «DescuentosTech», «Clubgratis», «cajitatech»)
- Create: `tests/fixtures/telegram/{descuentostech,clubgratis,cajitatech,cajitatech-empty}.{html.gz,meta.json}` (grabadas con `record-fixture`), `tests/fixtures/telegram/*.expected.json`
- Test: `tests/sources/test_telegram_channels.py`

**Interfaces:**
- Consumes: `ChannelPost` (Task 2).
- Produces (en `sources/base.py`):
  - `@dataclass(frozen=True, slots=True) class ChannelPage: posts: Sequence[ChannelPost]; max_id: int | None; has_more: bool; skipped: int` (`max_id`: id más alto de la página, sea oferta o no; `has_more`: la página trajo 20 publicaciones; `skipped`: publicaciones que no son oferta)
  - `class ChannelSource(Protocol): descriptor: SourceDescriptor; channel: str; def fetch_posts(self, ctx: SourceContext, after_id: int | None) -> ChannelPage: ...; def detect_block(self, resp: HttpResponse) -> BlockKind | None: ...`
- Produces (en `sources/telegram_channels.py`): `CHANNELS: Mapping[str, str]` (código → nombre del canal); `TelegramChannelSource(code: str)`; `parse_channel_page(code: str, html: bytes, now: datetime) -> ChannelPage`. Descriptor: `kind="channel"`, `currency=USD`, `hosts={"t.me"}`, `client="httpx"`, `capabilities=frozenset()`, `max_batch=1`, `external_id_re=^\d{1,12}$`, `image_hosts={"cdn1.telesco.pe",…,"cdn5.telesco.pe"}`. Los métodos de `Source` que no aplican devuelven `None`/`[]` o lanzan `NotImplementedError` como en `slickdeals.py`.

- [ ] **Step 1: Grabar las fixtures** desde la sesión: `uv run python -m discountz record-fixture --source tg_cajitatech --folder telegram --name cajitatech --url https://t.me/s/cajitatech` (igual para los otros dos, y `cajitatech-empty` con `?after=` igual al id más alto). Esto exige registrar el adaptador mínimo (descriptor) antes; si `record-fixture` aún no lo encuentra, grabar con el descriptor y dejar los parsers para el Step 3. Escribir a mano el `.expected.json` de cada canal a partir del HTML: lista de `{post_id, title[:40], price_minor, currency, coupon, store}` de las ofertas.
- [ ] **Step 2: Escribir las pruebas que fallan** en `tests/sources/test_telegram_channels.py`:
  - `test_parse_matches_expected[canal]` (parametrizada): `parse_channel_page` sobre la fixture produce exactamente el `.expected.json`.
  - `test_descuentostech_thousands_dot`: texto sintético `USD$1.999` → `Money(199_900, USD)`.
  - `test_clubgratis_currency_inference` (parametrizada): `💰 4.499.910` → COP 4499910; `💰 $19.783` → COP 19783; `💰 275.000 aplicar cupón PREHOT` → COP 275000 y cupón `PREHOT`; `💰 12.74` → USD 1274; `➡️ 6.5` → USD 650; `💰 130 precio incluye envio` → USD 13000; `$1,999.00` → USD 199900; `💰 1299` → USD 129900.
  - `test_non_deals_are_skipped`: «Johan reporta llegada: … 179 dolaritos» y «📦 El Madrugón: hasta 40% de descuento» → no salen; `skipped` los cuenta.
  - `test_image_only_from_telesco`: una foto con `//telegram.org/img/emoji/…` → `image_url is None`.
  - `test_link_outside_allowlist_falls_back_to_post` (parametrizada): `http://amzn.to/x`, `tg://resolve?domain=x`, `javascript:alert(1)` y `https://evil.example/x` → `link == https://t.me/cajitatech/<id>`; `https://pccajita.link/abc` se conserva.
  - `test_post_without_time_or_price_is_skipped`: sin `<time datetime>` o con precio 0 → no sale y cuenta en `skipped`.
  - `test_parser_never_crashes_on_arbitrary_text` (hypothesis, texto unicode arbitrario dentro de una publicación válida): `parse_channel_page` devuelve una `ChannelPage` o lanza `ParseError`, nunca otra excepción.
  - `test_empty_page_with_posts_markup_missing_is_parse_error`: HTML 200 sin ningún `tgme_widget_message` y sin el marcador de canal vacío → `ParseError`; la fixture `cajitatech-empty` (canal sin posts nuevos) → `ChannelPage((), None, False, 0)`.
  - `test_page_with_prices_but_zero_deals_is_parse_error`: 20 posts con `$` en el texto y 0 ofertas → `ParseError("format")`.
  - `test_fetch_posts_url`: con `_Router`/`FakeHttp`, `fetch_posts(ctx, 88160)` pide `https://t.me/s/cajitatech?after=88160`; con `None`, sin query.
  - `test_registry_has_three_channels`: `{c for c,s in load_all().items() if s.descriptor.kind=="channel"} == {"tg_descuentostech","tg_clubgratis","tg_cajitatech"}`.
- [ ] **Step 3: Correr y verlas fallar.** Run: `uv run pytest tests/sources/test_telegram_channels.py -q` Expected: FAIL.
- [ ] **Step 4: Implementar** el adaptador con selectolax: cada `div.tgme_widget_message[data-post]` da `post_id`, `<time datetime>`, la foto (`a.tgme_widget_message_photo_wrap` → `background-image:url('…')`) y el texto (`<br>` → salto de línea). Un parser por canal con las reglas de §3 del spec; los hashtags salen de `#\w+` normalizados. Regla de moneda de Clubgratis, en este orden: `,` de miles o `.` con 1-2 decimales → USD; grupos `\d{1,3}(\.\d{3})+` → COP; entero sin separador → USD. Precio a `Money` con `parse_price` del dominio cuando aplique y `Decimal` (nunca `float`).
- [ ] **Step 5: Correr y verlas pasar**, luego la verificación completa. Expected: verde (las pruebas de arquitectura de «Adaptadores independientes» incluyen el módulo nuevo).
- [ ] **Step 6: Commit** `feat(canales): adaptador de los canales de Telegram con fixtures reales`.

### Task 4: Tarjeta `community` y mensaje

**Files:**
- Modify: `src/discountz/infra/card.py` (`CardData.usual: Money | None`, `drop_bp: int | None`, `min_days: int | None`, `badge: str | None`, `coupon: str | None`), `src/discountz/app/deliver.py` (`card_png` lee los campos opcionales), `src/discountz/app/render.py` (`render_channel_deal`)
- Create: `tests/golden/channel_deal_usd.html`, `tests/golden/channel_deal_cop.html`
- Test: `tests/unit/test_card.py`, `tests/unit/test_render.py`

**Interfaces:**
- Consumes: `ChannelPost` (Task 2), `source_name` con los nombres de Task 3.
- Produces:
  - `channel_card(post: ChannelPost, topic: str) -> dict[str, Any]` en `app/render.py` con claves `store` (código `tg_…`), `topic`, `title`, `url` (`post.link`), `image_url`, `price_minor`, `currency`, `kind="community_deal"`, `coupon`, `badge` (`"<Canal> · sin verificar"`).
  - `render_channel_deal(post: ChannelPost) -> RenderedMessage` (pie de foto ≤ 1.024 caracteres; botones `Ver oferta` → `post.link`, `Ver en el canal` → `https://t.me/<canal>/<post_id>`).

- [ ] **Step 1: Escribir las pruebas que fallan:**
  - `test_render_channel_deal_matches_golden[usd|cop]`: posts fijos (USD con cupón y tienda «Amazon»; COP sin cupón) → HTML igual a `tests/golden/channel_deal_*.html`. El golden USD contiene «📣 Visto en Clubgratis · sin verificar», «🇺🇸 Compra por casillero» y `<code>T6YLVQZS</code>`.
  - `test_render_channel_deal_escapes_title`: título `Levi's <b>501</b> & Co` → aparece `Levi&#x27;s &lt;b&gt;501&lt;/b&gt; &amp; Co` y el HTML pasa el validador de etiquetas de Telegram que ya usan las otras pruebas de render.
  - `test_community_card_renders_without_usual`: `card_png(channel_card(post, "tecnologia"), photo=None)` devuelve un PNG de 1080×1080 (mismo `SIZE`) sin lanzar; con foto válida también.
- [ ] **Step 2: Correr y verlas fallar.** Run: `uv run pytest tests/unit/test_card.py tests/unit/test_render.py -q` Expected: FAIL.
- [ ] **Step 3: Implementar:** en la tarjeta, sin `usual` no se dibuja «antes» ni %; en su lugar la franja `badge` en el color de la tienda y, si hay `coupon`, la línea «Cupón <X>». El color de los canales: `STORE_COLORS` con un color por código `tg_…` (elegir tres de la paleta existente). Escribir los golden a partir de la salida revisada a mano.
- [ ] **Step 4: Correr y verlas pasar**, luego la verificación completa. Expected: verde; los golden y las tarjetas existentes no cambian.
- [ ] **Step 5: Commit** `feat(canales): tarjeta y mensaje de las ofertas de canales`.

### Task 5: Paso del ciclo `app/channels.py`

**Files:**
- Create: `src/discountz/app/channels.py`
- Modify: `src/discountz/app/cycle.py` (llamar a `run_channels` después de `compose_alerts` y antes de `_daily`; contar sus peticiones en `counters.requests`)
- Test: `tests/integration/test_channels.py`, `tests/e2e/test_cycle.py` (una prueba nueva)

**Interfaces:**
- Consumes: Task 1 (db), Task 2 (`find_duplicate`, `assign_channel_topic`, `title_tokens`), Task 3 (`ChannelSource`, `ChannelPage`), Task 4 (`channel_card`, `render_channel_deal`); `SourceRun`, `_close_source` y `PacedClient` como los usa `cycle._make_run`.
- Produces: `@dataclass(frozen=True, slots=True) class ChannelsResult: requests: int; sent: int; duplicates: int; skipped: int` y `run_channels(conn, sources: Mapping[str, Source], runs: Mapping[str, SourceRun], now: datetime, params: AppParams, topics: Topics) -> ChannelsResult` en `app/channels.py`. `cycle.py` arma los `SourceRun` de los canales con `_make_run(deps, code, (), inp.breakers, deadline)` solo para los canales habilitados, con breaker no abierto y cuyo cursor tiene `updated_at + channel_interval <= now` (o no existe), y llama a `_close_source` por cada uno al final.

Algoritmo por canal (en este orden):
1. Primero las peticiones, fuera de toda transacción: `page = run.source.fetch_posts(ctx, cursor.last_id if cursor else None)` hasta 3 páginas mientras `page.has_more` (la siguiente con `after = page.max_id`); `HardBlock`/`SourceError`/`ParseError` aplican `breaker_transition` con `HARD_BLOCK`/`UNAVAILABLE`/`PARSE_ERROR`, suben el contador y cortan ese canal; una página leída aplica `OK`.
2. Sin cursor: `save_channel_cursor(max_id)` y nada más.
3. Después, sin HTTP adentro, por cada post en orden de `post_id`, en **una transacción por página**: si `published_at < now - 6h` → solo cuenta como leído; si no, `dup = find_duplicate(post, recent)`; `insert_channel_post(..., duplicate_of=dup)`; si no es duplicado y el insert devolvió `True`, `enqueue` con los valores de Global Constraints y se agrega a `recent`. Al final de la transacción, `save_channel_cursor(page.max_id)`.
4. `event(log, "channel_done", source=…, requests=…, posts=…, sent=…, duplicates=…, skipped=…)`.

- [ ] **Step 1: Escribir las pruebas que fallan** en `tests/integration/test_channels.py` con un `FakeChannel` (implementa `ChannelSource` con páginas programadas, una petición por `fetch_posts`, igual que `FakeCatalog` en `test_crawl.py`):
  - `test_first_read_saves_cursor_without_sending`: 5 posts, sin cursor → `outbox` vacío, cursor = id máx.
  - `test_new_posts_are_queued_in_their_topic`: cursor 100, posts 101-102 → 2 filas en `outbox` con `topic` asignado, `card->>'kind' = 'community_deal'`, `dedup_key = 'canal:tg_cajitatech:101'`; cursor 102.
  - `test_pages_follow_after_until_three`: `has_more` siempre → 3 llamadas con `after` creciente y el cursor queda en el máx. de la tercera.
  - `test_old_posts_are_marked_read_not_sent`: post de hace 7 h → sin `outbox`, cursor avanza.
  - `test_cross_channel_duplicate_sent_once`: mismo cupón en `tg_clubgratis` y `tg_cajitatech` → 1 fila en `outbox`, el segundo `channel_post.duplicate_of = 'tg_clubgratis:59510'`.
  - `test_replayed_page_does_not_resend`: correr dos veces con el cursor reseteado a mano → sigue habiendo 1 fila por post (dedup_key + insert idempotente).
  - `test_failure_mid_page_keeps_cursor`: `enqueue` falla en el 2.º post (monkeypatch) → la transacción se revierte, cursor sin cambiar, 0 filas.
  - `test_parse_error_opens_breaker_after_threshold`: el canal lanza `ParseError` en cada ciclo → tras el umbral de `breaker_transition` el breaker queda `open` y el canal no se lee en el ciclo siguiente.
  - `test_channel_due_every_ten_minutes`: leído en `T` → no se lee en `T+5min`, sí en `T+10min`.
  - En `tests/e2e/test_cycle.py`: `test_cycle_delivers_channel_deal_card`: ciclo completo con un `FakeHttp` que sirve la fixture de cajitatech (con cursor previo un id menor que el máx.) → `deliver` manda una foto al hilo del tema con los dos botones.
- [ ] **Step 2: Correr y verlas fallar.** Run: `DISCOUNTZ_TEST_DATABASE_URL=… uv run pytest tests/integration/test_channels.py tests/e2e/test_cycle.py -q -m "integration or not integration"` Expected: FAIL.
- [ ] **Step 3: Implementar** `app/channels.py` y la llamada en `cycle.py`.
- [ ] **Step 4: Correr y verlas pasar**, luego la verificación completa. Expected: verde.
- [ ] **Step 5: Commit** `feat(canales): leer los canales en el ciclo y encolar sus ofertas`.

### Task 6: Mantenimiento, resumen diario y documentación

**Files:**
- Modify: `src/discountz/app/maintenance.py` (borra `channel_post` de más de 7 días en el paso de retención), `src/discountz/infra/db.py` (`CatalogDaily.channel_posts: int`, `channel_duplicates: int` de las últimas 24 h), `src/discountz/app/render.py` (`_catalog_line` agrega «Canales: N ofertas (M repetidas)» cuando N > 0)
- Modify: `README.md` (sección de canales: qué llega, `/fuente off tg_…`), `docs/estado.md`, `CLAUDE.md` (1-2 líneas: módulo nuevo y gotcha de `t.me/s`)
- Test: `tests/integration/test_maintenance.py`, `tests/unit/test_render.py`

- [ ] **Step 1: Escribir las pruebas que fallan:** `test_maintenance_deletes_channel_posts_after_7_days` (uno de 8 días se borra, uno de 6 queda); `test_daily_summary_counts_channel_posts` (3 enviados y 1 duplicado en 24 h → la línea dice «Canales: 3 ofertas (1 repetidas)»); sin posts, la línea no aparece.
- [ ] **Step 2: Correr y verlas fallar.** Expected: FAIL.
- [ ] **Step 3: Implementar** y actualizar la documentación (README en español de Colombia; `estado.md` reescrito con el estado del SP-6, la suite y el siguiente paso: correr *migrate* `to = head` al fusionar).
- [ ] **Step 4: Verificación completa.** Expected: verde.
- [ ] **Step 5: Commit** `feat(canales): retención, resumen diario y documentación`.
