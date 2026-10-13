# discountZ v1: spec de diseño

- Fecha: 2026-10-13
- Estado: aprobado por secciones; pendiente de revisión del documento completo
- Autor: David Santiago Rincón Bautista

## 0. Contexto, requisitos y decisiones previas

### 0.1 Qué es

Monitor **personal** de ofertas para David (Colombia). Rastrea tiendas colombianas (COP) y de EE. UU. que envían a casillero (USD), filtra descuentos inflados con un histórico propio y avisa por Telegram con botones a la tienda. Escala: decenas a pocos cientos de productos. Costo operativo: $0.

### 0.2 Decisiones de David (prevalecen sobre cualquier texto posterior)

| # | Decisión |
|---|---|
| D1 | Uso personal: alertas solo para David. |
| D2 | Solo nube (sin máquina en casa). |
| D3 | Repo **público** en su cuenta; minutos de Actions ilimitados. |
| D4 | Monitorear **seguimientos** (enlace + objetivo opcional) y **búsquedas** de descubrimiento. |
| D5 | Incluir todas las tiendas viables y más; las no viables entran como sub-proyectos. |
| D6 | Enfoque A: monolito modular Python síncrono, GitHub Actions, Supabase Postgres, comandos de Telegram en cada ciclo. |
| D7 | Recortes YAGNI: lease con TTL sin fencing, sin respaldo en v1, 2 roles de BD, healthchecks opcional. |
| D8 | Contra la pérdida de ofertas relámpago: **disparo externo cada 5 min** y **prioridad + modo caliente** (§6). No se incluyen páginas de ofertas por tienda ni alertas «sin confirmar». |
| D9 | Canales comunitarios: `t.me/DescuentosTech`, `t.me/Clubgratis`, `t.me/cajitatech` (SP-6). |
| D10 | **Sin costo puesto en Colombia**: las alertas de EE. UU. solo dicen «🇺🇸 Compra por casillero». |

### 0.3 Matriz de infraestructura a $0 (evaluada)

| Opción | Latencia real | Riesgo de ban | Persistencia | Límites | Veredicto |
|---|---|---|---|---|---|
| GitHub Actions (+ disparo externo) | ~5-8 min | Alto en HTML protegido, bajo en APIs (medido: ver 0.4) | Necesita BD externa | Público: ilimitado; se desactiva tras 60 d sin actividad | **Elegida** |
| Cloudflare Workers + D1 | 1 min | Medio | D1 500 MB | 10 ms CPU, 50 subpeticiones | Descartada: no alcanza para parsear HTML |
| Oracle Always Free | 1 min | Alto | Disco | Tarjeta, falta de capacidad, recuperación de VMs ociosas | Frágil |
| Render / Koyeb | — | Alto | Efímera | Se duermen; cron de pago | Descartada |
| Máquina en casa + Docker | 1 min | Bajo (IP residencial) | SQLite local | Luz e internet | Plan B (D2 la excluye hoy) |

Base de datos: **Supabase Postgres free** (500 MB) frente a SQLite en el repo (público: expondría la lista y los tokens), D1 o Turso.

### 0.4 Resultados del spike de fuentes (2026-10-13)

3 runners de Actions con IPs Azure distintas × 4 peticiones por fuente y cliente (curl_cffi suplantando Chrome / httpx plano). Precio legible sobre 12:

| Fuente | Resultado | Veredicto |
|---|---|---|
| VTEX legacy (Éxito, Carulla, Olímpica, Panamericana) | 12/12 ambos | v1 |
| Amazon `/s` y `/dp` | 12/12 ambos (volumen bajo) | v1 con rampa |
| Newegg | 12/12 | v1 con rampa |
| Walmart | curl_cffi 12/12, plano 0/12 (PerimeterX); extracción dudosa | v1 tras puerta de activación |
| Best Buy HTML | curl_cffi 12/12, plano 0/12; extracción dudosa | v1 tras puerta de activación |
| Slickdeals RSS | 12/12 | v1 (feed) |
| Alkosto, Ktronix, Falabella, Homecenter | 200 sin bloqueo, pero el «precio» era un texto fijo | SP-2, SP-3 |
| Mercado Libre | API 403 sin token; HTML → `account-verification` | SP-1 (OAuth) |
| eBay HTML 4-6/12; B&H 4/12; Micro Center 0/12; Reddit anónimo 0-2/12 | — | SP-4, SP-5 o fuera |

Lección: la suplantación de huella TLS decide en Walmart y Best Buy, y empeora Falabella → el cliente se elige por adaptador.

---

## Diseño detallado

Este documento une las propuestas «simplicidad», «anti-engaño» y «operación» con la crítica adversarial del algoritmo y la crítica de operación y seguridad. La base es la propuesta de simplicidad: monolito síncrono, SQL plano, un solo adaptador VTEX y comandos puros. Solo se le agrega lo que resuelve un problema que las críticas señalaron:

- segmentos de precio, porque `price_daily` no soporta el algoritmo;
- un lease con TTL, porque el candado consultivo puede quedar huérfano;
- confirmación independiente, porque la relectura con el mismo parser confirma su propio error;
- roles separados, migración aprobada y endurecimiento de logs, Telegram y HTTP.

Todas las cifras son valores por defecto que se configuran en `config/discountz.toml` y se validan con pydantic al arrancar.

---

## 1. Arquitectura, módulos y flujo del ciclo

### 1.1 Estructura de paquetes

```
discountz/
├── pyproject.toml              # dependencias ==, [tool.uv] required-version, ruff, mypy strict, import-linter
├── uv.lock
├── config/
│   ├── discountz.toml          # categorías, fuentes (presupuestos), umbrales, calendario de eventos. Público, sin datos de David
│   ├── canaries.toml           # 1-2 productos GENÉRICOS por tienda + rango de precio esperado
│   ├── supabase-ca.crt         # CA pública para sslmode=verify-full
├── migrations/
│   ├── 0001_init.up.sql
│   └── 0001_init.down.sql
├── src/discountz/
│   ├── __main__.py             # CLI: run | migrate [--to N] [--wait-lease 20m] | setup-bot | record-fixture | check-config
│   ├── config.py               # pydantic-settings (SecretStr) + TOML → dataclasses de dominio
│   ├── domain/                 # PURO: sin I/O, sin reloj, sin azar del sistema, sin pydantic
│   │   ├── money.py            # Currency, Money (Value Object), to_minor()
│   │   ├── model.py            # ListingRef, Offer, Lead, SearchSpec, SellerClass, AlertKind, Flag, Maturity
│   │   ├── urls.py             # SafeUrl: normalización, lista blanca de hosts, regex por fuente
│   │   ├── segments.py         # Segment, apply_sample()
│   │   ├── stats.py            # wq(), rarity_bp(), segment_weight_s(), compute_stats()
│   │   ├── guards.py           # G1–G14 (§3.4)
│   │   ├── evaluate.py         # evaluate() -> Evaluation
│   │   ├── decide.py           # decide() -> Send | Skip
│   │   ├── commands.py         # parse_command(), parse_price()
│   │   ├── scheduling.py       # plan_run(), next_check_at(), RoundRobin
│   │   ├── health.py           # breaker_transition(), classify_health()
│   │   └── errors.py
│   ├── app/                    # casos de uso; único lugar que combina BD + red + Telegram
│   │   ├── cycle.py            # run_cycle()
│   │   ├── inbox.py            # getUpdates idempotente + comandos
│   │   ├── collect.py          # tareas F→R→E→W
│   │   ├── confirm.py          # fase C: tormenta + confirmación
│   │   ├── compose.py          # alertas → outbox (individual o resumen)
│   │   ├── deliver.py          # drenado del outbox
│   │   ├── maintenance.py      # retención, alltime_min, tamaño de BD, canarios, resumen diario
│   │   └── render.py           # HTML de Telegram (puro, pruebas golden)
│   ├── sources/
│   │   ├── base.py             # Protocol Source, SourceDescriptor, SourceContext, CredentialStore
│   │   ├── registry.py         # SOURCES: Mapping[str, Source]; route_url()
│   │   ├── blocks.py           # detect_block() común (marcadores)
│   │   ├── vtex.py             # VtexSource(code, host, api_base) × 4
│   │   ├── amazon.py · walmart.py · newegg.py · bestbuy.py
│   │   └── slickdeals.py       # kind='feed' → Lead
│   └── infra/
│       ├── http.py             # HttpClient Protocol, HttpxClient, CurlClient, PacedClient (Decorator)
│       ├── db.py               # conexión, lease con TTL, funciones SQL (Repository)
│       ├── migrate.py          # ejecutor de migraciones (~80 líneas)
│       ├── telegram.py         # getUpdates, sendMessage, answerCallbackQuery
│       ├── healthchecks.py     # opcional: ping /start, ok, /fail si existe DISCOUNTZ_HEALTHCHECKS_URL
│       └── log.py              # key=value + redacción
└── tests/{unit,sources,integration,e2e,arch}/ + tests/fixtures/<source>/*.{json,html,xml}.gz + .meta.json + .expected.json
```

### 1.2 Regla de dependencias (verificada en CI)

Las capas son `app → sources → infra → domain`. Ninguna importa una capa superior.

```toml
[tool.importlinter]
root_package = "discountz"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "Capas"
type = "layers"
layers = ["discountz.app", "discountz.sources", "discountz.infra", "discountz.domain"]

[[tool.importlinter.contracts]]
name = "Dominio puro"
type = "forbidden"
source_modules = ["discountz.domain"]
forbidden_modules = ["httpx", "curl_cffi", "psycopg", "selectolax", "pydantic", "pydantic_settings", "defusedxml"]

[[tool.importlinter.contracts]]
name = "Adaptadores independientes"
type = "independence"
modules = ["discountz.sources.vtex", "discountz.sources.amazon", "discountz.sources.walmart",
           "discountz.sources.newegg", "discountz.sources.bestbuy", "discountz.sources.slickdeals"]
```

- **Ruff `TID251` (banned-api):** prohíbe `datetime.datetime.now`, `datetime.datetime.utcnow`, `datetime.date.today`, `time.time` y `random.random` fuera de `__main__.py` y de `infra/http.py`. Este último usa `time.monotonic` y `time.sleep`, inyectables.
- **Ruff `DTZ`:** prohíbe los datetime naive.
- **Reloj y azar:** `now` se calcula una sola vez en `__main__` y viaja como parámetro. El azar entra como `random.Random(seed)`.

### 1.3 Flujo exacto de un ciclo (`python -m discountz run`)

- `T0` es el arranque del proceso.
- Plazo blando: `T0 + 480 s`. Desde ahí no se toma trabajo nuevo.
- Plazo duro interno: `T0 + 12 min`.
- El job se corta a los 15 min y el lease vence a los 16.

| # | Paso | Detalle | Si falla |
|---|---|---|---|
| 0 | Arranque | `now = datetime.now(UTC)`. `Settings` (pydantic-settings, `SecretStr`, `hide_input_in_errors=True`) y `Params` (TOML). Logging con redacción. Ping `/start` a healthchecks si está configurado. | `ConfigError` ⇒ exit 2 |
| 1 | Conexión | psycopg 3 síncrono, Supavisor en **modo sesión** (`:5432`, IPv4), `sslmode=verify-full`, `connect_timeout=10`, `application_name=discountz-<run_id>`. 3 intentos con espera `U(0, 2·2^n)` s. | Ping `/fail` ⇒ exit 3 |
| 2 | Esquema | `max(schema_migrations.version) == SCHEMA_VERSION` | `run.status='skipped_schema'`, aviso de sistema (1 al día) ⇒ exit 0 |
| 3 | Lease | `acquire_lease(holder, ttl=16 min)` (§2.2). El plazo duro interno (12 min) es menor que el TTL, así que ningún proceso escribe con el lease vencido. | `skipped_locked` ⇒ exit 0. Con ≥ 3 `skipped_locked` seguidos: aviso + exit 1 |
| 4 | Registro | `INSERT run`. Los `run` en `running` de más de 16 min pasan a `failed`. Se calcula `gap_seconds` frente al run anterior. | — |
| 5 | Recuperar outbox | Lo que lleva más de 10 min en `sending` vuelve a `pending` con `attempts+1` | — |
| 6 | Bandeja de entrada, pasada 1 | §5.4. Tope de 20 s. Los comandos solo escriben la BD y encolan respuestas; **nunca** hacen HTTP a tiendas. | Telegram caído ⇒ sigue sin comandos |
| 8 | Plan (puro) | `plan_run(...)` aplica: puerta del breaker, presupuesto por ejecución y por 24 h por grupo, reserva de confirmación y prioridades (§4.5). Produce un `RoundRobin` por fuente. | — |
| 9 | Recolección | Turnos rotativos: siempre se atiende la fuente cuyo próximo turno permitido llega antes. Por tarea: **F** fetch → **R** lee segmento abierto e histórico (`= ANY`) → **E** `evaluate()` puro → **W** transacción corta (`listing`, `price_segment`, alertas no candidatas, agenda, contadores). Nunca hay HTTP dentro de una transacción. | Cada tarea está aislada: una excepción marca la tarea fallida y el run `partial` |
| 10 | Confirmación (fase C, por fuente) | Al terminar las tareas de una fuente: detección de tormenta (G12) y luego lecturas de confirmación con el presupuesto reservado (§3.6). Luego una transacción por candidato. | Sin presupuesto o tiempo ⇒ `pending_confirmation_at` |
| 11 | Componer | Alertas `delivery='new'` → `outbox` (individual o resumen), avisos de salud y respuestas a `/seguir` tras la primera lectura | — |
| 12 | Bandeja de entrada, pasada 2 | Los comandos que llegaron durante el ciclo se responden en este mismo ciclo | — |
| 13 | Entrega | Máximo 15 mensajes, 1,1 s entre envíos, por prioridad (§5.5) | Lo restante queda `pending` |
| 14 | Mantenimiento | Primer ciclo después de las 03:00 de Bogotá (`bot_state.last_maintenance_on < hoy`). Resumen diario en el primer ciclo después de las 08:00. | Errores registrados; el ciclo sigue |
| 15 | Cierre | Contadores en `source_hourly_stat`, `run.finished_at` y `status`, liberación del lease, ping de healthchecks si está configurado. `$GITHUB_STEP_SUMMARY` solo con conteos. | — |

**Códigos de salida:**
- **0:** `ok`, `partial` (una tienda falló, que es lo esperable), `skipped_*`.
- **1:** bug del orquestador, lease perdido, Telegram 403, o 3 `skipped_locked` seguidos.
- **2:** configuración inválida.
- **3:** BD inaccesible.

**Tamaño y consumo:**
- Duración esperada: 2-4 min, más unos 25 s de arranque con la caché de uv.
- RAM pico menor de 150 MB: no hay navegador, el HTML se parsea con `selectolax` y las respuestas se descartan en cuanto se extraen.
- CPU menor de 30 s por ciclo.

**Por qué es síncrono.** Con unas 110 peticiones como máximo por ciclo y pausas de 2 a 12 s por tienda, los turnos rotativos superponen las esperas de distintas tiendas en un solo hilo. El tiempo total queda acotado por la tienda más lenta, unos 2 min para Amazon. asyncio y TaskGroup no aportan nada a esta escala y agregan modos de falla (cancelación entre tareas hermanas, conexión compartida con candado).

### 1.4 Patrones: problema → patrón → dónde

| Problema | Patrón | Dónde |
|---|---|---|
| Mezclar COP con USD, o usar float | Value Object `Money(minor: int, currency)`; `CurrencyMismatch` | `domain/money.py` |
| Cada tienda habla un formato distinto | Adapter (capa anticorrupción) que normaliza a `Offer` y `Lead` | `sources/*.py` |
| Elegir el adaptador por código o host sin cadenas if/else; agregar tiendas sin tocar el núcleo | Strategy con registro (`SOURCES`, `route_url`) | `sources/registry.py` |
| Ritmo, presupuesto, reintentos y detección de bloqueo repetidos en cada adaptador | Decorator `PacedClient` sobre el transporte | `infra/http.py` |
| Fallas transitorias | Retry con backoff exponencial y *full jitter* | `PacedClient` |
| Seguir golpeando a una tienda que bloquea en procesos efímeros | Circuit Breaker **persistido** (transición pura, estado en `source`) | `domain/health.py` + tabla `source` |
| SQL fuera del dominio | Repository como módulo de funciones (una sola implementación, sin Protocol), probado contra Postgres real | `infra/db.py` |
| Caída entre el commit y el envío a Telegram | Transactional Outbox | `outbox` |
| Telegram reentrega updates | Consumidor idempotente (offset en la misma transacción) | `app/inbox.py` |
| Ejecuciones solapadas (cron, disparo externo, manual, migración) | Lease con TTL más `concurrency` de Actions | `run_lease` |
| Guardar cada lectura no cabe en 500 MB y sesga por muestreo | Codificación por segmentos (run-length) | `price_segment` |
| Probar la lógica de precios sin I/O | Núcleo funcional con capa imperativa | `domain` frente a `app` |
| Ensamblar sin framework | Composition root manual | `__main__.py` |

**No se usa:** ORM, Unit of Work abstracto (alcanza con `with conn.transaction()`), asyncio, colas, caché, contenedor de DI, eventos de dominio ni decaimiento exponencial.

---

## 2. Modelo de datos (DDL)

### 2.1 Convenciones

- **Esquema propio `discountz`**, no `public`. La Data API de Supabase (PostgREST) se **desactiva** en el proyecto, y además se revoca el acceso a `anon` y `authenticated`.
- **Requisitos de Postgres:** versión 15 o mayor, por `ON DELETE SET NULL (col)`. CI usa la 17, la misma versión mayor que Supabase (se verifica en el mini-spike M5).
- **Dinero:** `bigint` en unidad menor. COP usa exponente 0 (pesos) y USD exponente 2 (centavos). La moneda la fija `source.currency` y no se repite por fila. Los porcentajes van en puntos básicos (`_bp`).
- **Instantes:** `timestamptz`, con la sesión en UTC (configurado en el rol). Los instantes de negocio los escribe la app con el `now` inyectado; los de auditoría (`created_at`, `updated_at`) los pone la BD.
- **Estados:** `text` con `CHECK`, no `ENUM`, porque así se pueden revertir en una migración.
- **Sin `created_at`/`updated_at` en `price_segment`**, a propósito: `started_at` y `last_seen_at` cumplen ese papel y es la tabla de más volumen.

### 2.2 DDL completo

`infra/migrate.py` crea, antes de la primera migración, `CREATE SCHEMA IF NOT EXISTS discountz` y la tabla siguiente:

```sql
CREATE TABLE IF NOT EXISTS discountz.schema_migrations (
  version    integer PRIMARY KEY CHECK (version > 0),
  name       text NOT NULL CHECK (name ~ '^[a-z0-9_]{1,60}$'),
  checksum   char(64) NOT NULL,                 -- sha256 del .up.sql; falla si se edita una migración aplicada
  applied_at timestamptz NOT NULL DEFAULT now()
);
```

`migrations/0001_init.up.sql` (corre en una sola transacción):

```sql
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

CREATE DOMAIN category_slug AS text CHECK (VALUE IN ('tecnologia','componentes','hogar','ropa','otros'));
CREATE DOMAIN currency_code AS char(3) CHECK (VALUE IN ('COP','USD'));
CREATE DOMAIN money_minor  AS bigint  CHECK (VALUE > 0);

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- ───────────── Fuentes: catálogo + breaker + salud (estado que sobrevive entre procesos)
CREATE TABLE source (
  code                       text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]{1,31}$'),
  kind                       text NOT NULL CHECK (kind IN ('store','feed')),
  currency                   currency_code NOT NULL,
  enabled                    boolean NOT NULL DEFAULT true,          -- /fuente on|off
  ramp_until                 timestamptz,                            -- presupuesto al 50 % hasta esta fecha
  breaker                    text NOT NULL DEFAULT 'closed' CHECK (breaker IN ('closed','open')),
  breaker_open_until         timestamptz,                            -- semiabierto = open AND now >= open_until
  breaker_trips              smallint NOT NULL DEFAULT 0 CHECK (breaker_trips BETWEEN 0 AND 20),
  last_trip_at               timestamptz,
  last_trip_kind             text CHECK (last_trip_kind IN ('blocked','unavailable','parse')),
  consecutive_unavailable    smallint NOT NULL DEFAULT 0 CHECK (consecutive_unavailable >= 0),
  consecutive_parse_failures smallint NOT NULL DEFAULT 0 CHECK (consecutive_parse_failures >= 0),
  pace_multiplier            smallint NOT NULL DEFAULT 1 CHECK (pace_multiplier IN (1,2,4)),
  quarantine_cycles          smallint NOT NULL DEFAULT 0 CHECK (quarantine_cycles BETWEEN 0 AND 10),
  health                     text NOT NULL DEFAULT 'healthy'
                             CHECK (health IN ('healthy','degraded','blocked','broken')),
  health_since               timestamptz NOT NULL DEFAULT now(),
  notified_health            text NOT NULL DEFAULT 'healthy'
                             CHECK (notified_health IN ('healthy','degraded','blocked','broken')),
  last_notice_at             timestamptz,
  last_success_at            timestamptz,
  canary_checked_at          timestamptz,
  canary_ok                  boolean,
  created_at                 timestamptz NOT NULL DEFAULT now(),
  updated_at                 timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT source_breaker_ck CHECK ((breaker = 'open') = (breaker_open_until IS NOT NULL)),
  CONSTRAINT source_trip_ck    CHECK ((last_trip_kind IS NULL) = (last_trip_at IS NULL)),
  CONSTRAINT source_canary_ck  CHECK ((canary_ok IS NULL) = (canary_checked_at IS NULL))
);
-- Datos de referencia que exigen las FKs (no son semilla de prueba).
INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('exito','store','COP',true,NULL), ('carulla','store','COP',true,NULL),
  ('olimpica','store','COP',true,NULL), ('panamericana','store','COP',true,NULL),
  ('amazon_us','store','USD',true, now() + interval '7 days'),
  ('newegg','store','USD',true, now() + interval '7 days'),
  ('walmart_us','store','USD',false,NULL),      -- puerta de activación §4.1
  ('bestbuy_us','store','USD',false,NULL),      -- puerta de activación §4.1
  ('slickdeals','feed','USD',true,NULL);

-- Contadores por hora: presupuesto de 24 h, tasas de éxito, /estado (retención 90 d)
CREATE TABLE source_hourly_stat (
  source           text NOT NULL REFERENCES source(code) ON DELETE CASCADE,
  hour_start       timestamptz NOT NULL CHECK (extract(epoch FROM hour_start)::bigint % 3600 = 0),
  requests         integer NOT NULL DEFAULT 0 CHECK (requests >= 0),   -- cada intento HTTP, con reintentos
  ok               integer NOT NULL DEFAULT 0 CHECK (ok >= 0),
  blocked          integer NOT NULL DEFAULT 0 CHECK (blocked >= 0),
  unavailable      integer NOT NULL DEFAULT 0 CHECK (unavailable >= 0),
  parse_errors     integer NOT NULL DEFAULT 0 CHECK (parse_errors >= 0),
  not_found        integer NOT NULL DEFAULT 0 CHECK (not_found >= 0),
  confirm_mismatch integer NOT NULL DEFAULT 0 CHECK (confirm_mismatch >= 0),
  offers           integer NOT NULL DEFAULT 0 CHECK (offers >= 0),
  latency_ms_sum   bigint  NOT NULL DEFAULT 0 CHECK (latency_ms_sum >= 0),
  latency_ms_max   integer NOT NULL DEFAULT 0 CHECK (latency_ms_max >= 0),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, hour_start),                                     -- cubre la FK
  CONSTRAINT shs_counts_ck CHECK (ok + blocked + unavailable + parse_errors + not_found <= requests)
);
CREATE INDEX source_hourly_stat_hour_ix ON source_hourly_stat (hour_start);   -- retención

-- ───────────── Listing: una VARIANTE vendible en una fuente (SKU VTEX, ASIN, usItemId, Item#, sku Best Buy)
CREATE TABLE listing (
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source                  text NOT NULL REFERENCES source(code) ON DELETE RESTRICT,
  external_id             text NOT NULL CHECK (external_id ~ '^[A-Za-z0-9]{1,40}$'),
  parent_external_id      text CHECK (parent_external_id ~ '^[A-Za-z0-9]{1,40}$'),  -- productId VTEX, ASIN padre
  canonical_url           text NOT NULL CHECK (canonical_url ~ '^https://' AND length(canonical_url) <= 500),
  title                   text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
  category                category_slug NOT NULL DEFAULT 'otros',
  first_seen_at           timestamptz NOT NULL,
  last_seen_at            timestamptz NOT NULL,
  last_price_minor        money_minor,
  last_list_price_minor   money_minor,
  last_in_stock           boolean NOT NULL,
  last_seller_class       char(1) NOT NULL CHECK (last_seller_class IN ('f','m','u')),
  alltime_min_minor       money_minor,                 -- mínimo robusto; sobrevive a la retención
  alltime_min_at          timestamptz,
  history_reset_at        timestamptz,                 -- G6/G7: unidad o identidad cambió
  pending_confirmation_at timestamptz,                 -- candidato sin confirmar: prioridad en el siguiente plan
  muted_until             timestamptz,
  gone_at                 timestamptz,                 -- 404 tres veces
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT listing_identity_uq UNIQUE (source, external_id),          -- identidad entre ciclos; cubre FK
  CONSTRAINT listing_seen_ck     CHECK (last_seen_at >= first_seen_at),
  CONSTRAINT listing_min_pair_ck CHECK ((alltime_min_minor IS NULL) = (alltime_min_at IS NULL))
) WITH (fillfactor = 80);                                                 -- UPDATE HOT por lectura
CREATE INDEX listing_last_seen_ix ON listing (last_seen_at);              -- retención
CREATE INDEX listing_pending_ix   ON listing (source) WHERE pending_confirmation_at IS NOT NULL;

-- ───────────── Histórico: segmentos de estado constante (§3.2)
CREATE TABLE price_segment (
  listing_id       bigint      NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
  started_at       timestamptz NOT NULL,                 -- primera lectura con este estado
  last_seen_at     timestamptz NOT NULL,                 -- SIN índice: UPDATE HOT por lectura
  ended_at         timestamptz,                          -- instante de la lectura siguiente; NULL = abierto
  price_minor      money_minor,                          -- NULL = sin precio comprable
  list_price_minor money_minor,                          -- máximo L visto en el segmento (tras G4)
  in_stock         boolean     NOT NULL,
  seller_class     char(1)     NOT NULL CHECK (seller_class IN ('f','m','u')),   -- tienda / marketplace / desconocido
  unit_key         text        NOT NULL DEFAULT 'un|1' CHECK (unit_key ~ '^[a-z]{1,8}\|[0-9.]{1,10}$'),
  status           char(1)     NOT NULL DEFAULT 'o' CHECK (status IN ('o','s','g')), -- ok / sin confirmar / glitch
  sample_count     integer     NOT NULL DEFAULT 1 CHECK (sample_count >= 1),
  PRIMARY KEY (listing_id, started_at),                                            -- cubre FK
  CONSTRAINT seg_order_ck CHECK (last_seen_at >= started_at),
  CONSTRAINT seg_next_ck  CHECK (ended_at IS NULL OR ended_at > last_seen_at),
  CONSTRAINT seg_stock_ck CHECK (NOT in_stock OR price_minor IS NOT NULL)
) WITH (fillfactor = 85);
CREATE UNIQUE INDEX price_segment_open_uq ON price_segment (listing_id) WHERE ended_at IS NULL;  -- 1 abierto

-- ───────────── Seguimiento (producto puntual que David pidió)
CREATE TABLE watch (
  id                    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,          -- "p12"
  source                text NOT NULL REFERENCES source(code) ON DELETE RESTRICT,
  listing_id            bigint REFERENCES listing(id) ON DELETE CASCADE,          -- NULL hasta resolver
  pending_ref           text CHECK (pending_ref ~ '^(id|slug|short):[A-Za-z0-9/_|-]{1,190}$'),
  status                text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','paused','gone')),
  requested_category    category_slug NOT NULL DEFAULT 'otros',                    -- se copia al listing al resolver
  target_price_minor    money_minor,
  target_set_at         timestamptz,
  next_check_at         timestamptz NOT NULL,
  last_checked_at       timestamptz,
  consecutive_failures  smallint NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
  consecutive_not_found smallint NOT NULL DEFAULT 0 CHECK (consecutive_not_found >= 0),
  reply_to_message_id   bigint,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT watch_resolved_ck CHECK ((status = 'pending') = (listing_id IS NULL)),
  CONSTRAINT watch_ref_ck      CHECK ((status = 'pending') = (pending_ref IS NOT NULL)),
  CONSTRAINT watch_target_ck   CHECK ((target_price_minor IS NULL) = (target_set_at IS NULL))
);
CREATE UNIQUE INDEX watch_listing_uq ON watch (listing_id) WHERE listing_id IS NOT NULL;   -- cubre FK
CREATE UNIQUE INDEX watch_pending_uq ON watch (source, pending_ref) WHERE pending_ref IS NOT NULL;
CREATE INDEX watch_source_due_ix     ON watch (source, next_check_at);                    -- cubre FK + plan

-- ───────────── Búsqueda (descubrimiento; en 'slickdeals' es un filtro de leads)
CREATE TABLE search (
  id                   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,           -- "b3"
  source               text NOT NULL REFERENCES source(code) ON DELETE RESTRICT,
  query                text NOT NULL CHECK (length(query) BETWEEN 2 AND 100
                         AND query = lower(regexp_replace(btrim(query), '\s+', ' ', 'g'))),
  category             category_slug NOT NULL,
  exclude_terms        text[] NOT NULL DEFAULT '{}' CHECK (cardinality(exclude_terms) <= 20),
  min_price_minor      money_minor,
  max_price_minor      money_minor,
  enabled              boolean NOT NULL DEFAULT true,
  next_run_at          timestamptz NOT NULL,
  last_run_at          timestamptz,
  last_result_count    smallint CHECK (last_result_count >= 0),
  consecutive_failures smallint NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
  consecutive_empty    smallint NOT NULL DEFAULT 0 CHECK (consecutive_empty >= 0),  -- SOFT_EMPTY
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT search_uq       UNIQUE (source, query),                                -- cubre FK
  CONSTRAINT search_range_ck CHECK (min_price_minor IS NULL OR max_price_minor IS NULL
                                    OR min_price_minor < max_price_minor)
);
CREATE INDEX search_due_ix ON search (next_run_at) WHERE enabled;

-- ───────────── Outbox: todo mensaje saliente (alerta, resumen, respuesta, sistema)
CREATE TABLE outbox (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  purpose             text NOT NULL CHECK (purpose IN ('alert','digest','reply','system')),
  dedup_key           text NOT NULL UNIQUE CHECK (length(dedup_key) BETWEEN 3 AND 120),
  priority            smallint NOT NULL CHECK (priority BETWEEN 0 AND 9),               -- 0 = más urgente
  body_html           text NOT NULL CHECK (length(body_html) BETWEEN 1 AND 8000),       -- visible ≤ 4096: render
  reply_markup        jsonb CHECK (reply_markup IS NULL OR jsonb_typeof(reply_markup) = 'object'),
  silent              boolean NOT NULL DEFAULT false,
  reply_to_message_id bigint,
  status              text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','sending','sent','failed','expired')),
  attempts            smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 10),
  not_before          timestamptz NOT NULL,
  expires_at          timestamptz NOT NULL,
  sending_since       timestamptz,
  sent_at             timestamptz,
  telegram_message_id bigint,
  last_error_code     text CHECK (length(last_error_code) <= 60),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT outbox_expiry_ck  CHECK (expires_at > not_before),
  CONSTRAINT outbox_sending_ck CHECK ((status = 'sending') = (sending_since IS NOT NULL)),
  CONSTRAINT outbox_sent_ck    CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
  CONSTRAINT outbox_msgid_ck   CHECK (telegram_message_id IS NULL OR status = 'sent')
);
CREATE INDEX outbox_ready_ix   ON outbox (priority, not_before) WHERE status IN ('pending','sending');
CREATE INDEX outbox_created_ix ON outbox (created_at);                                 -- retención

-- ───────────── Alerta: hecho de dominio (dedupe, cooldown, rearme, ancla de tendencia)
CREATE TABLE alert (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  listing_id           bigint NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
  segment_started_at   timestamptz,                                  -- NULL tras la retención del segmento
  watch_id             integer REFERENCES watch(id) ON DELETE SET NULL,
  outbox_id            bigint  REFERENCES outbox(id) ON DELETE SET NULL,
  kind                 text NOT NULL CHECK (kind IN
                       ('price_error','historic_low','target_reached','good_deal','community_deal')),
  maturity             text NOT NULL CHECK (maturity IN ('cold','warm','mature')),
  price_minor          money_minor NOT NULL,
  reference_minor      money_minor,
  real_saving_bp       integer CHECK (real_saving_bp BETWEEN -100000 AND 10000),
  advertised_saving_bp integer CHECK (advertised_saving_bp BETWEEN 1 AND 10000),
  flags                text[] NOT NULL DEFAULT '{}' CHECK (flags <@ ARRAY[
                         'inflated_list','advertised_gap','pre_inflated','flash_seen','gap_before',
                         'short_history','marketplace','target_above_typical','ref_antigua','unit_changed',
                         'storm','target_also','persistence_only','hot',
                         'community']::text[]),
  delivery             text NOT NULL DEFAULT 'new' CHECK (delivery IN ('new','queued','muted','suppressed')),
  observed_at          timestamptz NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT alert_segment_fk FOREIGN KEY (listing_id, segment_started_at)
             REFERENCES price_segment (listing_id, started_at) ON DELETE SET NULL (segment_started_at),
  CONSTRAINT alert_dedup_uq   UNIQUE (listing_id, segment_started_at, kind),   -- cubre ambas FKs por prefijo
  CONSTRAINT alert_queued_ck  CHECK ((delivery = 'queued') = (outbox_id IS NOT NULL))
);
CREATE INDEX alert_listing_created_ix ON alert (listing_id, created_at DESC);  -- cooldown y ancla
CREATE INDEX alert_watch_ix   ON alert (watch_id)  WHERE watch_id IS NOT NULL;
CREATE INDEX alert_outbox_ix  ON alert (outbox_id) WHERE outbox_id IS NOT NULL;
CREATE INDEX alert_new_ix     ON alert (observed_at) WHERE delivery = 'new';
CREATE INDEX alert_created_ix ON alert (created_at);                           -- retención

-- ───────────── Singletons de operación
CREATE TABLE bot_state (
  id                  smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  telegram_offset     bigint NOT NULL DEFAULT 0 CHECK (telegram_offset >= 0),   -- último update_id aplicado
  muted_until         timestamptz,                                              -- /silenciar todo N
  fx_last_attempt_at  timestamptz,
  last_maintenance_on date,
  last_summary_on     date,
  last_db_size_bytes  bigint CHECK (last_db_size_bytes >= 0),
  discovery_paused    boolean NOT NULL DEFAULT false,                           -- guarda de 450 MB
  updated_at          timestamptz NOT NULL DEFAULT now()
);
INSERT INTO bot_state DEFAULT VALUES;

CREATE TABLE run_lease (
  id          smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  holder      text CHECK (length(holder) <= 80),           -- 'gh:<run_id>:<attempt>' | 'migrate:<run_id>' | 'local:<host>'
  acquired_at timestamptz,
  expires_at  timestamptz,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT lease_pair_ck CHECK ((holder IS NULL) = (expires_at IS NULL)
                              AND (holder IS NULL) = (acquired_at IS NULL))
);
INSERT INTO run_lease DEFAULT VALUES;

CREATE TABLE run (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  gh_run_id         bigint,
  gh_run_attempt    smallint CHECK (gh_run_attempt >= 1),
  trigger_kind      text NOT NULL CHECK (trigger_kind IN ('schedule','workflow_dispatch','local')),
  status            text NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','ok','partial','failed','skipped_locked','skipped_schema')),
  started_at        timestamptz NOT NULL,
  finished_at       timestamptz,
  gap_seconds       integer CHECK (gap_seconds >= 0),
  requests          integer NOT NULL DEFAULT 0 CHECK (requests >= 0),
  alerts_created    integer NOT NULL DEFAULT 0 CHECK (alerts_created >= 0),
  messages_sent     integer NOT NULL DEFAULT 0 CHECK (messages_sent >= 0),
  updates_processed integer NOT NULL DEFAULT 0 CHECK (updates_processed >= 0),
  error_code        text CHECK (error_code ~ '^[a-z0-9_.]{1,60}$'),       -- código, nunca texto libre
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT run_gh_uq      UNIQUE (gh_run_id, gh_run_attempt),
  CONSTRAINT run_local_ck   CHECK ((trigger_kind = 'local') = (gh_run_id IS NULL)),
  CONSTRAINT run_attempt_ck CHECK ((gh_run_id IS NULL) = (gh_run_attempt IS NULL)),
  CONSTRAINT run_open_ck    CHECK ((status = 'running') = (finished_at IS NULL)),
  CONSTRAINT run_time_ck    CHECK (finished_at IS NULL OR finished_at >= started_at)
);
CREATE INDEX run_started_ix ON run (started_at);

-- ───────────── Triggers de auditoría
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['source','source_hourly_stat','listing','watch','search','outbox','alert',
                           'bot_state','run_lease','run'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON discountz.%I FOR EACH ROW
                    EXECUTE FUNCTION discountz.set_updated_at()', t || '_touch', t);
  END LOOP;
END $$;

-- ───────────── Permisos (los roles se crean a mano con su secreto: runbook §7.6)
REVOKE ALL ON SCHEMA discountz FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON SCHEMA discountz FROM anon, authenticated';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'discountz_app') THEN
    GRANT USAGE ON SCHEMA discountz TO discountz_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA discountz TO discountz_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA discountz TO discountz_app;
  END IF;
END $$;
```

`migrations/0001_init.down.sql`:

```sql
SET LOCAL search_path = discountz;
DROP TABLE alert, outbox, search, watch, price_segment, listing, source_hourly_stat,
           run, run_lease, bot_state, source;
DROP FUNCTION set_updated_at();
DROP DOMAIN money_minor, currency_code, category_slug;
-- schema_migrations y el esquema los administra el ejecutor; la fila 0001 se borra al revertir.
```

Cada migración posterior incluye `ALTER DEFAULT PRIVILEGES ... GRANT` cuando crea tablas, o repite el bloque de permisos.

### 2.3 SQL operativo clave

```sql
-- Adquirir lease (reloj de la BD como autoridad; TTL = timeout del job + 1 min)
UPDATE discountz.run_lease
   SET holder = %(holder)s, acquired_at = now(), expires_at = now() + interval '16 minutes'
 WHERE id = 1 AND (holder IS NULL OR expires_at < now())
RETURNING holder;                             -- 0 filas ⇒ skipped_locked

-- Liberar
UPDATE discountz.run_lease SET holder = NULL, acquired_at = NULL, expires_at = NULL
 WHERE id = 1 AND holder = %(holder)s;

-- Extender el segmento abierto (≈ 95 % de las lecturas; UPDATE HOT)
UPDATE discountz.price_segment
   SET last_seen_at = %(t)s, sample_count = sample_count + 1,
       list_price_minor = GREATEST(list_price_minor, %(l)s)
 WHERE listing_id = %(id)s AND ended_at IS NULL;

-- Cerrar y abrir (cambio de estado o hueco > H), en la misma transacción
UPDATE discountz.price_segment SET ended_at = %(t)s WHERE listing_id = %(id)s AND ended_at IS NULL;
INSERT INTO discountz.price_segment (listing_id, started_at, last_seen_at, price_minor, list_price_minor,
                                     in_stock, seller_class, unit_key, status)
VALUES (%(id)s, %(t)s, %(t)s, %(p)s, %(l)s, %(stock)s, %(seller)s, %(unit)s, %(status)s);

-- Histórico de 365 d de N listings en una ida (prefijo de la PK)
SELECT listing_id, started_at, last_seen_at, ended_at, price_minor, list_price_minor,
       in_stock, seller_class, unit_key, status,
       lag(last_seen_at) OVER (PARTITION BY listing_id ORDER BY started_at) AS prev_last_seen_at
  FROM discountz.price_segment
 WHERE listing_id = ANY(%(ids)s) AND coalesce(ended_at, 'infinity') >= %(since)s
 ORDER BY listing_id, started_at;

-- Avance idempotente del offset de Telegram
UPDATE discountz.bot_state SET telegram_offset = %(uid)s WHERE id = 1 AND telegram_offset < %(uid)s;
```

**Por qué no hay `EXCLUDE` contra segmentos solapados.** Indexaría `last_seen_at` y rompería los UPDATE HOT. La invariante de un solo segmento abierto la garantiza `price_segment_open_uq`, y el lease con TTL asegura un único escritor.

### 2.4 Crecimiento frente a 500 MB

Tamaño por segmento: tupla de unos 66 B, más 24 B de cabecera, más 4 B de puntero, con fillfactor 85. A eso se suma la PK (bigint, timestamptz) de unos 32 B. **Total ≈ 150 B por segmento.**

| Tabla | Supuesto año 1 (base: 200 seguimientos, 20 búsquedas, ~1.500 descubiertos activos) | Filas/año | MB año 1 | Retención |
|---|---|---|---|---|
| `price_segment` | 200 × 1,5 cambios/día + 1.500 × 0,4/día ≈ 900/día | 330.000 | ~50 | 400 d; poda de descubiertos (abajo) |
| `listing` | ~8.000 únicos × ~500 B | 8.000 | ~4 | Descubiertos sin seguimiento ni alerta en 365 d, sin verse en 120 d |
| `alert` | ~15/día × ~300 B | 5.500 | ~2 | 400 d |
| `outbox` | ~25/día × ~1,8 KB | — | ~1,5 vivas | 30 d |
| `run` | 52.560 × ~200 B | — | ~1 vivas | 30 d |
| `source_hourly_stat` | 9 × 24 × 90 vivas × ~120 B | — | ~2,5 | 90 d |
| Resto | < 2.000 filas | — | < 1 | — |
| Esquemas internos de Supabase | medir el día 1 (mini-spike M5) | — | ~30-40 | — |
| **Total × 1,25 (bloat)** | | | **~115 MB año 1, ~180 año 2** | |

**Peor caso** (repricing estilo Amazon: 300 seguimientos × 4 cambios/día y 3.000 descubiertos × 1/día): unos 4.200 segmentos diarios, unos 230 MB por año. Se llega a 400 MB hacia el mes 18.

**Guardas automáticas** (mantenimiento diario, `pg_database_size(current_database())`):

| Tamaño | Acción |
|---|---|
| ≥ 300 MB | Aviso semanal con el top 5 de tablas por tamaño |
| ≥ 400 MB | Poda por lotes de 5.000 filas: (1) listings descubiertos sin seguimiento ni alertas y sin verse en 60 d (en cascada); (2) segmentos de más de 180 d de listings sin seguimiento |
| ≥ 450 MB | `bot_state.discovery_paused = true`: se pausan las búsquedas y solo siguen los seguimientos. Aviso urgente |

**Mantenimiento diario, en este orden:**
1. Actualiza `listing.alltime_min_*` con los segmentos que van a salir. Excluye los de `status ∈ {s, g}` y los relámpagos comprobados (§3.3).
2. Borra los segmentos con `ended_at < now − 400 d`.
3. Reclasifica a `status='o'` los segmentos `'g'` que llevan más de 7 d. Un «error de precio» que dura una semana es el precio real (ajuste propio, §3.12).
4. Aplica la retención de `run`, `outbox`, `source_hourly_stat` y `alert`.

---

## 3. Algoritmo de validación de ofertas

El algoritmo final es el del crítico adversarial, con los ajustes de §3.12. Todo vive en `domain/` y es puro. Usa **solo enteros**: dinero en unidad menor, pesos en segundos y porcentajes en bp. Una evaluación ocurre **solo por evento**:
- se abrió un segmento nuevo;
- cambió el objetivo del seguimiento;
- hay una confirmación pendiente;
- llegó un lead de Slickdeals.

### 3.1 Principios

1. El histórico son segmentos de estado constante `(precio, en_stock, seller_class, unit_key)`. Las estadísticas se ponderan por **tiempo observado**, no por número de lecturas.
2. La referencia es conservadora: `R = mín(M90, M30, M_prior)` con mediana inferior. Siempre es un precio que existió.
3. La **rareza** `F(P)` es obligatoria para `GOOD_DEAL`. Que P esté en un cuantil bajo no basta.
4. Los umbrales se adaptan a la volatilidad propia del producto, pero solo con ≥ 60 d de cobertura.
5. Hay un **ancla de tendencia** para detectar escaleras descendentes.
6. Se distingue **confirmación independiente** de **persistencia**. El patrón decimal `10^k` nunca alerta sin una ruta independiente.
7. No entra en las estadísticas nada que esté sin stock, que venga de un vendedor no admitido o de otra unidad, ni lo que tenga `status` `s` o `g`.
8. El % anunciado por la tienda **nunca** dispara una alerta.

### 3.2 Segmentos y pesos

`apply_sample(open_seg, obs, H) -> Extend | CloseAndOpen | Ignore` (puro):
- `obs.t ≤ open_seg.last_seen_at` → `Ignore`.
- Mismo estado `(price, in_stock, seller_class, unit_key)` y `t − last_seen_at ≤ H` → `Extend` (`last_seen_at = t`, `sample_count + 1`, `list_price = máx`).
- En cualquier otro caso → `CloseAndOpen` (`ended_at = t`; segmento nuevo con `status` según §3.6).

```
H          = máx(12 h, 1,5 × intervalo_nominal)   # intervalo_nominal = mín(cadencia del seguimiento,
                                                  #   cadencia de la búsqueda que lo trajo)
                                                  # VTEX seguimiento 60 min → 12 h; búsqueda Walmart 12 h → 18 h
fin_e      = mín(ended_e, last_seen_e + H)
s_cur      = started_at del segmento actual (queda FUERA de la base)
w_e[a,b)   = máx(0, mín(fin_e, b, s_cur) − máx(started_e, a, history_reset_at))   # segundos, int
cota_sup_e = ended_e − last_seen_{e−1}            # duración máxima posible; sin segmento previo = ∞
relámpago comprobado ⇔ cota_sup_e < 6 h
```

**Conjunto elegible E:** `in_stock ∧ status = 'o' ∧ unit_key = unit_key_actual ∧ (seller_class = 'f' ∨ (allow_marketplace(cat) ∧ seller_class = 'm'))`.

### 3.3 Estadísticos (enteros)

```
Q_q(I)    = mín{ p : 10000·Σ_{p_e ≤ p} w_e(I) ≥ q_bp · Σ w_e(I) }      # cuantil inferior; None si Σw = 0
C         = Σ_{e∈E} w_e[t−W, t)   (segundos; se compara con días·86400)
días      = nº de días calendario de Bogotá que tocan los intervalos cubiertos [started_e, fin_e) de E en W
M90       = Q_5000([t−W, s_cur))
M30       = Q_5000([t−30 d, s_cur))        válida si Σw ≥ 7 d
M_prior   = Q_5000([t−W, s_cur − K))       válida si Σw ≥ 7 d
R         = mín(M90, M30 válida, M_prior válida)
M_rec     = Q_5000([s_cur − 14 d, s_cur))
Q10, Q90  = Q_1000, Q_9000 sobre [t−W, s_cur)
v         = (Q90 − Q10)·10000 // R                                       # volatilidad propia (bp)
F(P)      = 10000 · Σ{ w_e : p_e·10000 ≤ P·(10000 + 50) } // Σ w_e      # rareza sobre [t−W, s_cur)
m_robust  = mín(listing.alltime_min, mín{ p_e : e∈E, retenido ≤ 365 d, ¬relámpago_comprobado })
raw_min   = mín{ p_e : e∈E }                                             # solo para mostrar
max_chg   = máx{ p : Σ_{e∈E, p_e = p} w_e[t−W, s_cur) ≥ 24 h }           # nivel realmente cobrado
A         = precio de la última alerta (kind ≠ target_reached) del listing en 180 d;
            si no hay: Q_5000([t−90 d, t−60 d)) si Σw ≥ 7 d; si no: None
S_real    = (R − P)·10000 // R                                           # piso: conservador
S_adv     = (L − P)·10000 // L   si L > P, si no None
T_eff     = T_good                        si C < 60 d
            máx(T_floor, mín(T_good, 2·v))   si C ≥ 60 d
D_eff     = D_low                         si C < 60 d
            máx(D_floor, mín(D_low, v))      si C ≥ 60 d
abs_ok    = R − P ≥ min_abs(moneda)       # COP 10.000 · USD 500 ¢
```

**Ventana extendida.** Si `C(W) < M_cat` pero `C(365 d) ≥ M_cat` (por ejemplo, tras un agotado largo), se recalcula con `W' = 365 d`, la madurez se baja a `warm` y se agrega la bandera `ref_antigua`.

### 3.4 Guardas, en orden (la primera que rechaza corta el proceso)

| # | Guarda | Regla | Efecto |
|---|---|---|---|
| G1 | Esquema | pydantic estricto en el adaptador; `json.loads(parse_float=Decimal)`; `P > 0` | `ParseError` |
| G2 | Moneda | Moneda declarada o detectada (marcadores `COP`, `US$`, `i18n-prefs`) = `source.currency` | `ParseError` |
| G3 | Cotas absolutas | COP ∈ [1.000; 100.000.000]; USD ∈ [50; 5.000.000] ¢ | `ParseError` |
| G4 | Tachado incoherente | `L ≤ P` o `L > 20·P` | Se descarta **solo L**; la lectura es válida |
| G5 | Desplazamiento del tachado | `L_prev` existe y `L_prev/L` o `L/L_prev` está a ±3 % de 10^k, k ∈ {1,2,3} | `PARSE_SHIFT`: no se persiste; anomalía `parse` |
| G6 | Unidad | `unit_key` ≠ el del segmento anterior | Segmento nuevo, `history_reset_at = t`, bandera `unit_changed`, sin alerta |
| G7 | Identidad | external_id observado ≠ pedido ⇒ se rutea al listing observado. Reinicio por título solo si cambian los tokens duros (marca, capacidad `\d+(gb\|tb\|w\|hz\|in)`, modelo `[a-z]{1,4}-?\d{2,}`) en 2 ciclos seguidos | Sin alerta en ese segmento |
| G8 | Precio no comprable | badges `see_in_cart`, `card_price`, `prime_only`, cupón; nodos «More Buying Choices», «used & new», «See options» | `price = None` (no entra en estadísticas) |
| G9 | Stock | Sin stock | Segmento sin stock; **no se evalúa** |
| G10 | Lote constante | n ≥ 5, moda ≥ 80 % **y** ≥ 3 listings del lote con histórico y `\|moda − R_listing\| > 30 %·R_listing` | Se descarta el lote, anomalía `parse`. Sin histórico: se persiste y no se alerta en ese ciclo |
| G11 | Patrón decimal | `R/P` o `P/R` a ±3 % de 10^k | `DECIMAL_SHIFT` ⇒ §3.6 |
| G12 | Tormenta (por fuente y ciclo) | ≥ 2 listings con candidato a `PRICE_ERROR` o `DECIMAL_SHIFT` | §3.6: sin ruta independiente se suprimen y se avisa «posible cambio de formato»; con ruta independiente confirmada se envían agrupados con la bandera `storm` |
| G13 | Salto brusco | `p_prev` en stock visto ≤ 7 d y (`2·P ≤ p_prev` o `P ≥ 2·p_prev`) | `needs_confirmation` para cualquier veredicto |
| G14 | Vendedor | `seller_class = 'u'`, o `'m'` con `allow_marketplace = false` | Solo se permite `TARGET_REACHED`, con bandera `marketplace` |

### 3.5 Madurez y veredictos

| Madurez | Condición | Se permite |
|---|---|---|
| `cold` | `C < 3 d` | `TARGET_REACHED` (con G13; si `2·P ≤ objetivo` ⇒ confirmación); `PRICE_ERROR` por pares; `COMMUNITY_DEAL` |
| `warm` | `3 d ≤ C < M_cat` | `PRICE_ERROR`; `TARGET_REACHED`; `GOOD_DEAL` **solo en seguimientos**, con `T_eff + 1000` y bandera `short_history`; `COMMUNITY_DEAL` |
| `mature` | `C ≥ M_cat ∧ días ≥ 14` | Todo. La etiqueta «mínimo en 90 d» exige `C ≥ 60 d`; si no, «mínimo en N días de histórico» |

Rangos: `PRICE_ERROR` 5 > `HISTORIC_LOW` 4 > `TARGET_REACHED` 3 > `GOOD_DEAL` 2 > `COMMUNITY_DEAL` 1. Se evalúan todos; gana el de mayor rango y los demás se agregan como etiquetas (`target_also` y otros).

```
PRICE_ERROR (candidato), siempre con seller_class = 'f':
  warm/mature: P·10000 ≤ R·(10000 − g) ∧ (m_robust is None ∨ P·10000 ≤ m_robust·7000)
  cold, pares: n_pares ≥ 8 ∧ P·10000 ≤ med_pares·3500 ∧ L ≠ None
               ∧ L·10000 ≥ med_pares·6000 ∧ P·10000 ≤ L·(10000 − g)
               (pares = otras ofertas en stock de la MISMA respuesta de búsqueda que pasan el filtro;
                med_pares = mediana inferior sin ponderar)
  → confirmar (§3.6)

HISTORIC_LOW: mature ∧ abs_ok ∧ P·10000 ≤ m_robust·(10000 − ε)
              ∧ ( S_real ≥ D_eff ∨ (A ≠ None ∧ P·10000 ≤ A·(10000 − D_trend)) )   # 2.º término: TENDENCIA

TARGET_REACHED: seguimiento con objetivo ∧ P ≤ objetivo

GOOD_DEAL: mature ∧ abs_ok ∧ F(P) ≤ F_max ∧ S_real ≥ T_eff
           o: seguimiento ∧ warm ∧ abs_ok ∧ F(P) ≤ F_max ∧ S_real ≥ T_eff + 1000
           (descubierto en warm: nunca)

COMMUNITY_DEAL: lead de Slickdeals que coincide con una búsqueda 'slickdeals' de David
                ∧ ficha leída: in_stock ∧ seller admitido ∧ P·10000 ≤ lead_price·10100
```

Un veredicto ≥ `GOOD_DEAL` que sale **solo de una búsqueda** (`origin='search'`) exige leer la ficha antes de alertar. Esa lectura es la confirmación de §3.6.

### 3.6 Confirmación: independiente frente a persistencia

Una **ruta independiente** es otro endpoint u otra vía de extracción con **otro parser**. Releer la misma ruta cuenta solo como *persistencia*.

| Fuente | Ruta principal | Ruta independiente |
|---|---|---|
| VTEX ×4 | `catalog_system/pub/products/search` | `POST /api/checkout/pub/orderForms/simulation?sc=1` (`sellingPrice` en **centavos**) |
| Amazon | `#corePrice_feature_div .a-offscreen` | `input#twister-plus-price-data-price` (si no está en la página, no hay ruta) |
| Walmart | `__NEXT_DATA__` | JSON-LD `offers.price` |
| Newegg | JSON-LD `offers.price` | HTML `.price-current` |
| Best Buy | JSON-LD `offers.price` | HTML `.priceView-customer-price span[aria-hidden=true]` |

```
Candidato o needs_confirmation (PRICE_ERROR, G13, búsqueda ≥ GOOD_DEAL, objetivo en cold con 2P ≤ objetivo):
  W persiste el segmento con status 's' (nunca entra en E); la alerta NO se crea todavía.
Fase C (al terminar las tareas de la fuente):
  1. G12: si hay ≥ 2 candidatos PRICE_ERROR/DECIMAL_SHIFT en la fuente:
       sin ruta independiente → se suprimen todos, aviso «posible cambio de formato», anomalía parse.
       con ruta independiente → se confirma cada uno; los confirmados salen en UN mensaje con bandera 'storm'.
  2. si DECIMAL_SHIFT:
       PRICE_ERROR solo con confirmación independiente (±1 %, stock, mismo vendedor y variante);
       sin ruta independiente → queda 's' para siempre, NUNCA alerta.
  3. si no:
       ruta independiente ±1 % → confirmado
       sin ruta independiente → persistencia: el siguiente ciclo (≥ 20 min después) relee la ficha;
         mismo P ±1 % y G12 negativa → confirmado con bandera 'persistence_only'
  4. confirmado: PRICE_ERROR → status 'g' (fuera de E); otros → status 'o'. Se crea la alerta.
  5. falla: se aplica la lectura de confirmación como muestra siguiente (abre un segmento 'o');
     confirm_mismatch+1; con 3 en 24 h en la fuente → health 'degraded' y aviso «posible extractor roto».
  6. sin presupuesto o sin tiempo: listing.pending_confirmation_at = now; prioridad 1 en el siguiente plan.
```

Presupuesto reservado para confirmar: `máx(2, 20 % de max_requests_per_run)` por fuente y ciclo.

### 3.7 Banderas (informan, nunca disparan una alerta)

| Bandera | Regla | Texto |
|---|---|---|
| `inflated_list` | `C ≥ 21 d ∧ L·100 > max_chg·102` | «tachado nunca cobrado en 90 d (máx. X)» |
| `advertised_gap` | `S_adv − máx(S_real, 0) ≥ 1500` | «anuncia 33,3 %, real 7,7 %» |
| `pre_inflated` | `M_rec·10000 ≥ M_prior·(10000 + ρ)` (contra `M_prior`, no contra R) | «subió a X en los 14 d previos» |
| `flash_seen` | `raw_min < m_robust` | «relámpago visto: X el 3-sep (duró máx. 4 h 46 min)» |
| `gap_before` | El último `last_seen` elegible antes de `s_cur` es anterior a `s_cur − 7 d` | «sin datos los últimos N días» |
| `short_history` | `cold` o `warm` | «histórico corto: N días» |
| `marketplace` | `seller_class = 'm'` | «vende un tercero» |
| `target_above_typical` | `R ≤ objetivo` | «tu objetivo está por encima del precio típico (X)» |
| `ref_antigua` | Ventana extendida | «referencia de hasta 365 d» |
| `unit_changed` | G6 | «cambió la presentación; histórico reiniciado» |
| `storm` | G12 con confirmación independiente | «varios errores a la vez en esta tienda: verifica» |
| `persistence_only` | Confirmado sin ruta independiente | «confirmado por 2 lecturas iguales del mismo origen» |

### 3.8 Deduplicación, cooldown y rearme (`decide`, puro)

```
1. veredicto NONE → Skip
2. muted (listing.muted_until > now ∨ bot_state.muted_until > now) → registrar con delivery 'muted'
3. ya existe (listing, segment_started_at, kind) → Skip                       # alert_dedup_uq
4. no hay `last` (cualquier delivery ≠ 'suppressed') en 180 d → Send
5. rank(nuevo) > rank(last) → Send (escalamiento)
6. P·10000 ≤ last.price·(10000 − 300) → Send (mejoró ≥ 3 %)
7. now − last.created_at ≥ cooldown_cat ∧ rearmado → Send
   rearmado (oferta)  ⇔ Σ w_e de segmentos e∈E posteriores a last con p_e·10000 ≥ last.reference·(10000 − T_eff/2) ≥ 24 h
   rearmado (TARGET)  ⇔ Σ w_e con p_e > objetivo ≥ 24 h  ∨  target_set_at > last.created_at
8. si no → Skip
```

Un listing que aparece en un seguimiento y en una búsqueda del mismo ciclo se evalúa una sola vez: primero los seguimientos, y luego un set `evaluated_listing_ids`.

**Resumen por categoría.** Con 3 o más `good_deal` o `community_deal` de una categoría en un mismo ciclo, se envía un solo mensaje con hasta 10 ítems.

**Horas de silencio.** De 23:00 a 07:00 (Bogotá) se envía con `disable_notification=true`, salvo `price_error`.

### 3.9 Parámetros

**Globales** (`[pricing]` del TOML):

| Parámetro | Valor |
|---|---|
| `H` | `máx(12 h, 1,5 × intervalo_nominal)` |
| Relámpago comprobado | cota superior < 6 h |
| `K` | 14 d (ropa 10 d) |
| Validez de `M30` y `M_prior` | ≥ 7 d de cobertura |
| Adaptación por volatilidad | solo con `C ≥ 60 d` |
| `ρ` (pre-inflado) | 800 bp |
| `D_trend` | 1000 bp (ropa 2000) |
| Ventana del ancla | 180 d |
| Tolerancia 10^k | ±300 bp, k ∈ {1,2,3} |
| Tolerancia de confirmación | ±100 bp |
| Persistencia | 2 lecturas ≥ 20 min aparte |
| Pares | ≥ 8; P ≤ 3500 bp de la mediana; L ≥ 6000 bp de la mediana |
| Tormenta | ≥ 2 candidatos por fuente y ciclo |
| Salto brusco | ×0,5 o ×2 frente a `p_prev` visto ≤ 7 d |
| Hueco | 7 d |
| `min_abs` | COP 10.000; USD 500 ¢ |
| Tolerancia de rareza | 50 bp |
| Re-aviso por mejora | 300 bp |
| Rearme | 24 h |
| Reclasificación de glitch | 7 d |

**Por categoría** (bp salvo que se indique):

| Categoría | W | M_cat | T_good | T_floor | D_low | D_floor | ε | g | F_max | Cooldown | Marketplace | Peso por defecto |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| tecnologia | 90 d | 21 d | 1200 | 800 | 600 | 300 | 200 | 5000 | 1000 | 72 h | no | 3,0 lb |
| componentes | 90 d | 21 d | 1000 | 700 | 500 | 300 | 200 | 5000 | 1000 | 72 h | sí (bandera) | 1,0 lb |
| hogar | 90 d | 21 d | 2000 | 1000 | 800 | 400 | 200 | 6000 | 1000 | 96 h | sí | 5,0 lb |
| ropa | 60 d | 14 d | 3000 | 2000 | 1500 | 1000 | 300 | 7500 | 1500 | 120 h | sí | 1,0 lb |
| otros | 90 d | 21 d | 1500 | 800 | 600 | 300 | 200 | 6000 | 1000 | 72 h | sí | 2,0 lb |

### 3.10 Núcleo (referencia de implementación)

```python
SECOND = timedelta(seconds=1)

def segment_weight_s(seg: Segment, a: datetime, b: datetime, s_cur: datetime,
                     h: timedelta, reset_at: datetime | None) -> int:
    end = seg.last_seen_at + h if seg.ended_at is None else min(seg.ended_at, seg.last_seen_at + h)
    lo = max(seg.started_at, a, reset_at) if reset_at else max(seg.started_at, a)
    hi = min(end, b, s_cur)
    return max(0, (hi - lo) // SECOND)          # timedelta // timedelta → int

def wq(points: Sequence[tuple[int, int]], q_bp: int) -> int | None:
    """Cuantil inferior ponderado. points = (price_minor, weight_s). Solo enteros."""
    total = sum(w for _, w in points)
    if total == 0:
        return None
    acc = 0
    for price, w in sorted(points):
        acc += w
        if acc * 10_000 >= q_bp * total:
            return price
    raise AssertionError("inalcanzable")

def rarity_bp(points: Sequence[tuple[int, int]], p: int, tol_bp: int = 50) -> int:
    total = sum(w for _, w in points)
    low = sum(w for price, w in points if price * 10_000 <= p * (10_000 + tol_bp))
    return low * 10_000 // total

def is_power10_shift(a: int, b: int, tol_bp: int = 300) -> bool:
    hi, lo = max(a, b), min(a, b)
    return any(abs(hi * 10_000 - lo * 10**k * 10_000) <= lo * 10**k * tol_bp for k in (1, 2, 3))

def evaluate(ctx: EvalContext) -> Evaluation: ...
# EvalContext: now, offer: Offer, origin: Literal["detail","search","lead"], segments: Sequence[Segment],
#   current_started_at, listing: ListingState, cat: CategoryParams, glob: GlobalParams,
#   nominal_interval: timedelta, watch: WatchTerms | None, peers: Sequence[int] | None,
#   lead_price: int | None, last_alert: AlertRecord | None, has_independent_route: bool
# Evaluation: verdict: AlertKind | None, labels: frozenset[AlertKind], flags: frozenset[Flag],
#   stats: HistoryStats, maturity: Maturity, confirmation: Literal["none","independent","persistence","forbidden"],
#   reject_code: str | None
def decide(ev: Evaluation, last: AlertRecord | None, rearm: RearmEvidence, listing: ListingState,
           now: datetime, cat: CategoryParams) -> Send | Skip: ...
```

### 3.11 Tabla de casos de prueba

Cada fila es una prueba unitaria parametrizada de `evaluate()` o `decide()`. Salvo nota, los segmentos están en stock, el vendedor es `f`, los precios en COP y las lecturas cada 50-90 min. `d−N` es `t − N días` y `t = 2026-10-13T15:00Z`.

| ID | Contexto | Serie (segmentos) | Lectura en `t` | Veredicto esperado |
|---|---|---|---|---|
| U01 | componentes, Éxito, seguimiento | `[d−90,d−21) 300.000`; `[d−21,t) 400.000 L=400.000` | P 300.000, L 400.000 | **NONE** + `pre_inflated`, `advertised_gap` |
| U02 | ídem | `[d−40,d−21) 300.000`; `[d−21,t) 400.000` | P 300.000 | **NONE** (R = M_prior = 300.000; F = 47,5 %) |
| U03 | ídem U01 | ídem U01 | P 255.000 | **HISTORIC_LOW** (S_real 1500 ≥ D_eff 500; 255.000 ≤ 294.000) + `pre_inflated`, etiqueta GOOD_DEAL |
| U04 | componentes, descubierto | `[d−20,t) 400.000` | P 300.000 | **NONE** (warm y descubierto) |
| U05 | ídem, seguimiento | `[d−20,t) 400.000` | P 300.000 | **GOOD_DEAL** + `short_history` (FP aceptado) |
| U06 | componentes | `[d−90,t) 279.900` sin L | P 279.900, L 499.900 | **NONE** + `inflated_list`, `advertised_gap` |
| U07 | tecnología, Amazon USD, seguimiento | `[d−90,t) 12.999¢`; el 2026-09-03: `13:12Z 12.999`, `15:41Z 8.999`, `17:58Z 12.999` | P 9.999¢ | **HISTORIC_LOW** + `flash_seen` (cota 4 h 46 min) |
| U08 | ídem, descubierto cada ~6 h | `09:30Z 12.999`, `15:41Z 8.999`, `21:50Z 12.999` | P 9.999¢ | **GOOD_DEAL** (8.999 entra en `m_robust`; F ≈ 0,3 %); exige ficha |
| U09 | tecnología, Panamericana | `[d−90,t) 2.299.000 L=2.499.000` | P 229.900, L 2.499.000; simulación 22.990.000 | **PRICE_ERROR** (`DECIMAL_SHIFT` confirmado por ruta independiente) |
| U10 | ídem | ídem | ídem; simulación 229.900.000 | **NONE** + `confirm_mismatch`; 229.900 queda `s` |
| U11 | ídem (`L_prev` = 2.499.000) | ídem | P 229.900, **L 249.900** | **NONE** `PARSE_SHIFT` (G5), no se persiste |
| U12 | tecnología, Amazon, página sin `twister-plus-price-data-price` | `[d−90,t) 12.999¢` | P 1.299¢ | **NONE**: `DECIMAL_SHIFT` sin ruta independiente queda `s` para siempre |
| U13 | Panamericana, 3 candidatos en el ciclo | R 2.299.000 / 1.599.000 / 899.000 | 229.900 / 159.900 / 89.900; la simulación devuelve los precios normales | **NONE ×3** + aviso de formato (G12 + mismatch) |
| U13b | Amazon, 2 candidatos, sin ruta independiente | R 12.999 / 8.999¢ | 1.299 / 899¢ | **NONE ×2** + aviso de formato |
| U14 | Amazon USD | cualquiera | «COP 520,000» ⇒ 52.000.000¢ | **ParseError** (G2/G3); no se persiste |
| U15 | componentes, búsqueda nueva | 12 pares, mediana 309.900 | P 39.900, L 49.900 | **NONE** (L < 0,6·mediana) |
| U16 | ídem | ídem | P 99.900, L 319.900; simulación confirma 99.900 | **PRICE_ERROR** + `short_history` |
| U17 | tecnología, `/seguir` con objetivo 1.000.000 | sin histórico | P 219.900, L 2.499.000; simulación 2.199.000 | **NONE** + anomalía (2P ≤ objetivo ⇒ confirmación fallida) |
| U18 | componentes | `[d−90,d−30) 300.000`; sin lecturas en `[d−30,t)` | P 240.000 | **HISTORIC_LOW** + `gap_before` (30 d) |
| U19 | `apply_sample`, Walmart, búsqueda de 12 h | 30 d a 19.900¢; huecos de 10,2 a 13,8 h más el retraso del cron | — | **1 segmento** (H = 18 h); 0 relámpagos |
| U20 | componentes | `[d−60,d−10) 500.000`; `[d−10,d−1) 350.000` sin stock | P 499.000 | **NONE** |
| U21 | ídem | ídem | P 350.000 con stock | **HISTORIC_LOW** (C = 50 d ⇒ D_eff 500; S_real 3000); etiqueta «mínimo en 50 días» |
| U22 | ídem | ídem | sin stock | **sin evaluación** |
| U23 | tecnología, Amazon | `[d−90,t) 12.999¢` vendedor `f` | P 8.999¢, vendedor `m` | **NONE** + `marketplace` (G14) |
| U24 | ídem, objetivo 10.000¢ | ídem | ídem | **TARGET_REACHED** + `marketplace` |
| U25 | componentes, VTEX por SKU | SKU 1 TB `[d−90,t) 400.000`; SKU 512 GB a 250.000 | 1 TB sin stock; 512 GB a 250.000 | 1 TB **sin evaluación**; 512 GB **NONE** (su propio histórico) |
| U26 | otros, Carulla | `[d−90,t) 25.000` con `un\|1` | P 12.500 con `g\|0.5` | **NONE** + `unit_changed` |
| U27 | componentes | días alternos (cambio 05:00Z) 300.000/240.000, 45 + 45 | P 240.000 | **NONE** (R = 240.000 por cuantil inferior) |
| U28 | componentes | ciclos de 26 h a 300.000 / 22 h a 240.000 durante 90 d | P 240.000 | **NONE** (R = 300.000, pero F = 46 % > 10 %) |
| U29 | tecnología | `[d−120,d−40) 4.000.000`, `[d−40,d−30) 3.920.000`, `[d−30,d−20) 3.840.000`, `[d−20,d−10) 3.760.000`, `[d−10,t) 3.680.000` | P 3.600.000 | **HISTORIC_LOW por tendencia**: R = 3.760.000, S_real 425 < D_eff 600; A = 4.000.000 ⇒ 3.600.000 ≤ 3.600.000 ✓; 3.600.000 ≤ 3.606.400 ✓ |
| U30 | ídem, evaluada un escalón antes (`t' = d−10`) | historia hasta `[d−20,d−10) 3.760.000` | P 3.680.000 | **NONE** (R = 3.840.000, S_real 416 < 600; A = 4.000.000 ⇒ 3.680.000 > 3.600.000) |
| U31 | tecnología | `[d−365,t) 4.999.000` | P 4.799.000 | **HISTORIC_LOW** (v = 0 ⇒ D_eff 300; S_real 400) |
| U32 | ídem | ídem | P 4.899.000 | **NONE** (S_real 200 < 300) |
| U33 | componentes | 79 d a 300.000 + 11 d a 255.000 en promociones de ≥ 6 h | P 255.000 | **NONE** (F = 12,2 % > 10 %; FN aceptado) |
| U34 | lead de Slickdeals «Samsung 990 EVO 1TB $74.99» → ASIN; búsqueda `slickdeals "990 evo"` | listing nuevo | ficha 7.499¢, en stock, `f` | **COMMUNITY_DEAL** + `short_history`, `community` |
| U34b | ídem | ídem | ficha 8.999¢ | **NONE** (lead vencido) |
| U35 | Olímpica, 2 candidatos, la simulación confirma ambos | R 1.899.000 / 2.499.000 | 189.900 / 249.900 | **PRICE_ERROR ×2** en un mensaje con `storm` |
| U36 | Walmart, sin ruta independiente en esa página | `[d−90,t) 22.900¢` | P 2.400¢ (patrón 24,00), 1 candidato | Ciclo 1: **NONE**, `s`, `pending_confirmation_at`. Ciclo 2 con 2.290¢: **NONE**, mismatch |
| U37 | `apply_sample` | segmento `g` de 8 d | mantenimiento | segmento pasa a `o` |
| D01 | `decide` | last = GOOD_DEAL en el mismo segmento | GOOD_DEAL | **SKIP** |
| D02 | `decide` | last = GOOD_DEAL hace 20 h | HISTORIC_LOW | **SEND** (escalamiento) |
| D03 | `decide`, componentes | last = GOOD_DEAL a 250.000 hace 30 h | GOOD_DEAL a 242.500 / a 245.000 | **SEND** (−3 %) / **SKIP** |
| D04 | `decide` | last hace 5 d con ref 300.000; después nunca ≥ 285.000 durante 24 h | GOOD_DEAL | **SKIP** (no rearmado) |
| D05 | `decide` | last = TARGET hace 2 h; `target_set_at` hace 1 h | TARGET | **SEND** |
| D06 | `decide` | `listing.muted_until` = now + 3 d | HISTORIC_LOW | registrar con `delivery='muted'` |

**Propiedades con Hypothesis:**
1. Partir un segmento `[a, b)` en dos con el mismo precio no cambia ningún estadístico.
2. `Q_q` pertenece al conjunto de precios y es monótona en `q`.
3. `R ≤ M90`.
4. Si P baja, `S_real` no baja y `F(P)` no sube.
5. Escalar todos los pesos por c > 0 no cambia `Q_q` ni `F`.
6. Ningún segmento con `status ∈ {s, g}` cambia `R`, `m_robust` ni `F`.
7. `Money` nunca mezcla monedas.
8. `is_power10_shift` es simétrica.

### 3.12 Ajustes respecto al algoritmo del crítico

1. **G12 tormenta.** El crítico definía «≥ 2 listings **o** ≥ 20 % de los evaluados». Con un solo candidato en una fuente con 5 o menos listings evaluados, el 20 % se cumple y se suprimiría un error de precio real, lo que contradice U09. La regla queda en «≥ 2 candidatos». Además, con ruta independiente los confirmados se envían con la bandera `storm` en lugar de suprimirse, porque dos parsers independientes rara vez fallan igual.
2. **U30.** El crítico daba «ancla 3.600.000». En `t' = d−10` todavía no existe esa alerta, así que el ancla es `Q50([t'−90, t'−60))` = 4.000.000. El veredicto (NONE) no cambia.
3. **G14 vendedor.** U23 esperaba NONE para un vendedor `m` en tecnología, pero solo `PRICE_ERROR` exigía `f`. Se hace explícito que con `u`, o con `m` no admitido, solo se permite `TARGET_REACHED`.
4. **`COMMUNITY_DEAL`.** Se agrega con rango 1 porque Slickdeals está en la v1 y el algoritmo final no lo contemplaba.
5. **`status 's'` al escribir.** Persistir el candidato como `s` en lugar de «no persistir hasta confirmar» hace el proceso resistente a caídas: el segmento nunca entra en E y la lectura siguiente lo cierra.
6. **Reclasificación de `g` a los 7 d.** Sin ella, un «error de precio» que la tienda mantiene, y que por lo tanto es el nuevo precio real, quedaría excluido para siempre y el listing quedaría ciego.
7. **`días`.** Se define sobre los intervalos cubiertos de los segmentos, porque no se guardan muestras individuales.
8. **`H` con seguimiento y búsqueda a la vez.** Se usa el intervalo nominal menor.

---

## 4. Fuentes, antibot y salud

### 4.1 Alcance de la v1

| code | Grupo de presupuesto | Endpoints | Cliente | Ruta independiente | Puerta de activación |
|---|---|---|---|---|---|
| exito | `grupo_exito` | `https://www.exito.com/io/api/catalog_system/pub/products/search?fq=skuId:…` y `?ft=<q>&_from=0&_to=49` (200 y 206 = éxito) | httpx | simulación VTEX | activa |
| carulla | `grupo_exito` | `https://www.carulla.com/api/catalog_system/pub/products/search…` | httpx | simulación | activa |
| olimpica | `olimpica` | ídem en `www.olimpica.com` | httpx | simulación | activa |
| panamericana | `panamericana` | ídem en `www.panamericana.com.co` | httpx | simulación | activa |
| amazon_us | `amazon_us` | `/dp/{ASIN}`, `/s?k=` | curl_cffi | `twister-plus-price-data-price` | activa con rampa 7 d |
| newegg | `newegg` | `/p/{item}`, `/p/pl?d=` | httpx | HTML frente a JSON-LD | activa con rampa 7 d |
| walmart_us | `walmart_us` | `/ip/{usItemId}`, `/search?q=` → `__NEXT_DATA__` | curl_cffi (plano 0/12) | JSON-LD | `enabled=false` hasta que pasen las fixtures |
| bestbuy_us | `bestbuy_us` | `/site/{slug}/{sku}.p?skuId={sku}&intl=nosplash`, `/site/searchpage.jsp?st=<q>&intl=nosplash` | curl_cffi (plano 0/12) | JSON-LD frente a HTML | `enabled=false` hasta que pasen las fixtures |
| slickdeals | `slickdeals` | RSS de portada `https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1` | httpx | — (feed) | activa |

**Puerta de activación para Walmart y Best Buy.** Ambas fuentes van en la v1 pero arrancan con `enabled=false`. Se activan con `/fuente on walmart_us` solo cuando se cumplen tres condiciones:
1. Las pruebas de contrato pasan con fixtures reales. Deben incluir una ficha con plan de protección o garantía que **debe** devolver el precio del producto y no el 24,00 del spike. En Best Buy, además, un lote de búsqueda que pruebe que el precio no es el 49,99 constante (G10).
2. El canario de la tienda está dentro de su rango durante 3 días.
3. La lectura independiente coincide con la principal en el 100 % de las fixtures.

Al activarse, `ramp_until = now + 7 d`.

Orden de preferencia para extraer datos: **API JSON**, luego **JSON embebido** (`__NEXT_DATA__`, JSON-LD) y al final **selectores CSS**.

### 4.2 Contrato del adaptador

El contrato está pensado para que Mercado Libre (OAuth con refresh rotativo), eBay (OAuth de cliente), Reddit (OAuth, feed), Alkosto y Falabella (POST JSON tipo Algolia) entren sin tocar el núcleo.

```python
# sources/base.py
class Capability(StrEnum):
    DETAIL = "detail"; SEARCH = "search"; CONFIRM = "confirm"; LEADS = "leads"; RESOLVE = "resolve"

class ExtractionPath(StrEnum):
    API_JSON = "api_json"; EMBEDDED_JSON = "embedded_json"; HTML = "html"

@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    code: str
    kind: Literal["store", "feed"]
    currency: Currency
    hosts: frozenset[str]                 # coincidencia EXACTA tras minúsculas + IDNA
    client: Literal["httpx", "curl"]
    capabilities: frozenset[Capability]
    max_batch: int                        # refs por petición en fetch_listings (VTEX 50 si M2 lo valida, si no 1)
    external_id_re: re.Pattern[str]       # p. ej. VTEX r"^\d{1,12}$", Amazon r"^[A-Z0-9]{10}$"
    auth: Literal["none", "api_key", "oauth_client", "oauth_refresh"] = "none"

class CredentialStore(Protocol):
    def bearer(self, provider: str) -> SecretStr: ...   # v1: NoCredentials → lanza CredentialUnavailable

@dataclass(frozen=True, slots=True)
class SourceContext:
    http: HttpClient                      # ya envuelto en PacedClient
    credentials: CredentialStore
    deadline_monotonic: float

@dataclass(frozen=True, slots=True)
class NeedsResolution:
    source: str
    pending_ref: str                      # 'slug:<slug>[|sku:<id>]' | 'short:<host>/<path>'

@dataclass(frozen=True, slots=True)
class ListingFound:  offer: Offer
@dataclass(frozen=True, slots=True)
class ListingGone:   ref: ListingRef
@dataclass(frozen=True, slots=True)
class ListingFailed: ref: ListingRef; error: SourceError
ListingOutcome = ListingFound | ListingGone | ListingFailed

@dataclass(frozen=True, slots=True)
class SearchPage:
    offers: Sequence[Offer]
    total_results: int | None

class Source(Protocol):
    descriptor: SourceDescriptor
    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None: ...          # puro
    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef: ...      # red
    def fetch_listings(self, ctx: SourceContext, refs: Sequence[ListingRef]) -> list[ListingOutcome]: ...
    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage: ...
    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None: ...    # None = sin ruta independiente
    def fetch_leads(self, ctx: SourceContext) -> list[Lead]: ...                # solo kind='feed'
    def detect_block(self, resp: HttpResponse) -> BlockKind | None: ...         # puro
```

```python
# domain/model.py
@dataclass(frozen=True, slots=True)
class Offer:
    ref: ListingRef                       # (source, external_id) a nivel de VARIANTE
    parent_external_id: str | None
    title: str
    price: Money | None                   # None = sin precio comprable (G8)
    list_price: Money | None
    in_stock: bool
    seller_class: SellerClass             # FIRST_PARTY 'f' | MARKETPLACE 'm' | UNKNOWN 'u'
    unit_key: str                         # 'un|1', 'g|0.5', 'pk|6'
    extraction: ExtractionPath
    observed_at: datetime

@dataclass(frozen=True, slots=True)
class Lead:
    source: str                           # 'slickdeals'
    title: str
    target_url: SafeUrl                   # URL de tienda ya validada contra la lista blanca
    price: Money | None
    published_at: datetime
```

**Reglas comunes a todos los adaptadores:**
- Los parsers son funciones **puras** a nivel de módulo, por ejemplo `parse_vtex_products(body: bytes, store: VtexStore) -> list[Offer]`, y se prueban con fixtures. Los métodos del adaptador son una capa delgada encima.
- Cada respuesta se modela con pydantic (`ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)`).
- El JSON se lee con `json.loads(body, parse_float=Decimal)`, así que **ningún precio pasa por float**. `to_minor(Decimal, currency)` usa `ROUND_HALF_UP`.
- Un `ValidationError` se convierte en `ParseError(code, loc)` con `e.errors(include_input=False, include_url=False)`.

**Reglas por tienda:**

- **VTEX:**
  - Un `Offer` por ítem (SKU).
  - El vendedor `sellerDefault=true` con id `"1"` da `f`; los demás, `m`.
  - El precio es `commertialOffer.Price`; `Price == 0` significa `price=None` y sin stock. El stock es `AvailableQuantity > 0`.
  - `L = máx(ListPrice, PriceWithoutDiscount)`, sujeto a G4.
  - `unit_key = measurementUnit|unitMultiplier`.
  - En búsquedas, como máximo 3 ítems en stock por producto.
  - Se ignoran los `PromotionTeasers` (precio con tarjeta).
  - Simulación: `{"items":[{"id":"<sku>","quantity":1,"seller":"<seller>"}],"country":"COL"}`. `sellingPrice` está en centavos y se divide entre 100 con `Decimal`.
- **Amazon:**
  - Solo el buybox (`#corePrice_feature_div .a-offscreen`).
  - El vendedor sale de «Ships from / Sold by»: Amazon.com da `f`.
  - Se descartan patrocinados, «More Buying Choices», «See options» y «used & new».
  - Cookies `i18n-prefs=USD` y `lc-main=en_US`.
  - En búsquedas, `in_stock` solo es verdadero si hay un nodo de precio principal, y todo veredicto exige leer la ficha.
- **Walmart:**
  - Ficha: `props.pageProps.initialData.data.product.priceInfo.currentPrice.price` y `availabilityStatus == "IN_STOCK"`.
  - Se excluyen los nodos `warranty` y `protection`.
  - `sellerName == "Walmart.com"` da `f`.
- **Newegg:** JSON-LD `Product.offers`; «Sold by Newegg» da `f`.
- **Best Buy:** JSON-LD `offers.price` y `availability`; siempre `f`.
- **Pack:** el título con `(\d{1,3})[- ]?(pack|count|ct|unidades|und)\b` da `unit_key = pk|N`.

### 4.3 Identidad, `/seguir` y búsquedas

| Fuente | `external_id` (variante) | URL canónica reconstruida | Desde la URL de David |
|---|---|---|---|
| VTEX ×4 | `itemId` `^\d{1,12}$`; `productId` en `parent_external_id` | `https://<host>/<linkText>/p` | slug `^[a-z0-9]+(?:-[a-z0-9]+)*$` (≤ 150) + `skuId` opcional ⇒ `NeedsResolution` |
| amazon_us | ASIN `^[A-Z0-9]{10}$` de `/(?:dp\|gp/product\|gp/aw/d)/` | `https://www.amazon.com/dp/<ASIN>` | directo; `a.co` y `amzn.to` ⇒ `short:` |
| walmart_us | `usItemId` `^\d{5,12}$` | `https://www.walmart.com/ip/<id>` | directo |
| newegg | Item# `^[A-Z0-9]{8,20}$` | `https://www.newegg.com/p/<item>` | directo |
| bestbuy_us | sku `^\d{6,8}$` | `https://www.bestbuy.com/site/<sku>.p?skuId=<sku>` | directo |

**`SafeUrl` (`domain/urls.py`, puro):**
- `urlsplit`, solo `https`, puerto vacío o 443.
- Sin userinfo (`https://www.exito.com@evil.com` se rechaza) y sin IP literal.
- Host en minúsculas y en IDNA, con **coincidencia exacta** contra `descriptor.hosts`.
- Se quitan `utm_*`, `ref`, `tag`, `psc`, `th`, `smid` y `_encoding`.
- **Nunca se pide la URL del usuario.** Solo la canónica, reconstruida desde el id validado.

**`/seguir` (I2):**
- El comando **no hace HTTP**. Crea un `watch` en estado `pending` con `pending_ref` (`id:<external_id>`, `slug:…` o `short:…`) y `next_check_at = now`.
- Si el listing ya existe porque lo descubrió una búsqueda, el `watch` nace `active` sobre ese listing y se **conserva su histórico**.
- El planificador resuelve el ref con el presupuesto de la fuente. Las redirecciones cortas se siguen **a mano**: máximo 3 saltos, validando cada `Location` contra la lista blanca, sin `follow_redirects`.
- La primera lectura crea el `listing`, activa el `watch` y encola la respuesta.
- Máximo 5 URLs por mensaje.

**Búsquedas:**
- La consulta va siempre como `params=`, nunca concatenada.
- Una página: VTEX `_from=0&_to=49`; Amazon, Newegg, Walmart y Best Buy, la primera página.
- Filtro puro `SearchSpec.accepts(offer)`:
  - el título normalizado contiene **todos** los tokens de la consulta. Normalizar es pasar a minúsculas, quitar tildes y unir número y unidad (`1 TB` → `1tb`);
  - no contiene ningún `exclude_terms`;
  - está dentro de `[min, max]` si se fijaron.
- `max` es un filtro, **no un objetivo**.
- Cada resultado aceptado hace upsert de su listing (la categoría es la de la búsqueda si el listing es nuevo) y de su segmento.

**Slickdeals:**
- `fetch_leads` baja el RSS cada 30 min y lo parsea con `defusedxml`.
- De cada ítem toma: título, precio (`\$([\d,]+(?:\.\d{2})?)` en el título), fecha y URL de tienda. La URL sale de un `href` directo del contenido o del parámetro `u2=` de los enlaces de redirección de Slickdeals, URL-decodificado **sin hacer ninguna petición**.
- Solo sobreviven los leads que cumplen tres condiciones:
  1. tienen menos de 24 h;
  2. coinciden con una `search` de `source='slickdeals'`;
  3. su URL resuelve sin red a `amazon_us`, `walmart_us` (si está activa), `newegg` o `bestbuy_us` (si está activa).
- Cada lead sobreviviente encola una lectura de ficha con prioridad de confirmación en la tienda de destino. Se omite si el listing se leyó hace menos de 6 h.
- Los demás leads se ignoran. No se guardan los leads.

### 4.4 Cliente HTTP

```python
@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    final_url: str
    headers: Mapping[str, str]
    body: bytes                          # ≤ 3 MB descomprimidos
    elapsed_ms: int

class HttpClient(Protocol):
    def request(self, method: Literal["GET", "POST"], url: str, *,
                params: Mapping[str, str] | None = None, json: Mapping[str, object] | None = None,
                headers: Mapping[str, str] | None = None,
                accept_status: frozenset[int] = frozenset({200}),
                content_types: frozenset[str] = frozenset({"application/json", "text/html"})) -> HttpResponse: ...
```

**Clientes:**
- **`HttpxClient`** usa `httpx.Client(timeout=Timeout(15, connect=5), follow_redirects=False, http2=False)`.
- **`CurlClient`** usa `curl_cffi.requests.Session(impersonate=<objetivo>, timeout=25)`.
- Se crea **una sesión por fuente y por ciclo**, con cookies aisladas y sin persistirlas, y se cierra al final.

**Topes en ambos clientes:**
- Lectura en streaming con un tope de **3 MB descomprimidos**, contados en el iterador. Esto cubre también las bombas gzip y brotli.
- **Plazo total de 25 s por petición**, verificado en cada fragmento contra `monotonic`.
- Se valida el `Content-Type`.
- `RecursionError` o `ValueError` de `json.loads` se convierten en `ParseError`.

**Huella coherente:**
- Con curl_cffi **nunca** se sobrescribe el User-Agent. La rotación consiste en elegir, por ciclo y por fuente, un objetivo de `impersonate` de la lista de config (`chrome131`, `chrome133a`, `chrome136`; una prueba de humo verifica que la versión fijada los soporte).
- Con httpx se usa un juego completo de cabeceras de Chrome reciente (UA, `sec-ch-ua`, `sec-ch-ua-platform`, `Accept`), fijo durante toda la ejecución.
- `Accept-Language`: `es-CO,es;q=0.9` en tiendas CO y `en-US,en;q=0.9` en tiendas US.
- **Tras un bloqueo no se cambia de huella.** Cambiarla es evasión y agrava el problema.
- Si una tienda VTEX empieza a bloquear httpx, se pasa a `client = "curl"` en el TOML.

**`PacedClient(inner, source, budget, sleep, monotonic, rng)` (Decorator):**
1. **Ritmo:** espera `min_gap × pace_multiplier + U(0, jitter)` desde el **fin** de la petición anterior a la misma fuente. Cada fuente arranca con un desfase de `U(0, 20) s`.
2. **Presupuesto:** cuenta **cada intento** (reintentos, bloqueos, confirmaciones). Al agotarse, lanza `BudgetExhausted`.
3. **Bloqueo:** si `detect_block` del adaptador o `blocks.common` devuelve algo, lanza `SourceBlocked`, sin reintento, y **la fuente se detiene el resto del ciclo**.
4. **Retry**, solo ante timeout, error de conexión, 500, 502, 503, 504 o 429 con `Retry-After ≤ 30 s`:
   - máximo 3 intentos;
   - espera `U(0, mín(20 s, 2 s·2^(n−1)))`, o `Retry-After + U(0, 3) s`;
   - no reintenta si `ahora + espera + 25 s` supera el plazo blando;
   - **nunca** reintenta un 403, un captcha, un 404 ni un `ParseError`.
5. **Clasificación del 404:** `ListingGone`, que no cuenta para el breaker.

**Marcadores de bloqueo (`blocks.py`):**
- status 403, o 429 repetido;
- `cf-mitigated: challenge`, `Just a moment`, `cf-chl-`;
- URL final con `/blocked`, o cuerpo con `px-captcha` o `Robot or human` (PerimeterX);
- `/errors/validateCaptcha`, `Enter the characters you see below` o `Robot Check` (Amazon);
- `captcha-delivery.com`;
- `Access Denied … Reference #` (Akamai);
- `account-verification`;
- 406 de VTEX Intelligent Search;
- `SOFT_EMPTY`: 200 sin los marcadores estructurales esperados, por ejemplo una ficha sin título.

### 4.5 Presupuesto y cadencia (`config/discountz.toml`)

> La columna «Seguimiento cada» se ajusta con §6.2: seguimientos VTEX en lote cada ciclo, watches con objetivo a la mitad del intervalo y listings calientes cada ciclo. Las prioridades de §6.2 sustituyen a las de esta sección.

| Grupo / fuente | Pausa mínima + jitter | Máx./ejecución | Máx./24 h | Seguimiento cada | Búsqueda cada |
|---|---|---|---|---|---|
| `grupo_exito` (exito + carulla) | 2 s + U(0, 2) | 30 | 1.500 | 60 min | 180 min |
| olimpica | 2 s + U(0, 2) | 20 | 800 | 60 min | 180 min |
| panamericana | 2 s + U(0, 2) | 20 | 800 | 60 min | 180 min |
| amazon_us | 6 s + U(0, 6) | 12 | 400 | 180 min | 360 min |
| newegg | 4 s + U(0, 4) | 12 | 400 | 180 min | 360 min |
| walmart_us | 8 s + U(0, 8) | 8 | 200 | 360 min | 720 min |
| bestbuy_us | 6 s + U(0, 6) | 8 | 200 | 360 min | 720 min |
| slickdeals | — | 1 | 60 | — | 30 min (RSS) |

**Reglas del plan:**
- **Rampa:** mientras `now < ramp_until`, los dos máximos se multiplican por 0,5.
- **Reserva de confirmación:** `máx(2, 20 %)` del máximo por ejecución.
- **Consumo de 24 h:** `Σ source_hourly_stat.requests` de las últimas 24 h, más los contadores del ciclo en curso.
- **`plan_run` (puro).** Por grupo con breaker cerrado, o semiabierto con 1 sonda, el orden es:
  1. confirmaciones pendientes (`pending_confirmation_at`);
  2. sonda canario o canario diario vencido;
  3. watches en `pending`;
  4. watches con objetivo;
  5. demás watches;
  6. búsquedas (si `discovery_paused` está apagado).
- Dentro de cada grupo se ordena por `overdue = (now − next_check_at) / intervalo`, de mayor a menor.
- Tras un retraso del cron **no hay ráfaga**: lo vencido se atiende por prioridad dentro del mismo presupuesto.
- **Siguiente lectura:** `next_check_at = now + intervalo × U(0,85; 1,15)`. Si falla, `now + mín(intervalo, 30 min × 2^(fallos−1))`. Si es 404, `now + 24 h`; al tercero, `watch.status='gone'`, `listing.gone_at` y aviso «parece retirado».
- **Saturación:** demanda = Σ (1 / intervalo); capacidad = máximo por ejecución × ciclos por hora observados (`run.gap_seconds`). Si la utilización supera el 100 % durante 3 ciclos, se avisa: «amazon_us saturada (140 %): sube el intervalo o reduce seguimientos».

**Capacidad resultante** (con ciclos reales cada 15-25 min, unos 3 por hora):
- VTEX: unas 60 lecturas por hora por tienda sin lotes. Si M2 valida los lotes `fq=skuId`, cientos.
- Amazon: unos 70 seguimientos a 3 h más 5 búsquedas.
- Newegg: unos 70.
- Walmart y Best Buy: unos 40 cada uno.

### 4.6 Circuit breaker (`domain/health.py` puro, estado en `source`)

```
closed --(1 bloqueo duro | 5 'unavailable' seguidos | 3 ParseError seguidos)--> open
open   --(now >= breaker_open_until)--> semiabierto: el plan asigna EXACTAMENTE 1 petición sonda (canario de la tienda)
sonda ok    --> closed; contadores a 0; si el disparo fue 'parse' → quarantine_cycles = 3
sonda falla --> open, trips + 1
breaker_open_until = now + mín(12 h, 30 min · 2^(trips−1)) · U(1,0; 1,1)
trips vuelve a 0 tras 24 h sin disparos. Cada transición se persiste EN EL MOMENTO (transacción de la tarea).
```

**Cuarentena** (`quarantine_cycles > 0`): se lee y se parsea, pero **no se escribe histórico ni se alerta**. Cada ciclo con 0 errores de parseo y canario en rango descuenta 1.

### 4.7 Salud y avisos

`classify_health` es pura y trabaja sobre las últimas 24 h de `source_hourly_stat`:

| Salud | Regla | Efecto |
|---|---|---|
| `broken` | Breaker abierto por `parse`, ≥ 3 ciclos con 2xx y 0 ofertas válidas (`SOFT_EMPTY`), una búsqueda con resultados previos que da 0 en 3 ciclos seguidos (`search.consecutive_empty ≥ 3`), o canario fuera de rango | Cuarentena; aviso **inmediato**: «walmart_us: posible cambio de formato» |
| `blocked` | Breaker abierto por bloqueo, o `blocked/requests ≥ 50 %` con ≥ 4 peticiones en 6 h | `pace_multiplier = 4`; aviso inmediato |
| `degraded` | Éxito < 80 % con ≥ 20 peticiones, ≥ 1 disparo en 24 h, o ≥ 3 `confirm_mismatch` | `pace_multiplier = 2`; aviso si se mantiene 2 h |
| `healthy` | Lo demás | Tras 24 h sana, el multiplicador se reduce a la mitad hasta llegar a 1. Aviso de «recuperada» si viene de `blocked` o `broken` |

**Histéresis:** se notifica solo si el estado se mantiene 2 ciclos y difiere de `notified_health`. Como mucho, un aviso por fuente cada 3 h, salvo la transición a `broken`. `dedup_key = health:<source>:<estado>:<fecha Bogotá>`.

Ejemplo de aviso:

> **amazon_us** empezó a bloquear (captcha) a las 14:20. Pausada hasta las 14:52. Último éxito: hoy 11:02. 18 seguimientos afectados.

**Canarios** (`config/canaries.toml`): 1 o 2 productos genéricos por tienda, con un rango de precio amplio (±60 %). Se leen una vez al día con el presupuesto del plan y sirven además de sonda del semiabierto. Su resultado no se persiste como histórico.

### 4.8 Cómo se agrega una tienda (y cómo entran los sub-proyectos sin tocar el núcleo)

1. Escribir `sources/<code>.py` con parsers puros y la clase que cumple `Source`. Si es otra tienda VTEX, basta una línea `VtexSource(...)` en el registro.
2. Grabar fixtures en local con `python -m discountz record-fixture --source <code> --kind search --query "<consulta genérica>"` (con *scrubber*, §7.3).
3. Registrarla en `SOURCES` y en el contrato `independence` de import-linter.
4. Agregar la sección `[sources.<code>]` al TOML y su canario.
5. Crear la migración `INSERT INTO source …` con `enabled=false`.

Cero cambios en `domain/` y `app/`. Los sub-proyectos usan:
- `auth="oauth_refresh"` y `CredentialStore`: Mercado Libre;
- `auth="oauth_client"`: eBay;
- `kind="feed"` y `fetch_leads`: Reddit;
- `HttpClient.request("POST", json=…)`: la API de listado de Alkosto y Falabella.

---

## 5. Telegram: alertas y comandos

### 5.1 Topología y seguridad

- **Chat privado** entre David y el bot en la v1. Los temas por categoría quedan para la v1.1.
- **Autorización:** solo se procesan updates con `from.id == TELEGRAM_OWNER_ID` **y** `chat.id == TELEGRAM_CHAT_ID`. En los botones se verifican `callback_query.from.id` y `callback_query.message.chat.id`.
- **Updates ajenos:** se descartan sin tocar la BD y el offset avanza en lote (§5.4).
- **BotFather:** `/setjoingroups` en *Disable*. El usuario del bot no se publica en el repo. Para desarrollo se usa **un token aparte**, porque un `getUpdates` local consumiría los updates de producción.
- **Configuración inicial:** `python -m discountz setup-bot` corre solo en local. Ejecuta `deleteWebhook`, sin el cual `getUpdates` da 409, y `setMyCommands`. Un 409 en el ciclo lanza `TelegramConflict`.
- **Logs:** el token aparece en la URL de la API, así que los loggers de `httpx` y `httpcore` quedan en `WARNING` y la redacción enmascara `bot\d+:[\w-]+`.

### 5.2 Formato de la alerta (`parse_mode=HTML`, `app/render.py`, puro)

- Los títulos se escapan con `html.escape(…, quote=True)` y se truncan a 120 caracteres.
- El texto visible tiene como máximo 4.096 caracteres.
- `link_preview_options={"is_disabled": true}`.
- Formato es-CO: `$1.299.900` para COP, `US$99,99` para USD, y porcentajes con un decimal truncado desde bp (`2307` → `23,0 %`).

Ejemplo, caso U07 (tienda de EE. UU.):

```html
<b>MÍNIMO EN 90 DÍAS</b> · #tecnologia · Amazon US
<b>Monitor 27" IPS 165 Hz</b>

<b>US$99,99</b>  antes <s>US$129,99</s>
Ahorro real: <b>23,0 %</b> frente a US$129,99 (precio habitual, mediana 90 d)
La tienda anuncia 33,3 % (tachado US$149,99): tachado nunca cobrado en 90 d
Histórico: mín US$129,99 · mediana 90 d US$129,99 · 90 días con datos
Relámpago visto: US$89,99 el 3-sep (duró máx. 4 h 46 min), excluido del mínimo
🇺🇸 Compra por casillero
<i>Visto 10:02 (hora Col.) · L1234</i>
```

**Encabezados por tipo:**

| Tipo | Encabezado |
|---|---|
| `price_error` | `POSIBLE ERROR DE PRECIO: verifica antes de pagar`, más «la tienda puede anular el pedido» |
| `historic_low` | `MÍNIMO EN 90 DÍAS`, o `MÍNIMO EN N DÍAS DE HISTÓRICO` si `C < 60 d` |
| `target_reached` | `PRECIO OBJETIVO ALCANZADO` |
| `good_deal` | `BUENA OFERTA` |
| `community_deal` | `OFERTA DE LA COMUNIDAD (Slickdeals), sin histórico propio suficiente` |

**Contenido:**
- «antes» es `listing.last_price_minor` antes de actualizarse. Se omite si no existe o es igual al actual.
- Se muestran el % real y el anunciado. Las banderas se muestran como líneas en cursiva.
- Los listings de tiendas de EE. UU. (USD) llevan la línea fija «🇺🇸 Compra por casillero». No se estima flete, impuestos ni TRM (decisión de David).

**Botones** (`inline_keyboard`; `callback_data` de 64 bytes o menos):

```json
{"inline_keyboard": [
  [{"text": "Abrir en Amazon US", "url": "https://www.amazon.com/dp/B0EXAMPLE1"}],
  [{"text": "Silenciar 30 d", "callback_data": "mute:L1234:30"},
   {"text": "Dejar de seguir", "callback_data": "unf:p12"}]
]}
```

Un listing descubierto lleva `{"text": "Seguir", "callback_data": "fol:L1234"}` en lugar de «Dejar de seguir».

Los botones de callback se procesan en el ciclo siguiente, entre 10 y 40 min después. `answerCallbackQuery` casi siempre responde «query is too old»: se ignora y el efecto se confirma con un mensaje.

**Resumen por categoría:** con 3 o más ítems, un solo mensaje silencioso:

```html
<b>#componentes · 4 buenas ofertas</b>
1. <a href="https://www.amazon.com/dp/B0EXAMPLE2">Samsung 990 EVO 1TB</a> · US$74,99 · real 18,0 % (anuncia 25,0 %) · ≈ $389.400 puesto
…
```

### 5.3 Comandos (`domain/commands.py`: `parse_command(text) -> Command | CommandRejected`)

| Comando | Ejemplo | Efecto |
|---|---|---|
| `/seguir <url…> [objetivo] [#cat]` | `/seguir https://a.co/d/xyz 120 #componentes` | Hasta 5 URLs, una por línea. Crea `watch`. La respuesta llega tras la primera lectura: «Siguiendo p12: SSD … · US$129,99 · en stock · alertas por histórico desde 3 días de cobertura» |
| `/buscar <fuente> <consulta> [-excluir…] [min:<p>] [max:<p>] [#cat]` | `/buscar exito ssd 1tb -carcasa -case max:400.000 #componentes` | Crea `search`. En `slickdeals` es un filtro de leads |
| `/lista [#cat\|fuente]` | `/lista #componentes` | Agrupada por categoría, 15 por mensaje: `p12 · SSD… · Éxito · $289.900 · obj $250.000 · ref $299.900`, y búsquedas `b3` |
| `/dejar <p12\|b3>` | `/dejar p12` | Borra el `watch` o la `search`. El listing y su histórico se conservan |
| `/objetivo <p12> <precio\|no>` | `/objetivo p12 280000` · `/objetivo p9 99,99` | El objetivo va en la moneda de la tienda. Fija `target_set_at` y fuerza la evaluación |
| `/categoria <p12\|L1234> <cat>` | `/categoria p12 tecnologia` | `listing.category` |
| `/silenciar <p12\|L1234\|todo> <días>` | `/silenciar todo 1` | `muted_until` (1 a 90 días) |
| `/fuente <on\|off> <code>` | `/fuente off amazon_us` | `source.enabled`; al activar, `ramp_until = now + 7 d` |
| `/estado` | — | Ver abajo |
| `/ayuda` | — | Sintaxis y aviso: «respondo en el siguiente ciclo (10-40 min)» |

**Precios** (`parse_price(raw, currency)`, cuando ya se conoce la fuente):
- **COP:** `^\$?\d{1,3}([.,]\d{3})+$|^\$?\d+$`. Sin decimales: el punto y la coma son separadores de miles.
- **USD:** `^(US)?\$?\d+([.,]\d{1,2})?$`. El último separador seguido de 1 o 2 dígitos es decimal.
- Cualquier otro formato da `CommandRejected("Formato de precio no válido. Ejemplos: 250.000 (COP) o 99,99 (USD).")`.
- La categoría por defecto es `otros`.

**`/estado`** (en `<pre>`):

```
discountZ · 13-oct 10:42
Último ciclo: hace 7 min (ok, 3 m 12 s) · intervalo entre ciclos p50 14 min / p95 31 min
Fuente        Salud      Éxito 24h  Uso 24h    Breaker
exito         sana       212/214    214/1500   cerrado
amazon_us     bloqueo     41/60      60/400    abierto hasta 11:30 (2.º)
walmart_us    apagada        —          —      —
BD 142 MB de 500 · Atrasados > 1 h: 3
```

### 5.4 getUpdates idempotente

```python
def process_updates(db: Db, bot: TelegramApi, now: datetime, deadline: float) -> int:
    offset = db.bot_state().telegram_offset + 1
    processed = 0
    while monotonic() < deadline:                        # tope de 20 s por pasada
        updates = bot.get_updates(offset=offset, timeout=0, limit=100,
                                  allowed_updates=("message", "callback_query"))
        if not updates:
            break
        for upd in updates:                              # ascendente por update_id
            if not is_owner(upd):                        # from.id y chat.id; sin tocar la BD
                offset = upd.update_id + 1
                continue
            try:
                with db.tx() as tx:                      # efecto + respuesta en outbox + offset
                    if tx.advance_offset(upd.update_id): # WHERE telegram_offset < $1
                        handle(tx, upd, now)             # CommandRejected → respuesta, misma transacción
            except Exception as exc:                     # update venenoso: no puede bloquear la cola
                with db.tx() as tx:
                    if tx.advance_offset(upd.update_id):
                        tx.enqueue_reply(upd, f"No pude procesar ese mensaje (ref U{upd.update_id}).")
                log.error("update_failed", update_id=upd.update_id, error=type(exc).__name__)
            offset = upd.update_id + 1
            processed += 1
    db.advance_offset_if_behind(offset - 1)              # updates ajenos al final del lote, un solo UPDATE
    return processed
```

**Garantías:**
- **Exactamente una vez en efecto.** Telegram reentrega todo lo que no se confirmó con un `offset` mayor. Con `WHERE telegram_offset < $1`, reaplicar es un no-op.
- **Comandos idempotentes por diseño**, como segunda red de seguridad: `watch_pending_uq` y `watch_listing_uq` responden «ya lo sigues (p12)», y `/objetivo` y `/silenciar` son asignaciones.
- **Respuesta al menos una vez** a través del outbox, con `dedup_key = reply:<update_id>`.
- **Riesgo aceptado:** Telegram guarda los updates solo 24 h. Una caída más larga los pierde; healthchecks avisa de la caída.

### 5.5 Envío y límites

- **Prioridades:** 0 `price_error`; 1 respuestas y `target_reached`; 2 `historic_low` y avisos de sistema; 3 `good_deal`, `community_deal` y resumen.
- **Ritmo:** 1,1 s entre mensajes y **15 por ciclo**. Lo que sobra queda `pending`.
- **Secuencia:** se confirma `status='sending'` con `sending_since=now` **antes** de llamar a la API; luego `sendMessage`; luego `sent`, `sent_at` y `telegram_message_id`. Una caída entre la API y el commit produce como mucho **un** duplicado (at-least-once, aceptado).
- **Caducidad** (`expires_at`): alertas a las 12 h, porque el precio ya es viejo; respuestas a las 24 h; sistema a las 48 h.

| Error | Acción |
|---|---|
| 429 | Espera `parameters.retry_after` si es ≤ 30 s y hay tiempo; si no, se detiene el drenado del ciclo |
| 400 «can't parse entities» | Se reenvía como texto plano (`html.unescape` sin etiquetas), se marca `sent` y se registra `render_bug` |
| 403 (bot bloqueado) | `failed`, ping `/fail` y exit 1 |
| 5xx o error de red | `attempts + 1` y queda `pending`; con ≥ 5 intentos pasa a `failed`. Una alerta `failed` **cuenta para el cooldown** |

**Resumen diario** (08:00, silencioso, funciona como latido):

> «discountZ: 92 ciclos en 24 h (último 07:58) · 6 alertas · exito sana, amazon_us 2 bloqueos (recuperada) · BD 41 MB de 500»

---

## 6. Frescura: disparo externo, prioridad y modo caliente

Objetivo de David: que las ofertas relámpago no se pierdan. Meta: una oferta que dura más de ~10 min se detecta casi siempre.

### 6.1 Disparo externo (ADR 024)

- **cron-job.org** (gratis, sin código) hace `POST https://api.github.com/repos/DavidRincon12/discountZ/actions/workflows/cycle.yml/dispatches` cada 5 min con `{"ref":"main"}`, cabeceras `Authorization: Bearer <token>`, `Accept: application/vnd.github+json` y `X-GitHub-Api-Version: 2022-11-28`.
- El token es **fine-grained**, solo para este repo, con permiso **Actions: read and write** y nada más; caduca a los 12 meses y se rota (recordatorio en el resumen diario 14 días antes, fecha en `bot_state.dispatch_token_expires_on`, que se fija con `/token_vence AAAA-MM-DD`).
- El `schedule` de GitHub queda como respaldo (`7,37 * * * *`). `concurrency` deja como mucho 1 ejecución en cola y el lease descarta solapes.
- `run.trigger_kind` distingue `workflow_dispatch` de `schedule`; el resumen diario muestra cuántos ciclos llegaron por cada vía (si el externo falla, se nota).
- Consumo: ~288 ciclos al día de ~3 min en un repo público (sin costo).

### 6.2 Prioridad y modo caliente (ADR 025)

- `listing.hot_until timestamptz`: se fija en `now + 2 h` cuando un listing seguido o descubierto **baja de precio** respecto al segmento anterior (en stock) o cuando una alerta queda pendiente de confirmar. Mientras `hot_until > now`, su intervalo efectivo es **1 ciclo**.
- Prioridades de `plan_run` (sustituye a la lista de §4.5): 1) confirmaciones pendientes; 2) sonda o canario; 3) watches en `pending`; 4) **listings calientes**; 5) watches con objetivo; 6) demás watches; 7) búsquedas y feeds.
- **VTEX en lotes:** los seguimientos VTEX se consultan con `fq=skuId:A&fq=skuId:B…` (hasta 50 por petición, confirmado en el mini-spike M2); con eso **todos los seguimientos VTEX se leen en cada ciclo** (intervalo de seguimiento VTEX = 1 ciclo).
- **Watches con objetivo** usan la mitad del intervalo de su fuente.
- **Calendario de eventos** (`[[events]]` en `config/discountz.toml`: nombre, fuentes, inicio y fin en hora de Bogotá; p. ej. Black Days, Cyber Lunes, Hot Sale, Prime Day, Black Friday, Cyber Monday): durante el evento el intervalo de las fuentes afectadas se divide entre 3 y el presupuesto por ejecución sube un 50 % sin pasar el máximo de 24 h.
- Todo ocurre **dentro** del presupuesto por fuente y del circuit breaker: el modo caliente nunca produce ráfagas.
- Bandera `hot` en la alerta cuando se detectó en una lectura de modo caliente (solo informativa).

### 6.3 Cambios al modelo de datos

```sql
ALTER TABLE listing ADD COLUMN hot_until timestamptz;           -- en 0001_init directamente
CREATE INDEX listing_hot_ix ON listing (source, hot_until) WHERE hot_until IS NOT NULL;
ALTER TABLE bot_state ADD COLUMN dispatch_token_expires_on date; -- en 0001_init directamente
```

(Se escriben dentro de `0001_init.up.sql`; aquí se listan aparte para que el cambio sea visible.)

---

## 7. Errores, seguridad, pruebas y despliegue

### 7.1 Jerarquía de errores

```python
class DiscountzError(Exception):
    """str(exc) NUNCA lleva URLs, títulos, consultas, precios ni secretos: se registra en logs públicos."""

# Dominio (puros)
class DomainError(DiscountzError): ...
class InvalidMoney(DomainError): ...
class CurrencyMismatch(DomainError): ...
class StaleObservation(DomainError): ...
class CommandRejected(DomainError):
    def __init__(self, user_message: str) -> None:
        super().__init__("command_rejected"); self.user_message = user_message
class UnsupportedUrl(CommandRejected): ...

# Fuentes
class SourceError(DiscountzError):
    kind: ClassVar[Literal["blocked", "unavailable", "parse", "gone", "budget"]]
    def __init__(self, source: str, code: str, http_status: int | None = None) -> None: ...
class SourceBlocked(SourceError):     kind = "blocked"      # sin retry; abre el breaker
class SourceUnavailable(SourceError): kind = "unavailable"  # retry; 5 seguidos abren el breaker
class ParseError(SourceError):        kind = "parse"        # sin retry; 3 seguidos abren el breaker
class ListingGoneError(SourceError):  kind = "gone"         # 404; no afecta el breaker
class BudgetExhausted(SourceError):   kind = "budget"
class CredentialUnavailable(SourceError): kind = "unavailable"

# Telegram
class FxRejected(FxError): ...
class TelegramError(DiscountzError): ...
class TelegramRetryAfter(TelegramError): retry_after: int
class TelegramBadRequest(TelegramError): ...
class TelegramForbidden(TelegramError): ...
class TelegramConflict(TelegramError): ...

# Infraestructura
class InfrastructureError(DiscountzError): ...
class ConfigError(InfrastructureError): ...         # exit 2
class DatabaseUnavailable(InfrastructureError): ... # exit 3
class SchemaMismatch(InfrastructureError): ...
class LeaseBusy(InfrastructureError): ...
```

Los adaptadores traducen en el borde, nunca más adentro, las excepciones de `httpx.HTTPError`, `curl_cffi.CurlError`, `psycopg.Error` y `pydantic.ValidationError`. De psycopg solo se registran `sqlstate`, `diag.constraint_name` y `diag.table_name`, **nunca `str(e)`**, porque `DETAIL: Failing row contains (…)` filtra filas completas.

### 7.2 Qué pasa cuando algo falla

| Falla | Alcance | Comportamiento | Exit |
|---|---|---|---|
| 404 de un listing | Ese listing | Reintento en 24 h; al tercero, `gone` y aviso | 0 |
| Timeout o 5xx tras 3 intentos | Esa tarea | Reintento en `mín(intervalo, 30 min·2^n)`; cuenta para el breaker | 0 |
| Bloqueo (403, captcha, challenge) | Esa fuente | Se detiene el resto del ciclo; breaker abierto; aviso | 0 |
| Cambio de formato | Esa fuente | `ParseError`, health `broken`, cuarentena, aviso | 0 |
| Excepción inesperada en una tarea | Esa tarea | Se captura en el borde de la tarea con el tipo y una traza redactada; run `partial` | 0 |
| Telegram caído | Envíos | El outbox queda `pending` | 0 |
| Telegram 403 | Envíos | `failed`, `/fail` | 1 |
| Supabase pausado o caído | Todo | exit 3; GitHub notifica el job fallido por correo; ping `/fail` si healthchecks está configurado | 3 |
| Esquema desfasado | Todo | `skipped_schema` y aviso de «migración pendiente» (1 al día) | 0 |
| Otro ciclo o una migración con el lease | Todo | `skipped_locked`. Con 3 seguidos: aviso y `pg_terminate_backend` de las sesiones `application_name LIKE 'discountz-%'` con `backend_start < now() − 16 min` | 0 / 1 |
| El job muere por timeout | Todo | Cada tarea ya estaba confirmada; el lease vence a los 16 min; el siguiente ciclo marca el `run` huérfano como `failed` | — |
| Workflows desactivados a los 60 días, cuenta suspendida, secreto caducado | Todo | Falta el resumen diario de las 08:00 (latido por defecto); con healthchecks configurado, aviso por Telegram y correo | — |

### 7.3 Seguridad (repo y logs públicos)

**1. Logs** (`infra/log.py`):
- Formato `key=value` con solo: evento, código de fuente, ids opacos (`L1234`, `p12`, `U<id>`), status HTTP, conteos, duraciones y códigos de error.
- **Prohibido** registrar URLs, títulos, consultas o precios.
- Un filtro redacta, como defensa adicional, `https?://\S+`, `bot\d+:[\w-]+` y `postgres(ql)?://\S+`.
- `sys.excepthook` imprime solo `type(e).__name__` y el código.
- Todos los modelos pydantic usan `hide_input_in_errors=True` y los secretos son `SecretStr`.
- Retención de logs y artefactos de Actions: **7 días**. Nunca `ACTIONS_STEP_DEBUG` ni artefactos en claro.

**2. Prueba de canarios** (`tests/integration/test_no_leaks.py`, con `capfd`, que captura stdout y stderr):
- siembra `ZZ_CANARIO_PRIVADO` en títulos, consultas y URLs de fixtures, y `postgresql://u:CANARYPASS@…`;
- fuerza un CHECK violado, un error de pydantic de configuración y un `ParseError`;
- verifica que ningún canario aparezca en la salida.

**3. Roles de BD** (runbook manual, una sola vez desde el editor SQL):

```sql
CREATE ROLE discountz_owner LOGIN PASSWORD '<32+ aleatorio>';      -- migrador: dueño del esquema, sin superusuario
ALTER SCHEMA discountz OWNER TO discountz_owner;
CREATE ROLE discountz_app LOGIN PASSWORD '<32+ aleatorio>';        -- ciclo: solo DML
ALTER ROLE discountz_app SET search_path = discountz;
ALTER ROLE discountz_app SET timezone = 'UTC';
ALTER ROLE discountz_app SET statement_timeout = '15s';
ALTER ROLE discountz_app SET lock_timeout = '5s';
ALTER ROLE discountz_app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE discountz_app SET idle_session_timeout = '20min';
ALTER ROLE discountz_app SET tcp_keepalives_idle = 30;
```

- Se desactiva la Data API del proyecto.
- URL de la app: `postgresql://discountz_app.<project_ref>:<pwd>@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=verify-full&sslrootcert=config/supabase-ca.crt`.

**4. Telegram:** §5.1.

**5. HTTP:** lista blanca de hosts exacta, redirecciones manuales validadas en cada salto, topes de 3 MB y 25 s, `defusedxml` para el RSS (§4.4).

**6. Fixtures sin datos personales:**
- `record-fixture` corre solo en local, con consultas genéricas.
- Un *scrubber* reemplaza IPv4 e IPv6, «Deliver to …», ciudades y `Set-Cookie`, y descarta las cabeceras de la petición.
- Los fixtures de Telegram son **sintéticos**.
- `tests/arch/test_fixtures_no_pii.py` rechaza en `tests/fixtures/**`: IPs, `680001`, «Bucaramanga», patrones de correo y cualquier entero igual a los ids de chat u owner de un archivo local `.pii-denylist` (ignorado por git; en CI solo se aplican los patrones genéricos).
- Los commits usan el correo `noreply` de GitHub.

**7. Ajustes del repositorio:**
- Acciones fijadas por SHA completo, con la política de GitHub que lo exige, y lista de acciones permitidas (las de GitHub y `astral-sh/setup-uv`).
- `GITHUB_TOKEN` en solo lectura por defecto; Actions no puede crear ni aprobar PR.
- «Require approval for all outside collaborators».
- Secret scanning con push protection; gitleaks en pre-commit.
- `CODEOWNERS` sobre `.github/`.
- Nunca `pull_request_target`.
- Dependabot con `cooldown` de 7 días y `[tool.uv] exclude-newer` móvil (se actualiza al renovar el lock).
- El repo vive en una **cuenta u organización dedicada**, para que una suspensión por los términos de uso de Actions no afecte la cuenta principal. Plan B documentado: el mismo código en una VM gratuita o en el equipo de David, con cron del sistema.

**8. Runbook de fuga:**
1. `gh run delete <id>`.
2. Rotar el secreto afectado (BotFather `/revoke`, `ALTER ROLE … PASSWORD`, regenerar el check de healthchecks).
3. Revisar `pg_stat_activity`.

### 7.4 Pruebas (TDD)

| Nivel | Qué cubre | Herramientas | Meta |
|---|---|---|---|
| Unitarias de dominio (`tests/unit`) | Todos los casos U01–U37 y D01–D06 de §3.11; cada guarda G1–G14 con un caso positivo y uno negativo; `apply_sample`; `parse_command` y `parse_price`; `SafeUrl` (userinfo, IDNA, IP, `..`); transiciones del breaker; `classify_health`; `plan_run` (presupuesto, grupo, rampa, reserva, sonda) | pytest + Hypothesis (§3.11), `now` fijo, `random.Random(42)` | < 3 s; cobertura de ramas ≥ 95 % en `domain/` |
| Render (`tests/unit`) | Archivos golden del HTML; ≤ 4.096 caracteres visibles con títulos hostiles (`<`, `&`, comillas) | pytest | — |
| Contrato de adaptadores (`tests/sources`) | Por fuente, como mínimo: búsqueda, ficha en stock, ficha agotada, vendedor de marketplace, variante y página de bloqueo (debe dar `detect_block`). Casos del spike: Éxito `/io` con 206, Walmart con plan de protección, Best Buy con precio constante (G10) y la simulación VTEX en centavos. Más el RSS de Slickdeals (con `u2=`) | `FakeHttp` (URL → fixture; una URL sin mapear falla la prueba); `.expected.json` con precios **exactos** | < 5 s |
| Política HTTP | Ritmo, presupuesto, reintentos con jitter, `Retry-After`, tope de 3 MB, plazo de 25 s, redirección a un host fuera de la lista blanca | Transporte falso y reloj falso (`sleep` avanza el reloj) | < 2 s |
| Integración (`-m integration`) | Migración up → down hasta 0 → up; CHECK y UNIQUE que deben fallar (segundo segmento abierto, watch sin resolver con `listing_id`); `ON DELETE SET NULL (segment_started_at)`; lease (2 conexiones, solo una gana); fencing tras la expiración; offset monótono; retención por lotes; `SOURCES` coincide con las filas de `source`; el dominio `category_slug` coincide con el enum `Category` y con el TOML; prueba de canarios en logs (§7.3) | Postgres 17 en CI | ~30 s |
| E2E (`-m e2e`) | Dos `run_cycle` seguidos con fuentes servidas desde fixtures y Telegram falso: el segundo no duplica alertas ni mensajes y el offset avanza. Caída simulada entre el commit y el envío. Update venenoso | pytest + `pytest-httpserver` | ~10 s |
| Arquitectura | import-linter (capas, dominio puro, independencia) + ruff `TID251` + anti-PII en fixtures | — | < 2 s |

Cobertura global ≥ 85 %. `mypy --strict`, `ruff check` (con `DTZ`, `S`, `B`, `UP`, `PL`, `TID`) y `ruff format --check` corren en pre-commit y en CI.

### 7.5 Workflows de GitHub Actions

Todas las acciones van fijadas por SHA completo (`<sha40>` se fija al implementar y Dependabot lo mantiene), con `permissions` mínimos.

`.github/workflows/cycle.yml`:

```yaml
name: cycle
on:
  schedule:
    - cron: '7,37 * * * *'           # respaldo cada 30 min; el disparo principal es externo (§6.1)
  workflow_dispatch:                 # cron-job.org lo llama cada 5 min
permissions:
  contents: read
concurrency:
  group: discountz-cycle
  cancel-in-progress: false          # nunca matar un ciclo a mitad; GitHub deja 1 pendiente
jobs:
  cycle:
    runs-on: ubuntu-24.04
    timeout-minutes: 15              # plazo blando interno 480 s, duro 12 min; lease TTL 16 min
    steps:
      - uses: actions/checkout@<sha40>          # v5
        with:
          persist-credentials: false
          fetch-depth: 1
      - uses: astral-sh/setup-uv@<sha40>        # v6
        with:
          version-file: pyproject.toml          # [tool.uv] required-version = "==<x.y.z>"
          python-version: '3.13'
          enable-cache: true
          cache-dependency-glob: uv.lock
      - run: uv sync --locked --no-dev
      - name: Ciclo
        run: uv run --no-sync python -m discountz run
        env:
          DISCOUNTZ_DATABASE_URL: ${{ secrets.DATABASE_URL }}
          DISCOUNTZ_TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          DISCOUNTZ_TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          DISCOUNTZ_TELEGRAM_OWNER_ID: ${{ secrets.TELEGRAM_OWNER_ID }}
          DISCOUNTZ_HEALTHCHECKS_URL: ${{ secrets.HEALTHCHECKS_PING_URL }}
          DISCOUNTZ_GH_RUN_ID: ${{ github.run_id }}
          DISCOUNTZ_GH_RUN_ATTEMPT: ${{ github.run_attempt }}
          DISCOUNTZ_TRIGGER: ${{ github.event_name }}
          DISCOUNTZ_SOFT_DEADLINE_S: '480'
```

`.github/workflows/keepalive.yml`. Va en su propio archivo para que GitHub no lo desactive junto con `cycle`:

```yaml
name: keepalive
on:
  schedule:
    - cron: '41 13 * * 1'            # lunes 08:41 en Bogotá
  workflow_dispatch:
permissions:
  actions: write
jobs:
  keepalive:
    runs-on: ubuntu-24.04
    timeout-minutes: 3
    steps:
      - name: Reactivar workflows programados
        env:
          GH_TOKEN: ${{ github.token }}
          REPO: ${{ github.repository }}
        run: |
          for wf in cycle.yml keepalive.yml; do
            gh api --method PUT "repos/$REPO/actions/workflows/$wf/enable"
          done
```

No hay commits automáticos a `main`. La red ante los 60 días es el resumen diario (y healthchecks.io si se configura), y el mini-spike M6 verifica si `enable` reinicia el contador.

`.github/workflows/migrate.yml`. Es manual, con aprobación, y es **el único** que aplica DDL:

```yaml
name: migrate
on:
  workflow_dispatch:
    inputs:
      to:
        description: 'Versión destino (número entero; "head" = última)'
        required: true
        default: 'head'
permissions:
  contents: read
concurrency:
  group: discountz-migrate
  cancel-in-progress: false
jobs:
  migrate:
    runs-on: ubuntu-24.04
    timeout-minutes: 25
    environment: production-db       # revisor obligatorio: David aprueba cada migración
    steps:
      - uses: actions/checkout@<sha40>
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@<sha40>
        with: { version-file: pyproject.toml, python-version: '3.13', enable-cache: true }
      - run: uv sync --locked --no-dev
      - run: uv run --no-sync python -m discountz migrate --to "${{ inputs.to }}" --wait-lease 20m
        env:
          DISCOUNTZ_MIGRATOR_DATABASE_URL: ${{ secrets.DATABASE_URL_MIGRATOR }}   # secreto de ENVIRONMENT
```

`.github/workflows/ci.yml` (sin secretos):

```yaml
name: ci
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
jobs:
  quality:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@<sha40>
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@<sha40>
        with: { version-file: pyproject.toml, python-version: '3.13', enable-cache: true }
      - run: uv sync --locked
      - run: uv run --no-sync ruff format --check . && uv run --no-sync ruff check .
      - run: uv run --no-sync mypy --strict src tests
      - run: uv run --no-sync lint-imports
      - run: uv run --no-sync pytest -m "not integration and not e2e" --cov=discountz --cov-branch --cov-fail-under=85
  integration:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    services:
      postgres:
        image: postgres:17@sha256:<digest>
        env: { POSTGRES_PASSWORD: postgres }
        ports: ['5432:5432']
        options: >-
          --health-cmd "pg_isready -U postgres" --health-interval 5s
          --health-timeout 5s --health-retries 10
    env:
      DISCOUNTZ_MIGRATOR_DATABASE_URL: postgresql://postgres:postgres@localhost:5432/postgres
      DISCOUNTZ_DATABASE_URL: postgresql://postgres:postgres@localhost:5432/postgres
    steps:
      - uses: actions/checkout@<sha40>
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@<sha40>
        with: { version-file: pyproject.toml, python-version: '3.13', enable-cache: true }
      - run: uv sync --locked
      - run: |
          uv run --no-sync python -m discountz migrate --to head
          uv run --no-sync python -m discountz migrate --to 0
          uv run --no-sync python -m discountz migrate --to head
      - run: uv run --no-sync pytest -m "integration or e2e"
```

`main` queda protegido exigiendo `ci/quality` y `ci/integration`.

**Secretos:**

| Secreto | Alcance | Usado en | Rotación |
|---|---|---|---|
| `DATABASE_URL` (rol `discountz_app`) | repositorio | cycle | `ALTER ROLE discountz_app PASSWORD` y actualizar |
| `TELEGRAM_BOT_TOKEN` | repositorio | cycle | BotFather `/revoke` |
| `TELEGRAM_CHAT_ID`, `TELEGRAM_OWNER_ID` | repositorio | cycle | — |
| `HEALTHCHECKS_PING_URL` (opcional) | repositorio | cycle | Regenerar el check |
| `DATABASE_URL_MIGRATOR` (rol `discountz_owner`) | environment `production-db` (revisor: David) | migrate | `ALTER ROLE` |
| Futuros: `DISCOUNTZ_TOKEN_KEY` (ML), `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET`, `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET`, `BESTBUY_API_KEY` | repositorio | cycle | Por sub-proyecto |

En local: `.env` en `.gitignore` y `.env.example` con nombres pero sin valores.

**healthchecks.io (opcional, plan gratuito):** periodo de 10 min, gracia de 60 min e integraciones con Telegram y correo. Pings `/start`, normal y `/fail`. Cubre la desactivación a los 60 días, la pausa de Supabase, un secreto caducado y una cuenta suspendida.

### 7.6 Migraciones y despliegue

**Ejecutor propio** (`infra/migrate.py`, unas 80 líneas):
- Archivos `NNNN_nombre.up.sql` y `.down.sql`.
- Cada migración corre en su propia transacción, bajo el **lease** (`holder='migrate:<run_id>'`), esperando hasta `--wait-lease`.
- Guarda el sha256 y **falla si se edita una migración ya aplicada**.
- `--to N` aplica o revierte en orden.
- Fija `lock_timeout='5s'` y `statement_timeout='60s'`.

**Expand/contract** para todo cambio destructivo:
1. Columna nula.
2. Backfill por lotes en `maintenance`.
3. Código que la usa.
4. `SET NOT NULL`.
5. Borrado de lo viejo en un release posterior.

`SCHEMA_VERSION` en el código. El ciclo **no migra** y se salta con aviso si la versión no coincide.

**Despliegue:**
1. PR con su CI en verde.
2. Si hay migración expand, se ejecuta `migrate` con aprobación **antes** del merge.
3. Merge a `main`.
4. El siguiente ciclo usa el código nuevo.

**Rollback:** `migrate --to N-1` aprobado y revert del commit. Como el ciclo no automigra, no la vuelve a aplicar.

**Orden de implementación y mini-spikes:**

| # | Paso | Criterio de salida |
|---|---|---|
| 1 | Tooling (uv, ruff, mypy, import-linter, pre-commit, gitleaks) y dominio por TDD: `money`, `segments`, `stats`, `guards`, `evaluate` (§3.11), `decide`, `commands`, `urls`, `health`, `scheduling` | Todos los casos de §3.11 en verde |
| 2 | M5: `select version()` ≥ 15 en Supabase y tamaño base de un proyecto vacío | Versión confirmada; base medida |
| 3 | Migración 0001, roles, `infra/db.py`, lease con TTL, integración en CI | up/down/up en verde |
| 4 | VTEX Éxito con fixtures; M2: ¿varios `fq=skuId:` en una petición son OR? (criterio: 50 SKU en < 3 s → `max_batch=50`, si no 1); M3: ¿la simulación VTEX responde anónima desde Actions en las 4 tiendas sin `postalCode`? (≥ 11/12 coherentes; si exige código postal se agrega el secreto `DISCOUNTZ_POSTAL_CODE`; si falla, esa tienda no tiene ruta independiente) | Fixtures + `.expected.json` |
| 5 | Outbox, render, `getUpdates`, comandos, `cycle.yml`, disparo externo (§7.7), healthchecks opcional, `keepalive.yml`, `migrate.yml`. Puesta en marcha con 5-10 seguimientos | 48 h sin `/fail` |
| 6 | Amazon y Newegg con rampa; M4: ¿Amazon sostiene 12 peticiones por ciclo durante 48 h con < 10 % de captchas? (si no, se baja a 6) | Tasa medida |
| 7 | Slickdeals; Walmart y Best Buy con su puerta de activación (§4.1) | Puerta cumplida |
| 8 | M6: verificar en un repo de prueba si `PUT …/enable` reinicia el contador de 60 días | Documentado en el ADR |
| 9 | Revisión a las 2 semanas: tasas por fuente, p95 del intervalo entre ciclos, MB por semana y alertas descartadas a mano | Ajuste de presupuestos y umbrales |

---

## 8. Fuera de la v1 y sub-proyectos

### 8.1 Sub-proyectos de fuentes

Ninguno toca `domain/` ni `app/`; el contrato está en §4.2.

| Sub-proyecto | Qué hace falta | Criterio de entrada | Uso del contrato |
|---|---|---|---|
| **SP-1 Mercado Libre** | App OAuth gratuita; mini-spike del refresh token de un solo uso que rota en cada refresh (~6 meses). Migración `oauth_credential`; `DbCredentialStore` en `infra/credentials.py`; comando local `python -m discountz ml-auth` | v1 estable 4 semanas | `auth="oauth_refresh"`, `CredentialStore.bearer("mercadolibre")` |
| **SP-2 Alkosto / Ktronix** | Descubrir su JSON interno (API de listado o Algolia), grabar fixtures, regex de ids | Spike con ≥ 11/12 desde Actions y precio real (no el umbral de envío gratis) | `HttpClient.request("POST", json=…)` si es Algolia; grupo de presupuesto compartido (misma empresa) |
| **SP-3 Falabella / Homecenter** | Ídem. Falabella dio 403 de Cloudflare con impersonación: `client="httpx"` | Ídem | Ídem; grupo compartido |
| **SP-4 eBay** | Browse API (requiere aprobación de la cuenta de desarrollador); token de cliente cacheado en memoria por ciclo | Aprobación concedida | `auth="oauth_client"`; `seller_class` según `seller.feedbackScore` |
| **SP-6 Canales de Telegram** | Adaptador `kind="feed"` sobre `https://t.me/s/{canal}` (HTML público); canales `DescuentosTech`, `Clubgratis`, `cajitatech` en la tabla `search` (privada); extracción de URL de tienda (resolviendo acortadores `amzn.to`, `a.co`, `bit.ly`, `meli.la` con lista blanca) y precio del texto | Fixtures reales de los 3 canales con ≥ 20 publicaciones cada uno | Leads → misma ruta que Slickdeals; si la tienda no es legible (p. ej. Mercado Libre), alerta `community_deal` con bandera `unverified` |
| **SP-5 Reddit** | OAuth (script app); subreddits de ofertas como feed | v1 + SP-1 (reutiliza el `CredentialStore`) | `kind="feed"`, `fetch_leads` → mismo flujo de leads que Slickdeals |

**Decisión ya tomada para SP-1** (crítica I8):
- El token **no** va en GitHub Secrets: actualizarlo desde Actions exigiría un PAT con `secrets:write`, que podría reescribir todos los secretos.
- Va en esta tabla:

```sql
CREATE TABLE oauth_credential (
  provider          text PRIMARY KEY CHECK (provider ~ '^[a-z_]{2,32}$'),
  refresh_ct        bytea NOT NULL,
  access_ct         bytea,
  nonce_refresh     bytea NOT NULL CHECK (octet_length(nonce_refresh) = 12),
  nonce_access      bytea CHECK (octet_length(nonce_access) = 12),
  key_version       smallint NOT NULL CHECK (key_version >= 1),
  access_expires_at timestamptz,
  rotation_seq      bigint NOT NULL DEFAULT 0 CHECK (rotation_seq >= 0),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
```

- Cifrado AES-256-GCM (`cryptography`), con la clave en `DISCOUNTZ_TOKEN_KEY` y AAD = `provider‖rotation_seq`.
- El refresco ocurre solo bajo el lease, con `SELECT … FOR UPDATE`, y solo si el access token vence en menos de 30 min.
- **Commit inmediato** del token nuevo, con 3 reintentos de escritura, antes de cualquier otro trabajo.
- Si no se puede persistir: aviso «re-autorizar Mercado Libre».
- La respuesta nunca se registra. Al leer el token se usa `::add-mask::`.

### 8.2 v1.1 (mejoras con disparador)

| Elemento | Disparador |
|---|---|
| Supergrupo con temas por categoría (`/vincular`, `message_thread_id`) | David lo pide, o más de 10 alertas al día |
| `/historial <id>` (mín/mediana/máx a 30/90/365 d, últimos 8 cambios, sparkline) | Primer mes de uso |
| `/pausar`, cadencia por seguimiento | Saturación persistente (§4.5) |
| Best Buy por API oficial con llave gratuita (`BESTBUY_API_KEY`) | Si la puerta HTML no se cumple o el HTML empieza a bloquear |
| Modo de referencia `recent` por categoría (contra los falsos negativos en mercados al alza) | Más de 3 falsos negativos reportados en una categoría |
| Agrupar variantes en un mensaje (`parent_external_id`) | Más de 2 alertas del mismo producto padre en un ciclo |
| Resumir `price_segment` de más de 400 d a mínimos semanales | BD de 300 MB o más |

### 8.3 Fuera de alcance

**Sin fecha:**
- Emparejar el mismo producto entre tiendas (GTIN o EAN) y comparación cruzada.
- Relleno de histórico externo (Keepa y CamelCamelCamel son de pago).
- Precios con tarjeta (Éxito, Falabella), cupones, Prime, Subscribe & Save y cashback como precio efectivo.
- Peso volumétrico, consolidación de varios artículos en un envío, flete nacional en tiendas CO.
- Alertas de reposición de stock, gráficas en imagen, panel web.
- Decaimiento exponencial, modelos estacionales o de ML.
- Concurrencia con hilos o asyncio (solo si el p95 del ciclo pasa de 5 min).
- B&H y Micro Center (Cloudflare 0-4/12).
- Guardar cuerpos de respuestas fallidas (`fetch_failure_sample`): riesgo de datos personales; se usa `record-fixture` en local.

**Nunca:**
- navegador headless;
- proxies de pago;
- resolver captchas o evadir un login;
- servidor de webhooks o long polling residente;
- self-hosted runners (peligrosos en un repo público);
- multiusuario.

### 8.4 Riesgos residuales aceptados

1. **Relámpagos más cortos que la cadencia efectiva** (10-40 min más el intervalo): se pierden.
2. **Producto descubierto durante una inflación sin histórico previo:** un seguimiento en `warm` puede dar `GOOD_DEAL` con `short_history` (U05).
3. **Promociones recurrentes** que ocupan más del 10 % del tiempo no se avisan, salvo con objetivo (U33).
4. **`R = mín(…)`** da falsos negativos en mercados al alza.
5. **Una tienda sin ruta independiente** nunca confirma un patrón 10^k (U12), y en arranque en frío un error de parseo persistente puede disparar un `TARGET_REACHED` con `persistence_only`.
6. **IPs de Azure:** cualquier tienda puede volverse inviable sin proxies.
7. **Términos de uso** de GitHub Actions y de las tiendas.
8. **Normativa aduanera y sales tax:** pueden cambiar; todo es configurable y se marca como estimado.
9. **Duplicado de un mensaje** si el proceso cae entre la API de Telegram y el commit.
10. **Comandos perdidos** tras más de 24 h de caída.

---

## 9. Decisiones para ADR

| ADR | Decisión | Alternativas descartadas | Motivo |
|---|---|---|---|
| 001 | Monolito hexagonal-lite **síncrono** con turnos rotativos por fuente | asyncio + TaskGroup (A, O) | Unas 110 peticiones con pausas de 2-12 s caben en 2-4 min en un hilo; asyncio agrega cancelación entre tareas hermanas y una conexión compartida con candado |
| 002 | Histórico en **segmentos** (`price_segment`) con `status o/s/g`, `seller_class` y `unit_key`; PK `(listing_id, started_at)`; un solo segmento abierto garantizado por índice | Mínimo diario (S); una fila por lectura; `id` sustituto + UNIQUE (O) | Ponderar por tiempo, distinguir relámpagos y marcar glitches; unas 10 veces menos filas que por lectura; un índice menos en la tabla grande |
| 003 | `R = mín(M90, M30, M_prior)` con cuantil inferior ponderado por segundos; rareza `F(P) ≤ F_max`; umbrales adaptativos a la volatilidad con ≥ 60 d; ancla de tendencia | Mediana con decaimiento (O); `P ≤ P10` (S); mínimo diario | Resiste inflación previa (U01-U02), oscilación (U27-U28) y escaleras (U29), y detecta rebajas pequeñas en productos estables (U31) |
| 004 | **Confirmación independiente** (otro parser u otro endpoint) frente a **persistencia**; el patrón 10^k nunca alerta sin ruta independiente; tormenta con ≥ 2 candidatos | Releer la misma ficha (S, O); freno con ≥ 5 y > 30 % (S) | Releer con el mismo parser confirma el mismo error de parseo (U12); umbral corregido según §3.12 |
| 005 | Exclusión mutua con **lease con TTL de 16 min** más `concurrency` sin cancelar; plazo duro interno de 12 min | `pg_try_advisory_lock` (S, A); fencing por `epoch` (recortado por David: el plazo duro < TTL hace imposible el proceso zombi) | Un candado huérfano tras matar el job deja el sistema mudo |
| 006 | Ejecutor de migraciones propio (SQL up/down + sha256), ejecutado **solo** por `migrate.yml` con aprobación; el ciclo solo verifica la versión | Alembic (arrastra SQLAlchemy); automigrar en cada ciclo (S) | Cada ciclo correría con permisos de DDL, y un rollback se re-aplicaría |
| 007 | **Outbox único** para todo mensaje + `alert` como hecho de dominio con `UNIQUE (listing_id, segment_started_at, kind)` | `alerts` como outbox y respuestas directas (S); `dedup_key` duplicado (A) | Efecto exactamente una vez, envío al menos una vez; la retención no borra la memoria del cooldown |
| 008 | `getUpdates` con offset en la misma transacción que el efecto; update venenoso en una segunda transacción; ajenos descartados sin BD; autorización por `from.id` y `chat.id` | Solo `chat.id` (S); sin manejo de veneno (S, A) | Ni cola bloqueada ni DoS por inundación |
| 009 | Supavisor en **modo sesión** (`:5432`, IPv4) con `verify-full`; roles `discountz_app` (DML) y `discountz_owner` (DDL, secreto de environment con revisor); timeouts en el rol | Conexión directa (solo IPv6); modo transacción `:6543`; `postgres` para todo | Los runners no tienen IPv6; mínimo privilegio; ninguna sesión colgada |
| 010 | Esquema propio `discountz`, Data API apagada, `REVOKE` a `anon` y `authenticated` | `public` | No exponer la lista por PostgREST |
| 011 | Categorías como `DOMAIN` con CHECK y parámetros en el TOML público | Tabla `category` (A, O) | Cambia rara vez; una migración basta; menos sincronización |
| 012 | Identidad a nivel de **variante**; URL canónica reconstruida desde un id validado con regex por fuente; `SafeUrl` con host exacto; redirecciones manuales | Slug de producto (S); `"exito.com" in url` | Evita FP entre SKU (U25), SSRF e inyección de ruta |
| 013 | Contrato `Source` con `SourceContext` (HTTP con presupuesto + `CredentialStore`), `kind` store/feed, `confirm()` y `fetch_leads()` | `plan_*`/`parse` puros obligatorios (A); `async` (O) | ML, eBay, Reddit y Alkosto/Falabella entran sin tocar el núcleo |
| 014 | Circuit breaker **persistido** en `source`: bloqueo duro abre de inmediato; `pace_multiplier`; cuarentena de 3 ciclos tras un fallo de parseo; canarios genéricos como sonda | Breaker en memoria; rotar la huella tras un bloqueo | Procesos efímeros; rotar la huella es evasión y agrava el bloqueo |
| 015 | Presupuestos por **grupo de infraestructura** (Éxito + Carulla), por ejecución y por 24 h, con rampa al 50 % durante 7 d | Por tienda; sin rampa | Comparten WAF; el volumen sostenido de Amazon y Walmart no está medido |
| 016 | **healthchecks.io** como detector de inactividad; `keepalive.yml` aparte solo con `actions: write`; sin commits automáticos a `main` | Resumen diario como latido (S); commit vacío con bypass de protección (A) | Nadie nota bien una ausencia; un bot con `contents: write` en `main` es un riesgo |
| 017 | Logs públicos: lista blanca de campos, `hide_input_in_errors`, de psycopg solo `sqlstate`, `excepthook` mínimo, prueba con canarios usando `capfd`, retención de 7 d | Solo redactar URLs (S, A, O) | pydantic y psycopg filtran secretos y filas en los mensajes de excepción |
| 018 | Walmart y Best Buy (HTML) en la v1 detrás de una **puerta de activación** (fixtures + canario + coincidencia de la ruta independiente) | Diferirlas a la v1.1 (S); activarlas sin validar | El spike mostró extracciones dudosas (24,00 y 49,99) |
| 019 | Slickdeals como `feed`: leads filtrados por búsquedas de David, URL de tienda sin resolver redirecciones (`u2=`), confirmación con la ficha propia; veredicto `COMMUNITY_DEAL` | Alertar el RSS directamente; resolver redirecciones | Ninguna alerta sin precio propio leído; sin peticiones extra |
| 020 | Tiendas de EE. UU. marcadas solo como «🇺🇸 Compra por casillero»; **sin** costo puesto, TRM ni objetivo en COP | Costo puesto informativo con TRM (propuesta del panel) | Decisión de David: el cálculo de flete e impuestos no le aporta y agrega una dependencia (TRM) |
| 021 | **Sin respaldo en la v1**; entra en v1.1 si el histórico gana valor | `pg_dump` cifrado con `age` a un repo privado | Perder el histórico solo retrasa alertas unas semanas (YAGNI) |
| 022 | Token rotativo de Mercado Libre cifrado con AES-GCM en la BD, refresco solo bajo el lease y commit inmediato | GitHub Secrets actualizados con un PAT | Un PAT con `secrets:write` puede reescribir todos los secretos |
| 023 | Repo público **en la cuenta de David** (portafolio); plan B: mover el ciclo a una máquina local o VM sin tocar el código | Cuenta u organización dedicada | Lo peor probable es que GitHub desactive Actions en este repo, no la cuenta |
| 024 | **Disparo externo** cada 5 min (cron-job.org → `workflow_dispatch` con token fine-grained solo `actions: write` sobre este repo) + `schedule` de respaldo cada 30 min | Solo `schedule` (retrasos de 5-30 min); job en bucle de horas (choca con los términos de Actions) | Ofertas relámpago: latencia de ~5-8 min |
| 025 | **Prioridad y modo caliente**: productos que bajaron en las últimas 2 h y seguimientos con objetivo se leen cada ciclo; calendario de eventos divide intervalos entre 3 | Cadencia fija por fuente | Que las ofertas cortas no se escapen entre lecturas |
| 026 | **Canales públicos de Telegram como feed** (SP-6) vía `t.me/s/<canal>`: confirmación propia si la tienda es legible; si no, reenvío marcado «sin verificación propia» | Solo Slickdeals | David sigue DescuentosTech, Clubgratis y cajitatech |