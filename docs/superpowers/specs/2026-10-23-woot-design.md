# Woot como fuente de ofertas de EE. UU. (diseño)

- Fecha: 2026-10-23
- Estado: aprobado por adelantado. David pidió que el sub-proyecto avance solo («deja activo q continue lo q pueda automáticamente», 2026-10-23 04:36Z): se toman las opciones recomendadas, anotadas aquí con su porqué, y el PR queda abierto para que él lo fusione.
- Sub-proyecto 3 de «Mejoras mayores del bot» (después del perfil e intereses y de 9to5Toys).

## 1. Qué se quiere

Más ofertas de tecnología de EE. UU. que compensen por casillero, de una fuente con API oficial. Woot (de Amazon) vende con descuento por tiempo limitado; su API (`developer.woot.com`) da por categoría las ofertas vivas con precio de venta, precio de lista, fechas, foto y si están agotadas. David ya tiene la clave (secreto `WOOT_API_KEY` en GitHub).

Éxito: cada oferta nueva de Woot de Computers, Electronics o Tools que pasa los filtros de siempre (perfil, US$25 por casillero, tope de 8 tarjetas por fuente al día, intereses) llega al grupo como tarjeta «sin verificar» con «🇺🇸 Compra por casillero», sin pasar la cuota de 1.000 peticiones al día.

## 2. Lo que dice la API (leído el 2026-10-23)

- `GET https://developer.woot.com/feed/{feedname}` con cabecera `x-api-key`. Feeds: All, Clearance, Computers, Electronics, Featured, Home, Gourmet, Shirts, Sports, Tools, Wootoff. Sin `page` devuelve todo el feed; con `page`, 100 por página.
- Cada ítem: `OfferId` (UUID), `Title`, `Url` (`https://<categoría>.woot.com/offers/…`), `Photo` (`d3gqasl9vmjfd8.cloudfront.net`), `SalePrice` y `ListPrice` como `{Minimum, Maximum}` en dólares (decimales JSON), `StartDate` y `EndDate` ISO con zona, `IsSoldOut`, `IsAvailableOnMobileAppOnly`, `Condition` (puede ser `null`), `Categories` (`"COMPUTERS/Laptops"`…).
- Límites: 1 petición por segundo (ráfaga de 10) y 1.000 al día (se reinicia a las 00:00 UTC); lo que sale de su caché no cuenta.
- `POST /getoffers` (detalle de hasta 25 ofertas) no hace falta: el feed ya trae lo que la tarjeta usa.

## 3. Enfoques

1. **Canal «sin verificar» sin orden de ids (elegido)**, como DealNews y Slickdeals (ADR 0006). Se relee el feed, se salta lo ya guardado por id y la primera lectura deja todo visto (`baseline`). Reutiliza `channel_post`, el dedupe entre canales, el tema por hashtags, el perfil, los intereses y el tope diario. Costo de revertir: bajo (una fila en `source` y un adaptador).
2. **Rastreo de catálogo con histórico** (como eBay). Woot vende por pocos días: casi ninguna oferta junta el histórico de 24 h que exige la alerta, y gastaría la cuota en releer. Descartado.
3. **Las dos**: más código y cuota para el mismo resultado. Descartado (YAGNI).

## 4. Diseño

**Adaptador `sources/woot.py`** (`WootSource`, `PostsOnlySource`, código `woot`, `kind="channel"`, USD, `auth="api_key"`, `ordered_ids=False`, `refeatures=True`):
- Una lectura pide los feeds `Computers`, `Electronics` y `Tools` (3 peticiones, sin `page`) y junta sus ítems en una `ChannelPage`; un `OfferId` repetido entre feeds cuenta una vez. Por qué esos tres: son los temas en tarjeta del perfil (tecnología, electro, ferretería); Home y Sports son solo de interés y no compensan la cuota.
- `post_id`: los primeros 63 bits del UUID (`int.from_bytes(uuid.bytes[:8]) >> 1`, mayor que 0 salvo un UUID nulo, que se descarta). La columna es `bigint`; con unos cientos de ofertas vivas, una colisión es despreciable.
- `published_at` = `StartDate`. `refeatures=True` porque un feed de categoría puede sumar ofertas que empezaron antes: el tope de 6 h de los canales las perdería; lo ya guardado se salta por id igual.
- Precio = `SalePrice.Minimum` en `Money` USD con `parse_float=Decimal` (nunca `float`). El «antes» no se usa: el canal no tiene precio de referencia y el de lista viene de Amazon (inflable).
- Se saltan sin contar como formato roto (`skipped`): agotadas, solo en la app, vencidas (`EndDate` ≤ ahora) y las de condición distinta de nueva (`Condition` no nula ni «New»). Por qué solo nuevas: es la regla que David fijó para eBay (`conditionIds:{1000}`).
- Cuentan en `unparsed` (formato roto): sin `OfferId` válido, título, `StartDate` con zona, `Url` en los hosts de Woot o precio legible.
- `hashtags`: las palabras de `Categories` («COMPUTERS/Laptops» → `computers`, `laptops`) para el tema, como las categorías de DealNews. `store="Woot"`, `casillero=True`, `post_url = link` (un solo botón «Ver oferta»). Foto solo de `d3gqasl9vmjfd8.cloudfront.net`. Hosts del enlace: `woot.com`, `www.woot.com` y los subdominios de sus categorías (`computers`, `electronics`, `tools`, `home`, `sport`, `shirt`, `wine`, `gourmet`, `kids`, `sellout`, `accessories`, `deals`).
- Clave: `ctx.credentials.bearer("woot")`, solo en la cabecera, nunca en logs. Un 403 de la API es clave mala (`CredentialUnavailable("bad_key")`), no un bloqueo anti-bots.
- Un feed vacío no es error (una categoría puede quedarse sin ofertas); un cuerpo que no es un `FeedPage` (`Items` lista) sí (`ParseError`).

**Cambios fuera del adaptador:**
- `run_channels` recibe `credentials` (por defecto `NoCredentials()`) y `_fetch` se las pasa al contexto; hoy los canales no tienen claves. `cycle.py` le pasa `deps.credentials`.
- `Settings.woot_api_key: SecretStr | None` (`DISCOUNTZ_WOOT_API_KEY`) y `EnvCredentials.bearer("woot")`. El workflow `cycle.yml` mapea `secrets.WOOT_API_KEY`. Sin clave, la fuente falla como no disponible y su breaker la frena (igual que eBay sin claves).
- `config/discountz.toml`: `[sources.woot]` con `min_pause_s = 1`, `max_per_run = 3`, `max_per_24h = 400`, `watch_interval_min = 15` (≈ 288 peticiones al día, menos de un tercio de la cuota, dejando margen a la caché y a los reintentos).
- Migración `0014_woot` (la `0013` es de 9to5Toys, acordado con ese hilo): `INSERT INTO source ('woot','channel','USD',true,NULL)`; la reversa la borra (cursor y publicaciones en cascada). `SCHEMA_VERSION = 14`.
- Nombre «Woot» en `render.py` y color propio en `infra/card.py`; ADR 0015 (el 0014 es de 9to5Toys).

## 5. Errores y operación

- Cuota: 429 o un 5xx los maneja `infra/http` como no disponible; el breaker y `max_per_24h` hacen el resto.
- Formato roto: la racha de `unparsed` de los canales (`FORMAT_BROKEN_MIN`) avisa como en los demás feeds.
- Logs: solo los campos de la lista blanca (fuente, contadores); ni la clave ni la URL con query.

## 6. Pruebas

- `tests/sources/test_woot.py` sobre una fixture sintética (`tests/fixtures/woot/computers.json`, con `"nota"` en su `.meta.json` hasta grabarla con la clave desde Actions): precio en `Money`, id estable desde el UUID, saltos (agotada, app, vencida, reacondicionada), `unparsed`, dedupe entre feeds, hashtags, 403 → clave mala, cabecera `x-api-key`.
- `tests/unit` para `Settings`/`EnvCredentials` con `woot`, y que `run_channels` pasa las credenciales.
- Integración: migración `0014` arriba y abajo y conteo de fuentes.

## 7. Fuera de alcance

- Grabar la fixture real (la nube no tiene la clave): queda como pendiente con un workflow temporal autorizado por David.
- Precio de referencia en la tarjeta, Woot-Off y los feeds Home/Sports.
