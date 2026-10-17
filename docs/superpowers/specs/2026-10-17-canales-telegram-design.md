# discountZ: canales de ofertas de Telegram como fuente (spec de diseño, SP-6)

- Fecha: 2026-10-17
- Estado: aprobado por David el 2026-10-17
- Autor: David Santiago Rincón Bautista
- Base: spec de la v1 (`2026-10-13-discountz-v1-design.md`, fila SP-6 y ADR 026) y spec del rastreo (`2026-10-14-rastreo-catalogo-design.md`). Donde este spec no dice nada, mandan ellos.

## 0. Contexto y decisiones

### 0.1 Qué cambia

Amazon no se puede leer desde Actions (CAPTCHA) y su API oficial exige ventas de afiliado. Los canales públicos de ofertas DescuentosTech, Clubgratis y cajitatech publican muchas ofertas de Amazon y de otras tiendas que discountZ no cubre. discountZ lee esos canales y reenvía **todas** sus ofertas al grupo, cada una en el tema de su categoría. Como el bot no puede leer la tienda, la alerta usa el precio que publica el canal y va marcada **«sin verificar»**.

### 0.2 Decisiones de David (2026-10-17; prevalecen sobre el resto del documento)

| # | Decisión |
|---|---|
| F1 | Llegan **todas** las ofertas de los tres canales, cada una en el tema de su categoría. No se filtran por búsquedas ni por descuento. |
| F2 | Sin costo y sin cuenta: se lee la página pública `https://t.me/s/<canal>`. No se usa la cuenta de Telegram de David (riesgo para su cuenta). |
| F3 | La misma oferta publicada en varios canales llega una sola vez. |
| F4 | Cada alerta es una tarjeta con foto, precio y cupón, con la etiqueta «📣 <canal> · sin verificar» y un botón al enlace original. |

F1 reemplaza el filtro de la fila SP-6 de la v1, que pasaba los leads por las búsquedas.

### 0.3 Mediciones (2026-10-17, desde la nube de la sesión)

| Canal | Publicaciones al día | Formato del precio | Enlace |
|---|---|---|---|
| DescuentosTech | ~97 | `USD$180`, `USD$1.999` (punto de miles) | Facebook (`facebook.com/permalink.php`) |
| cajitatech | ~50 | `💰 $7.79 USD`, `💰 $4499910 COP` | acortador propio `pccajita.link` |
| Clubgratis | ~32 | `💰 12.74` (USD), `💰 4.499.910` (COP), `💰 $19.783` (COP); sin moneda escrita | `joylink.io` o enlace directo a la tienda |

- `t.me/s/<canal>` responde 200 con HTML (~120 KB, 20 publicaciones). `?after=<id>` devuelve las publicaciones posteriores a `id`, y `?before=<id>` las anteriores.
- Cada publicación trae `data-post="<canal>/<id>"` con un id creciente, `<time datetime=…>`, la foto como `background-image:url('https://cdnN.telesco.pe/file/…')` y el texto en `tgme_widget_message_text`.
- Hay publicaciones que no son ofertas: «reporta llegada» (Clubgratis), eventos sin precio («El Madrugón», «Hot Days») y avisos del canal.
- Las mismas ofertas salen en varios canales. Por ejemplo, el TV OLED con cupón `TVZE10` aparece en Clubgratis y en cajitatech, y el cortapelo con cupón `94A9K9HX` en DescuentosTech y en cajitatech. Los títulos cambian entre canales.
- `pccajita.link` y `joylink.io` responden 403 desde la nube: no se resuelven los acortadores.

## 1. Arquitectura

Sigue la regla `app → sources → infra → domain`.

| Módulo | Qué hace |
|---|---|
| `sources/telegram_channels.py` | Adaptador `kind="channel"` con tres códigos: `tg_descuentostech`, `tg_clubgratis` y `tg_cajitatech`. Una clase parametrizada por canal, como `VtexSource` por tienda. `fetch_posts(ctx, after_id) -> ChannelPage`. Un parser por canal convierte el HTML en `ChannelPost`. |
| `domain/community.py` (puro) | `ChannelPost`, `dedup_match(post, recent)`, `assign_channel_topic(post, rules)` y el texto del mensaje. |
| `app/channels.py` | El paso del ciclo: lee los canales que tocan, descarta duplicados, asigna el tema y encola la tarjeta. |
| `infra/db.py` | Cursor por canal y publicaciones recientes para la deduplicación. |
| `infra/card.py` | Variante de tarjeta `community` (§5). |
| `migrations/0003_canales` | Tablas `channel_cursor` y `channel_post` (§6). |

`/fuente on|off tg_…` enciende y apaga cada canal con el mecanismo de la v1. Los tres arrancan encendidos.

## 2. Lectura

- **Cuándo:** dentro del ciclo de 5 minutos, cada canal como máximo cada 10 minutos (`[channels] interval_min = 10` en `config/discountz.toml`). Son 3 peticiones cada 10 minutos, unas 430 al día.
- **Qué:** `GET https://t.me/s/<canal>?after=<último id>`. Si la página trae 20 publicaciones (puede haber más), se pide la siguiente hasta 3 páginas por canal y por ciclo. El resto queda para el ciclo siguiente.
- **Primera lectura:** sin cursor, se lee la página sin `after`, se guarda el id más alto y **no se envía nada**. Así la activación no manda de golpe las 20 últimas de cada canal.
- **Cursor:** se avanza en la misma transacción en que se encolan las tarjetas, así que una caída a mitad no pierde ni repite publicaciones.
- **Antigüedad:** una publicación con más de 6 h (por `<time>`) se marca como leída y no se envía. Cubre un cursor muy atrasado tras días sin ciclo.
- **HTTP:** el mismo `PacedClient` de las fuentes, con presupuesto propio por canal en `[sources.tg_…]`. Se usan las mismas cabeceras de navegador que con las demás fuentes y sin trucos contra detección (E6).

## 3. Extracción (`ChannelPost`)

Campos: `channel`, `post_id`, `published_at`, `title`, `price: Money`, `coupon: str | None`, `store: str | None`, `hashtags: tuple[str, ...]`, `image_url: SafeUrl | None` y `link: SafeUrl` (el enlace de la oferta o, si no hay, el de la publicación `https://t.me/<canal>/<id>`).

Reglas por canal, sacadas de las publicaciones reales:

- **DescuentosTech:** precio `USD\$([\d.,]+)` (el punto con 3 dígitos detrás es de miles: `USD$1.999` son 1999 dólares). El título es el primer párrafo después de la línea del precio y del cupón. El cupón sale de `Código: <X>`, y el enlace de `Ver oferta: <url>`. La tienda es «eBay» si trae `#Ebay`; si no, queda sin tienda.
- **cajitatech:** `💰 $<n> (USD|COP) - <título> 🛍`. El cupón sale de `Aplicar <X> para obtener` y el enlace de `Oferta ➡️ <url>`.
- **Clubgratis:** precio tras `💰` (o `➡️` sin `💰`). La moneda se deduce así: un número con grupos de miles con punto (`4.499.910`, `$19.783`, `275.000`) es **COP**; un número entero o con decimales de 1-2 dígitos (`12.74`, `6.5`, `130`) o con coma de miles (`$1,999.00`) es **USD**. El título es el primer párrafo después del precio y del cupón. El cupón sale de `CODE: <X>` o `cupón <X>`, la tienda de `Comprar en <tienda>` y el enlace del primer `href` a una tienda o a `joylink.io`.
- **Común:** una publicación sin precio legible o sin título **no es oferta**. Se marca como leída sin enviar y se registra `channel_skip reason=no_price`. El título se corta a 200 caracteres. La foto solo se acepta de `cdn*.telesco.pe` por https.
- Un formato que cambie en todas las publicaciones de una página (0 ofertas en 20 publicaciones con precio en el texto) cuenta como `ParseError` para el circuit breaker (§7).

## 4. Duplicados y tema

### 4.1 Duplicados (F3)

Una publicación es duplicado de otra de las últimas 24 h (de cualquier canal) si:

1. trae el mismo cupón (sin distinguir mayúsculas), o
2. tiene la misma moneda, precio a ±10 % y una similitud de títulos (Jaccard de tokens de `normalize_title`, sin palabras de 1-2 letras) de 0,5 o más.

Un duplicado se guarda como leído con `duplicate_of` y no se envía. Gana la primera publicación que llegue.

### 4.2 Tema

Nuevas reglas `[[channel_rules]]` en `config/topics.toml`, con `topic` y `words` (palabras normalizadas). Se comparan contra los hashtags y las palabras del título; gana la regla con más palabras que coinciden y, a igual número, la que va primero en el archivo. Sin coincidencias va a **Otros**. Ejemplos de partida:

- `tecnologia`: audifonos, iphone, celular, tablet, laptop, monitor, teclado, raton, mouse, control, videojuego, consola, smartwatch, reloj inteligente, proyector, cargador, accesoriostecnologia, board, ssd, ps5, xbox, switch
- `electro`: televisor, tv, freidora, aspiradora, licuadora, cafetera
- `hogar`: cocina, sartenes, ollas, cuchillos, almohadilla, decoracion, navidad, termo, botella
- `moda`: calzado, zapatilla, ropa, camisa, sueter, boxer, ropahombre, reloj, mochila
- `belleza`: maquillaje, cuidadofacial, cuidadocorporal, cortapelo, cuidadodelcabello, perfume
- `deportes`: deportes, gimnasio, senderismo, baloncesto, bañador
- `bebes`: juguete, ninos, bebe
- `ferreteria`: herramienta, destornillador, sierra, accesorioscarro, taladro

Una prueba de configuración comprueba que cada `topic` existe. Las reglas se ajustan con datos reales después de la primera semana.

## 5. Tarjeta y mensaje (F4)

- **Tarjeta `community`** en `infra/card.py`: misma plantilla de la tarjeta del catálogo, con la foto del canal, el precio grande y debajo el cupón («Cupón TVZE10») cuando lo hay. En lugar del «antes» y el %, una franja «📣 <Canal> · sin verificar». Si la foto no baja, la tarjeta sale sin foto, igual que hoy.
- **Mensaje** (pie de foto, HTML): título (máx. 120 caracteres), precio con `format_money`, tienda si se conoce, cupón en `<code>` y la línea «📣 Visto en <canal> · sin verificar». Las ofertas en USD llevan «🇺🇸 Compra por casillero», como en la v1. Un botón «Ver oferta» lleva al enlace original y otro «Ver en el canal» a `https://t.me/<canal>/<id>`.
- **Cola:** `outbox` con `topic`, `card`, prioridad baja (después de las alertas propias) y `dedup_key = "canal:<canal>:<id>"`. No pasa por la tabla `alert`: no hay listing ni histórico.
- **Ritmo:** el mismo de la entrega actual. ~180 tarjetas al día son ~1 por ciclo en promedio; los picos se reparten en los ciclos siguientes.
- La foto se baja con `make_photo_fetcher`, con `cdn1.telesco.pe` a `cdn5.telesco.pe` en los `image_hosts` del adaptador.

## 6. Datos (migración `0003_canales`, `SCHEMA_VERSION = 3`)

```sql
CREATE TABLE discountz.channel_cursor (
  channel     text PRIMARY KEY,
  last_id     bigint NOT NULL,
  updated_at  timestamptz NOT NULL
);

CREATE TABLE discountz.channel_post (
  channel       text NOT NULL,
  post_id       bigint NOT NULL,
  published_at  timestamptz NOT NULL,
  title_norm    text NOT NULL,
  price_minor   bigint NOT NULL,
  currency      text NOT NULL,
  coupon        text,
  topic         text NOT NULL,
  duplicate_of  text,            -- «<canal>:<id>» de la primera
  seen_at       timestamptz NOT NULL,
  PRIMARY KEY (channel, post_id)
);
CREATE INDEX channel_post_recent ON discountz.channel_post (seen_at);
```

- Solo se guardan las publicaciones que son ofertas (§3); las demás solo mueven el cursor.
- Retención: `channel_post` de más de 7 días se borra en el mantenimiento diario. Ocupa unos 1.300 registros, menos de 1 MB.
- Los grants siguen el patrón de `0002` (`discountz_app` lee y escribe).

## 7. Errores y salud

- Cada canal es una fuente con su circuit breaker y su salud, como las tiendas: 403/429 o una página de captcha es `HARD_BLOCK`; 5xx o timeout es `UNAVAILABLE`; el formato roto es `PARSE_ERROR`. El aviso de cambio de salud es el de la v1, una vez por cambio.
- Un canal caído no detiene el ciclo ni los otros canales.
- Logs con lista blanca: `channel_done source=… requests=… posts=… sent=… duplicates=… skipped=…`. Nunca el texto de la publicación, URLs con query ni cupones.

## 8. Resumen diario

La línea del catálogo suma «Canales: N ofertas (M repetidas)» de las últimas 24 h.

## 9. Pruebas

- **Fixtures reales:** una página de cada canal (`tests/fixtures/telegram/<canal>.html`) grabada con `record-fixture`, más una página `?after=` vacía.
- **Unitarias del parser** (por canal, sobre las fixtures): cuántas ofertas salen, precio y moneda de cada formato de §3 (incluidos `USD$1.999`, `4.499.910`, `$19.783`, `12.74`, `130`), cupón, tienda, foto, y que «reporta llegada» y los eventos sin precio no salen.
- **Unitarias de `domain/community.py`:** duplicado por cupón, por título y precio, y no duplicado con precio a más de 10 % o en otra moneda; asignación de tema por hashtag y por título, y caída a Otros.
- **Integración (Postgres):** primera lectura sin envíos, cursor y paginación con `after`, publicación vieja descartada, duplicado entre canales, retención de 7 días, y cursor que no avanza si falla la transacción.
- **E2E:** un ciclo con un canal falso encola una tarjeta `community` en el tema correcto, y `deliver` la manda con foto y botones.
- **Configuración:** las reglas de `[[channel_rules]]` apuntan a temas que existen.

## 10. Puesta en marcha (lo que hace David)

1. Fusionar el PR y correr *Actions → migrate* con `to = head`.
2. Nada más: los tres canales arrancan encendidos. Para apagar uno: `/fuente off tg_descuentostech` (o `tg_clubgratis`, `tg_cajitatech`).

## 11. Riesgos

- **Cambio de formato de un canal:** el parser deja de sacar ofertas y el breaker lo pausa con aviso. Se corrige el parser con una fixture nueva.
- **Moneda mal deducida en Clubgratis:** un precio en USD escrito con punto de miles (`1.299`) saldría como COP. Es raro en sus publicaciones; si aparece, se agrega la tienda (`Comprar en Amazon`) como pista de USD.
- **Volumen:** si un canal sube mucho su ritmo, `/fuente off` lo apaga. El resumen diario muestra el conteo.
- **Telegram limita `t.me/s`:** sería un `HARD_BLOCK` con pausa automática; no se intenta sortear.

## 12. Fuera de alcance

- Resolver acortadores (`pccajita.link`, `joylink.io`) y confirmar el precio en la tienda.
- Filtrar por búsquedas o por descuento (F1).
- Canales privados o que exijan cuenta.
- eBay por su API (pendiente de que eBay apruebe la cuenta de desarrollador de David).
