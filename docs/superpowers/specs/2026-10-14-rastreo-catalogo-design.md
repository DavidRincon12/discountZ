# discountZ: rastreo de catálogo completo y alertas con imagen (spec de diseño)

- Fecha: 2026-10-14
- Estado: aprobado por David el 2026-10-14 (con E7 y E8)
- Autor: David Santiago Rincón Bautista
- Base: `docs/superpowers/specs/2026-10-13-discountz-v1-design.md` (v1). Donde este spec no dice nada, manda la v1.

## 0. Contexto y decisiones

### 0.1 Qué cambia

La v1 solo vigila lo que David pide (`/seguir` y `/buscar`, D4). Ahora discountZ recorre **solo** el catálogo completo de las tiendas, detecta bajadas de precio reales contra su propio histórico y las publica como **imagen** (foto, precio grande, % y tienda) en un **grupo de Telegram con temas**, uno por categoría grande. Los seguimientos y búsquedas de la v1 siguen igual.

### 0.2 Decisiones de David (2026-10-14; prevalecen sobre el resto del documento)

| # | Decisión |
|---|---|
| E1 | Rastrear **cualquier producto** de todas las tiendas, sin que David elija. Categorías grandes que abarquen casi todo. |
| E2 | Costo $0: la opción que más abarque sin pagar. No descartar productos relevantes. |
| E3 | Alertas en **pestañas por categoría** (grupo de Telegram con temas). |
| E4 | Cada alerta es una **imagen** con la foto del producto y el precio actual, al estilo de los canales de ofertas (PROMOCAJITA, Club de los gratis, DescuentosTech). |
| E5 | Todas las tiendas, no solo Éxito y Carulla. Las de EE. UU. son las que más le importan. |
| E6 | Nunca sortear CAPTCHA ni la detección de bots. EE. UU. entra **solo por vías oficiales** (APIs) y por Slickdeals. |
| E7 | Best Buy por su API se implementa ahora; Walmart por su API de afiliados queda **lista para activar** cuando aprueben a David. |
| E8 | Ampliar todo lo posible la cobertura de `/seguir` en las tiendas sin catálogo (Amazon, Newegg, Walmart) (§4.4). |

E1 reemplaza el alcance de D4 de la v1 (que se conserva y se amplía).

### 0.3 Mediciones (2026-10-14, desde la nube de la sesión)

| Tienda | Catálogo | Con stock | Peticiones por vuelta (50 por petición) | Peso por petición |
|---|---|---|---|---|
| Éxito | 1.060.000 | 305.000 | ~6.100 | ~5,8 MB sin compresión (las `Installments` pesan el 95 %); ~1,8 s |
| Carulla | 986.000 | 126.000 | ~2.500 | ~0,8 MB |
| Olímpica | 51.000 | 50.000 | ~1.000 | ~6 MB, 0,34 MB con gzip |
| Panamericana | 35.000 | 33.000 | ~700 | ~1,2 MB |

- «Con stock»: `hideUnavailableItems=true` en intelligent-search; equivalente legacy `fq=isAvailablePerSalesChannel_1:1`.
- El `ListPrice` del marketplace está inflado: un set de bolsas de $20.700 anunciaba «antes» $3.450.034.500. El orden `OrderByBestDiscountDESC` / `sort=discount:desc` devuelve esa basura primero ⇒ **descartado**; el descuento se mide solo contra precios observados por discountZ.
- Con varios vendedores, el primero puede venir con precio 0: se toma el menor `Price > 0` con `AvailableQuantity > 0`.
- `*.vtexassets.com` (fotos) está bloqueado por el proxy de la sesión, no desde Actions.

### 0.4 Tiendas de EE. UU. (E5, E6)

| Tienda | Vía | Estado en este spec |
|---|---|---|
| Best Buy | **API oficial** `api.bestbuy.com/v1` con clave gratuita de developer.bestbuy.com | **Incluida**: catálogo completo y cambios por `priceUpdateDate` |
| Walmart | API de afiliados (walmart.io + programa de afiliados), requiere aprobación | **Lista para activar** (E7): `walmart_us` usa la API cuando hay credenciales (§4.5) |
| Amazon | Creators API (sucesora de PA-API 5 desde 2026-04-30): exige ventas referidas cada 30 días | Fuera: no viable sin ventas. Keepa es de pago. Cubierta por Slickdeals y `/seguir` |
| Newegg | Sin API pública | Fichas individuales con `/seguir` (v1); sin catálogo |
| Slickdeals | RSS (v1) | Todas sus categorías, al tema «🇺🇸 Comunidad EE. UU.» |

---

## 1. Arquitectura

Se mantiene el monolito síncrono `app → sources → infra → domain` (ADR 0001). El rastreo es un **segundo caso de uso** con su propio comando y workflow; el ciclo de 5 min no cambia salvo la entrega (§5). Decisión en ADR 0004.

### 1.1 Módulos nuevos

| Módulo | Capa | Responsabilidad |
|---|---|---|
| `domain/catalog.py` | domain | Puro: `CatalogItem`, `CatalogObservation`, regla de detección (§3), clave de duplicados, prioridad de la capa caliente |
| `domain/topics.py` | domain | Puro: temas, umbrales por tema y asignación ruta de categoría de la tienda → tema |
| `sources/base.py` | sources | `Capability.CATALOG` y protocolo `CatalogSource` (§2.1) |
| `sources/vtex.py` | sources | `VtexSource` implementa `CatalogSource` (partición por hoja del árbol de categorías) |
| `sources/bestbuy.py` | sources | `bestbuy_us` pasa del HTML (bloqueado desde Actions) a la API oficial: `CatalogSource`, `fetch_listings` y búsqueda por SKU/texto. Mismo código de fuente, así se conserva su histórico |
| `infra/card.py` | infra | Genera la tarjeta PNG con Pillow (§5.2) |
| `infra/telegram.py` | infra | `send_photo`, `create_forum_topic` y `message_thread_id` en todos los envíos |
| `infra/db.py` | infra | Repositorio del catálogo, cursores y temas |
| `app/crawl.py` | app | Caso de uso: una corrida de rastreo de una tienda, con plazo (§2.2) |
| `app/deliver.py` | app | Entrega con foto y tema (§5) |
| `config/topics.toml` | config | Temas, umbrales y reglas de asignación |

Dependencias nuevas: `pillow` y `cryptography` (firma RSA de Walmart), fijadas con `==`, y una fuente tipográfica OFL (Inter) versionada en `assets/fonts/` con su licencia.

### 1.2 Comandos de la CLI

- `python -m discountz crawl --source <code> [--minutes 45]`: una corrida de rastreo.
- `python -m discountz run`: sin cambios de interfaz; su entrega ahora manda fotos a temas.

---

## 2. Rastreo

### 2.1 Contrato

```python
class CatalogSource(Protocol):
    descriptor: SourceDescriptor            # capabilities incluye Capability.CATALOG
    def catalog_partitions(self, ctx: SourceContext) -> Sequence[str]: ...
    def fetch_catalog_page(self, ctx: SourceContext, partition: str, page: int) -> CatalogPage: ...
    def fetch_changed(self, ctx: SourceContext, since: datetime, page: int) -> CatalogPage: ...  # opcional

@dataclass(frozen=True, slots=True)
class CatalogPage:
    items: Sequence[CatalogObservation]     # solo con stock y precio > 0
    has_more: bool

@dataclass(frozen=True, slots=True)
class CatalogObservation:
    external_id: str                        # skuId VTEX / sku Best Buy
    price: Money
    category_path: str                      # «/Tecnología/Computadores/…» tal como la da la tienda
    ean: str | None                         # EAN/UPC para duplicados
    seller_class: SellerClass
```

Título, foto y URL **no se guardan**: se leen al alertar (§4.3).

- **VTEX:** particiones = hojas de `/api/catalog_system/pub/category/tree/<n>`; si una hoja pasa de 2.500 productos (tope de paginación), se divide por rango de precio (`fq=P:[a TO b]`). Página = `products/search?fq=C:/<id>/&fq=isAvailablePerSalesChannel_1:1&_from&_to` de 50. Éxito y Carulla siguen el 308 a `/io/api/...` (como la v1).
- **Best Buy (`bestbuy_us`):** una partición `all`: `products(active=true&orderable!=NotAvailable)?show=sku,salePrice,categoryPath.name,upc&pageSize=100&sort=sku.asc`. `fetch_changed` = `products(priceUpdateDate>{since})`. Clave en el secreto `BESTBUY_API_KEY`; nunca en logs (la URL lleva la clave como query: los logs ya excluyen queries).

### 2.2 Corrida (`app/crawl.py`)

1. Toma el lease del rastreo de esa tienda (`crawl_lease`, distinto del lease del ciclo; TTL 60 min).
2. **Capa caliente** primero (hasta el 25 % del presupuesto de la corrida): productos con `last_changed_at` en los últimos 7 días, más recientes primero, releídos por lote con `fetch_listings` (VTEX: 50 `skuId` por petición). En Best Buy la capa caliente es `fetch_changed(since=última corrida)`.
3. **Vuelta completa** con el resto del tiempo: continúa desde `crawl_cursor` (partición, página); al terminar todas las particiones empieza otra vuelta y registra su duración.
4. Por cada página: `upsert` en lote de los productos, registro de cambios de precio y evaluación de candidatos (§3). Los candidatos se encolan para confirmar (§4).
5. Para con margen al llegar a `--minutes`, al agotar el presupuesto o ante un bloqueo (403, 429, CAPTCHA): en bloqueo marca la fuente con el circuit breaker de la v1 (§4.6 v1) y no reintenta.

### 2.3 Presupuesto y cadencia (`config/discountz.toml`, `[crawl.<code>]`)

Separado del presupuesto de `/seguir`; se respeta `min_pause_s` + jitter de la fuente.

| Tienda | Pausa | Tope 24 h | Vuelta completa estimada |
|---|---|---|---|
| Éxito | 1 s + jitter 1 s | 24.000 | ~6 h |
| Carulla | 1 s + jitter 1 s | 12.000 | ~2 h |
| Olímpica | 2 s + jitter 1 s | 6.000 | ~1 h |
| Panamericana | 2 s + jitter 1 s | 6.000 | ~1 h |
| Best Buy API | 0,25 s (límite 5/s) | 40.000 (límite 50.000) | medir en el plan |

### 2.4 Workflow `crawl.yml`

- `workflow_dispatch` desde cron-job.org cada hora, más `schedule` de respaldo cada 3 h.
- Matriz con un job por tienda (`fail-fast: false`), `timeout-minutes: 55`, `concurrency: discountz-crawl-<code>`.
- Interruptor: la variable `DISCOUNTZ_RASTREO=true` (como `DISCOUNTZ_ACTIVO`). Best Buy además exige el secreto de la clave.

---

## 3. Detección (`domain/catalog.py`, puro)

### 3.1 Estado por producto

`price` (último), `usual` (precio que se mantuvo al menos 24 h y 2 lecturas, el «antes»), `min_seen` (mínimo de 90 días, excluyendo precios vistos una sola vez que luego subieron), `first_seen_at`, `stable_since`, `last_changed_at`.

### 3.2 Reglas

Con `P` = precio nuevo, `U` = `usual`, `drop_bp = (U − P)·10000 / U`, umbrales por tema (§6):

| Tipo | Condición | Desde |
|---|---|---|
| `PRICE_ERROR` | `drop_bp ≥ g_bp` (50-75 % según tema) | En cuanto existe `U` (24 h y 2 lecturas), sin esperar los 3 días |
| `CATALOG_DROP` | `first_seen_at ≤ now − 3 d` ∧ `drop_bp ≥ t_bp` del tema ∧ `P < min_seen` ∧ ahorro absoluto ≥ `min_abs_cop` / `min_abs_usd` | 3 días de observación |

- Nunca se usa el `ListPrice` de la tienda.
- Un producto visto por primera vez no puede alertar (no hay `U`).
- **Cooldown:** una alerta por producto cada 72 h, salvo nueva bajada ≥ 3 % (`improve_bp` de la v1).
- **Duplicados:** mismo EAN, misma moneda y precio ±1 % alertado en las últimas 6 h por otra tienda ⇒ la segunda se omite (queda en el log).

### 3.3 Casos de prueba mínimos

| Caso | Historial | Lectura | Resultado |
|---|---|---|---|
| C01 | 99.900 estable 5 d | 59.900 (Tecnología, t 12 %) | `CATALOG_DROP` 40 % |
| C02 | 99.900 estable 2 d | 59.900 | nada (menos de 3 d) |
| C03 | 99.900 estable 1 d | 19.900 (g 50 %) | `PRICE_ERROR` |
| C04 | primera lectura 19.900 con ListPrice 3.450.034.500 | — | nada |
| C05 | 99.900 → 89.900 (1 lectura) → 99.900 | 89.900 | no baja `usual` ni `min_seen` por el pico de una lectura |
| C06 | alerta hace 10 h a 59.900 | 58.900 | nada (mejora < 3 %) |
| C07 | Éxito y Carulla, mismo EAN, 59.900 | — | 1 alerta con las dos tiendas |
| C08 | Mercado, 20.000 → 18.500 | — | nada (ahorro < $10.000 o < t del tema) |

---

## 4. Confirmación y alerta

### 4.1 Confirmación

Antes de encolar, se relee el producto por la ruta de ficha (`fetch_listings` de la fuente; Best Buy: `products/<sku>.json`): precio ±1 %, en stock y vendedor admitido por el tema. Si falla, se descarta y se registra el código de rechazo.

### 4.2 Integración con la v1

El candidato confirmado se convierte en `listing` de la v1 (`upsert_listing`, origen `catalog`), así funcionan `/seguir` desde el botón, silenciar y el histórico por segmentos. La alerta se guarda en `alert` con `kind ∈ {price_error, catalog_drop}` y pasa por la `outbox` con `purpose='alert'`.

### 4.3 Datos de la tarjeta

En la confirmación se leen título, URL de la ficha y URL de la foto (VTEX: `items[].images[0].imageUrl`; Best Buy: `image`). Se guardan en la fila de la `outbox` (`card` JSONB: tienda, tema, título, precio, `U`, `drop_bp`, días de mínimo, URL de foto). La imagen se genera en la entrega.

### 4.4 Más cobertura con `/seguir` (E8)

Para Amazon, Newegg y Walmart no hay catálogo, así que discountZ acumula histórico de todos los productos que pueda leer de forma legítima:

- **Seguimiento automático desde Slickdeals:** cada lead que se resuelve a un producto de una tienda (hoy, Amazon por `data-aps-asin`) crea un seguimiento automático (`watch.origin = 'auto'`) sin objetivo. Vence a los 30 días sin alerta. Tope por tienda (`[sources.<code>] max_auto_watches`, Amazon 300), los más nuevos primero. Así esos productos ya tienen histórico la próxima vez que bajen.
- **`/seguir` en lote:** hasta 20 URLs por mensaje (antes 5), una por línea. Los enlaces cortos (`a.co`, `amzn.to`) se siguen resolviendo como en la v1.
- **Botón «Seguir»** en cada tarjeta del catálogo y de la comunidad (v1 `fol:L…`).
- **Best Buy por API:** una URL de Best Buy en `/seguir` se lee por la API (`products/<sku>.json`) en lugar del HTML, que bloquea desde Actions.
- **Walmart por API** cuando haya credenciales (§4.5); mientras tanto sigue el HTML de la v1 detrás de `/fuente on`.
- `/lista` distingue los seguimientos automáticos (`a12`) de los manuales (`p12`); `/dejar a12` y `/dejar auto` (todos) los quitan.
- Todo dentro del presupuesto por fuente de la v1: si Amazon da CAPTCHA, el circuit breaker la detiene igual que hoy (E6).

### 4.5 Walmart lista para activar (E7)

- `walmart_us` (sigue apagada) cambia a la API cuando `CredentialStore` tiene las credenciales de Walmart: `fetch_listings` por `itemId` (`/api-proxy/service/affil/product/v2/items?ids=…`, hasta 20 por petición) y búsqueda (`/search`), firmada con la clave privada RSA según la documentación de walmart.io (cabeceras `WM_CONSUMER.ID`, `WM_CONSUMER.INTIMESTAMP`, `WM_SEC.KEY_VERSION`, `WM_SEC.AUTH_SIGNATURE`).
- Secretos: `WALMART_CONSUMER_ID`, `WALMART_KEY_VERSION`, `WALMART_PRIVATE_KEY` (PEM) y `WALMART_PUBLISHER_ID`. `check-config` valida que estén los cuatro o ninguno.
- Fixtures sintéticas marcadas con `"nota"` construidas desde la documentación pública; al aprobarse el acceso, David graba fixtures reales y la activa con `/fuente on walmart_us`. Sin credenciales sigue el HTML de la v1. Si la API resulta tener una paginación de catálogo útil, el rastreo de §2 se le agrega sin tocar el núcleo.

---

## 5. Telegram

### 5.1 Topología

- `TELEGRAM_CHAT_ID` pasa a ser el **supergrupo con temas** de David. Se mantiene la autorización de la v1 (`from.id == OWNER` ∧ `chat.id == CHAT`).
- Comando nuevo `/temas`: crea con `createForumTopic` los temas que falten (§6) y guarda su `message_thread_id` en la tabla `topic`. Es idempotente.
- Los comandos y respuestas del bot van al tema «General».
- Alertas de seguimientos de la v1: al tema de su categoría (tecnologia y componentes → Tecnología, hogar → Hogar y muebles, ropa → Moda y calzado, otros → Otros). Slickdeals → «🇺🇸 Comunidad EE. UU.».
- Si un tema no existe (o Telegram responde `message thread not found`), se envía a «General» y se avisa una vez para correr `/temas`.

### 5.2 Tarjeta (`infra/card.py`)

PNG 1080×1080 (maqueta aprobada `/mnt/project-files/rastreo-catalogo/maqueta-tarjeta.png`):

- Píldora con el nombre de la tienda (color por tienda) y `#Tema` arriba.
- Foto del producto en un recuadro blanco, escalada sin deformar. Si la descarga falla o no es imagen válida (≤ 5 MB, solo JPEG/PNG/WebP, `Image.verify`), se dibuja un recuadro con el nombre de la tienda en su lugar.
- Insignia amarilla `-NN%` «vs. su precio».
- Precio actual grande en verde; «Antes» `U` tachado; línea «Mínimo en N días» o «POSIBLE ERROR DE PRECIO» en rojo para `PRICE_ERROR`; «🇺🇸 Compra por casillero» en USD.
- Las fotos se descargan con el `HttpClient` de la v1, solo desde los hosts de imágenes declarados por la fuente (`SourceDescriptor.image_hosts`).
- `sendPhoto` sube el PNG como `multipart/form-data`: el `HttpClient` gana el parámetro `files`.

### 5.3 Mensaje

`sendPhoto` con `message_thread_id` del tema, la tarjeta y `caption` HTML (≤ 1.024 caracteres): `#Tema`, título escapado, precio – antes, tienda y enlace; los botones de la v1 (Abrir en tienda, Seguir, Silenciar 30 d). Las alertas de seguimientos de la v1 sin foto siguen como texto, pero al tema que les toca.

### 5.4 Ritmo

Límite de Telegram: ~20 mensajes por minuto en un grupo. La entrega del ciclo envía como máximo 15 por ciclo, por orden: `PRICE_ERROR`, luego mayor `drop_bp`. Lo demás espera en la `outbox` con capacidad de ~180 por hora; una alerta del catálogo que lleve 24 h sin salir expira, porque su precio ya pudo cambiar.

---

## 6. Temas (`config/topics.toml`)

| Código | Tema | t_bp | g_bp |
|---|---|---|---|
| `errores` | 🔥 Errores de precio | — | — |
| `tecnologia` | 📱 Tecnología | 1200 | 5000 |
| `electro` | 🔌 Electrodomésticos | 1500 | 5000 |
| `hogar` | 🛋️ Hogar y muebles | 2000 | 6000 |
| `moda` | 👕 Moda y calzado | 3000 | 7500 |
| `belleza` | 💄 Belleza y cuidado | 2500 | 7000 |
| `mercado` | 🛒 Mercado | 2500 | 7000 |
| `deportes` | ⚽ Deportes | 2000 | 6000 |
| `bebes` | 🧸 Bebés y juguetes | 2000 | 6000 |
| `ferreteria` | 🔧 Ferretería y autos | 2000 | 6000 |
| `mascotas` | 🐶 Mascotas | 2000 | 6000 |
| `libros` | 📚 Libros y papelería | 2000 | 6000 |
| `comunidad_us` | 🇺🇸 Comunidad EE. UU. | — | — |
| `otros` | 📦 Otros | 2000 | 6000 |

- `PRICE_ERROR` se publica en «Errores de precio» **y** se menciona en el tema de su categoría con una línea de texto.
- Reglas de asignación: lista ordenada `{fuente o "*", prefijo de ruta, tema}`; gana el prefijo más largo; sin coincidencia ⇒ `otros`. Se construye en el plan con los árboles reales de las 4 tiendas VTEX y de Best Buy; una prueba exige que ninguna hoja de primer nivel caiga en `otros` sin estar listada a propósito.
- Los umbrales iniciales se revisan tras 2 semanas con el conteo de alertas por tema (resumen diario).

---

## 7. Datos

### 7.1 Migración `0002_catalogo`

```sql
CREATE TABLE catalog_item (
  id bigserial PRIMARY KEY,
  source text NOT NULL REFERENCES source(code),
  external_id text NOT NULL,
  ean text,
  topic text NOT NULL,
  currency char(3) NOT NULL,
  price_minor bigint NOT NULL,
  usual_minor bigint,
  min_seen_minor bigint,
  first_seen_at timestamptz NOT NULL,
  stable_since timestamptz NOT NULL,
  last_changed_at timestamptz,
  last_seen_at timestamptz NOT NULL,
  last_alert_at timestamptz,
  last_alert_minor bigint,
  UNIQUE (source, external_id)
) WITH (fillfactor = 80);
CREATE INDEX ON catalog_item (source, last_changed_at DESC) WHERE last_changed_at IS NOT NULL;
CREATE INDEX ON catalog_item (ean) WHERE ean IS NOT NULL;

CREATE TABLE catalog_price_change (
  item_id bigint NOT NULL REFERENCES catalog_item(id) ON DELETE CASCADE,
  at timestamptz NOT NULL,
  price_minor bigint NOT NULL,
  PRIMARY KEY (item_id, at)
);

CREATE TABLE crawl_cursor (
  source text PRIMARY KEY REFERENCES source(code),
  sweep_no int NOT NULL DEFAULT 0,
  partition_idx int NOT NULL DEFAULT 0,
  page int NOT NULL DEFAULT 0,
  sweep_started_at timestamptz,
  last_sweep_s int,
  last_run_at timestamptz,
  changed_since timestamptz
);

CREATE TABLE crawl_lease (source text PRIMARY KEY, holder text NOT NULL, expires_at timestamptz NOT NULL);

CREATE TABLE topic (code text PRIMARY KEY, title text NOT NULL, thread_id bigint);

ALTER TABLE outbox ADD COLUMN topic text, ADD COLUMN card jsonb;
ALTER TABLE watch ADD COLUMN origin text NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'auto')),
                  ADD COLUMN expires_at timestamptz;
```

Más `alert.kind` con `catalog_drop`, `outbox.purpose` sin cambios y los permisos de `discountz_app` en las tablas nuevas.

### 7.2 Tamaño frente a 500 MB

| Tabla | Supuesto | MB |
|---|---|---|
| `catalog_item` | 515.000 VTEX + Best Buy (estimado 150.000) × ~130 B + índices | ~110 |
| `catalog_price_change` | 5 % de productos cambia al día, 90 d de retención, ~50 B | ~60-80 |
| Tablas de la v1 | §2.4 v1 | ~115 año 1 |
| **Total** | | **~290-310 MB** |

Mantenimiento diario (se agrega al de la v1): borrar `catalog_price_change` de más de 90 d; borrar `catalog_item` sin verse en 30 d. Guardas de la v1 adaptadas: a ≥ 400 MB la retención de cambios baja a 45 d; a ≥ 450 MB se pausa el rastreo de Éxito y Carulla (los más grandes) y se avisa.

---

## 8. Errores y salud

- Un bloqueo en el rastreo usa el circuit breaker de la fuente (v1 §4.6) y el aviso de salud; el ciclo de `/seguir` de esa tienda también lo respeta.
- Un error de parseo en una página se registra (código, partición, página) y se salta esa página; 3 páginas seguidas con error detienen la corrida.
- El resumen diario agrega: duración de la última vuelta por tienda, productos activos, cambios del día, alertas por tema y tamaño de la BD.
- Logs con la lista blanca de la v1: nunca URLs con query (la clave de Best Buy viaja en la query), títulos ni cuerpos.

## 9. Pruebas

- **Fuentes:** fixtures reales de una página de catálogo de cada tienda VTEX (grabadas desde la nube, que sí responde) y de Best Buy API (grabada por David con su clave, clave redactada por el *scrubber*).
- **Dominio:** casos C01-C08 y pruebas con hypothesis de la regla (nunca alerta sin `U`; nunca usa `ListPrice`).
- **Temas:** asignación de todas las hojas de primer nivel reales.
- **Tarjeta:** PNG 1080×1080 válido con foto normal, sin foto, foto corrupta, foto enorme, título largo y USD; prueba *golden* por hash perceptual en `tests/golden/`.
- **Integración (Postgres):** corrida de rastreo con fuente falsa: cursor, reanudación, capa caliente, cooldown, duplicados por EAN, `outbox` con `card`; entrega con `sendPhoto` y tema falso.
- **Arquitectura:** import-linter con los módulos nuevos.

## 10. Puesta en marcha (lo que hace David)

1. BotFather: `/setjoingroups` → Enable (se puede volver a Disable después de agregar el bot).
2. Crear un grupo, activar «Temas» y agregar el bot como administrador con «Gestionar temas».
3. Escribir algo en el grupo, leer su id (negativo, `-100…`) y ponerlo en el secreto `TELEGRAM_CHAT_ID`.
4. Enviar `/temas` en el grupo.
5. Crear la clave de Best Buy, guardarla en el secreto `BESTBUY_API_KEY` y activar la fuente con `/fuente on bestbuy_us`.
6. Aplicar la migración `0002`, poner `DISCOUNTZ_RASTREO=true` y crear en cron-job.org el disparo horario de `crawl.yml`.

Se entregan como pasos de copiar y pegar en el README y en la guía.

## 11. Riesgos

- Términos de la API de Best Buy: limitan el cacheo de su contenido. Se guardan solo SKU, precio y fechas (no títulos, fotos ni descripciones); David los revisa al crear la clave y, si lo prohíben, `bestbuy_us` queda apagada.
- Volumen de Éxito y Carulla (~36.000 peticiones al día entre las dos): si aparecen 429 o bloqueos, el circuit breaker detiene el rastreo y se baja el tope.
- Avalancha de alertas al principio: los umbrales se ajustan con datos reales (§6).

## 12. Fuera de alcance

- Amazon por API (exige ventas de afiliado); Keepa (de pago).
- Rastreo del catálogo completo de Walmart (solo cuando su API esté activa y se mida).
- Otras tiendas de Colombia (Alkosto, Falabella, Mercado Libre): siguen como SP-1 a SP-3 de la v1.
- Productos agotados: no se rastrean; vuelven a entrar cuando reaparecen con stock en una vuelta.
