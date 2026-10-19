# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`, cron-job.org cada 5 min).
- Rastreo de catálogo (ADR 0004) en producción desde el 2026-10-17: VTEX ×4, grupo con 14 temas, tarjetas PNG; canales de Telegram (ADR 0005) y feeds de Amazon «sin verificar» (ADR 0006/0007) también.
- Best Buy y Walmart fuera por decisión de David. Amazon da CAPTCHA desde Actions: su breaker sigue abierto.
- eBay (`ebay_us`, ADR 0008) en producción desde el 2026-10-18 (PR #13): `/seguir`, `/buscar` y job `crawl` con bajada de la misma publicación y `MODEL_LOW` (mediana del mismo `epid`). Esquema 6 (migración `0006`: canales DescuentosHome y OFERTU).
- Canal general de ofertas (opcional, secreto `TELEGRAM_CHANNEL_ID`): copia de cada oferta al canal, sin tema y sin botones de callback.
- Canales: DescuentosTech, DescuentosHome, Clubgratis, cajitatech y OFERTU (solo con código). Suite: 924 pruebas (unitarias, integración y e2e) en verde el 2026-10-19.

## Último cambio
- Rama `claude/precision-categorias-qof49c`: el tema (hashtag) de canales y feeds acertaba el 51 % contra ofertas reales etiquetadas; ahora frases, plurales, hashtags compuestos y peso menor tras «para/con/for» (93 % en ofertas no vistas; prueba con 946 etiquetadas exige ≥ 95 %). Subcategorías de las tiendas CO y los televisores quedan con el mismo tema en todas las fuentes. Clubgratis ya no toma como título «Comprar en Amazon…», «Precio incluye envío» ni «Aplicar cupón», ni un «20 %» como precio.

## Siguiente paso
- Ofertas con el tema equivocado: agregar el término a `channel_rules` y la oferta a `tests/fixtures/topics/ofertas-etiquetadas.jsonl`.
- David: crear el canal, hacer admin al bot con «Publicar mensajes» y guardar su id en el secreto `TELEGRAM_CHANNEL_ID` (README, paso 12).
- Revisar el texto oficial del acuerdo de la API de eBay (6 h de atraso máximo, nada de entrenar IA) antes de dejar el job encendido.
- Mirar la primera semana: alertas de eBay al día, cuota (`Unavailable quota` en los logs) y peso de la BD (tope 150 mil publicaciones). Si satura: `/fuente off ebay_us`.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase Postgres, Telegram vía getUpdates (ADR 0001); histórico por segmentos (ADR 0002); disparo externo y modo caliente (ADR 0003); rastreo de catálogo completo (ADR 0004); canales de Telegram como fuente `channel` sin verificar (ADR 0005); feeds de Amazon como canales sin orden de ids (ADR 0006), DealNews sin tope de antigüedad (ADR 0007) y eBay por la Browse API con comparación por modelo (ADR 0008).
- Sin costo puesto ni TRM: las alertas USD dicen «🇺🇸 Compra por casillero». Si spec y plan difieren, manda spec §0.2 y luego el plan.
- La confirmación exige ±1 %, stock, misma variante y mismo vendedor; en búsquedas sin vendedor visible (Amazon, Newegg) la ficha decide.
- Rastreo: descuento solo contra precios observados (nunca `ListPrice`); la alerta exige «antes» estable 24 h y 2 lecturas, confirmación en la ficha y que otra tienda no haya alertado el mismo EAN en 6 h.
- Sin temas guardados todo va a *General* sin aviso; con temas, una alerta cuyo tema falta va a *General* y se avisa una vez al día que corra `/temas`. Con la BD en ≥ 450 MB se pausan las búsquedas y el rastreo de Éxito, Carulla y eBay.
- Slickdeals: un solo task `feed` por ciclo, sin reserva de confirmación; el lead se omite si el listing se leyó hace < 6 h. Sus leads a Amazon se descartan con el breaker abierto: los cubre `slickdeals_amazon`.
- No se sortea ningún CAPTCHA ni detección de bots (camelcamelcamel descartado por Cloudflare; Keepa en pausa por costo).

## Pendientes y gotchas
- Supabase: el migrador necesita `GRANT CREATE ON DATABASE postgres TO discountz_owner` y `postgres` debe ser miembro de `discountz_owner`; varias claves erradas dan timeouts pasajeros en el pooler. Postgres en la nube: `service postgresql start` y `ALTER USER postgres PASSWORD 'postgres'`.
- Desde la nube de la sesión: Newegg da 403 CAPTCHA, Best Buy 502/timeout y Amazon con `curl_cffi` el intersticial de Akamai (fixtures con httpx); `*.vtexassets.com` está bloqueado por el proxy (no desde Actions). La búsqueda de Newegg da CAPTCHA también desde la red de David: desactivada, fixture sintética; sus fichas sí responden (buy box de un tercero). `record-fixture` no sigue redirecciones y en Windows escribe el `.meta.json` con CRLF.
- Slickdeals ya no publica `u2=` (usa `u3=` cifrado): solo Amazon (`data-aps-asin`) llega a una tienda. Su fixture de portada es del 2026-10-13. `slickdeals` y `slickdeals_amazon` bajan el mismo RSS por separado (~96 peticiones al día en total).
- DealNews responde `x-dn-is-bot-user-agent: Yes` pero sirve normal (`s-maxage=300`); `robots.txt` pide 2 s entre peticiones. Vuelve a destacar ofertas viejas y vigentes con su `pubDate` original (~13 al día): llegan desde la ADR 0007. Si el breaker de Amazon se cierra, una oferta puede llegar dos veces (lead verificado y feed sin verificar).
- Fechas del calendario de eventos 2026-2027 estimadas: David debe confirmarlas (Hot Sale y Cyber Lunes de Colombia).
- Falta: `setup-bot`, `pg_terminate_backend` tras 3 `skipped_locked`, descarte G10 contra `R_listing`, mensaje agrupado de tormenta y `config/supabase-ca.crt` para `verify-full`. Una alerta de un ciclo que murió antes de componer sale con vista reducida. En `/estado`, los códigos de más de 14 letras (`tg_descuentostech`, `slickdeals_amazon`) descuadran la columna.
- VTEX: el WAF rechaza `+` como espacio (`infra/http` usa `%20`); una página de 50 productos pesa hasta ~43 MB (se pide por mitades sobre 24 MB); `products/search` con varios `skuId` devuelve 10 sin `_from`/`_to`.
- Pendientes menores del rastreo: reloj fijo por corrida, `min_seen` sin ventana de 90 d, tope diario por día UTC, orden del barrido VTEX, fotos sin mirar el plazo, C07 sin unir tiendas, desalojo de automáticos por antigüedad, `crawl` sin verificar `SCHEMA_VERSION`, código 3 compartido, borrado del catálogo tras 30 d sin rastreo. Un producto ya seguido que reaparece en Slickdeals con lectura de hace < 6 h no da `COMMUNITY_DEAL`.
- Canales y feeds: un cambio de formato (≥ 5 ilegibles y 0 ofertas, una página sin posts o un feed vacío) cuenta como `PARSE_ERROR` (3 seguidos abren el breaker). Las fixtures de `t.me` llevan los usuarios cambiados por «Usuario»: anonimizar al regrabar. Temas de canales: cada término de `channel_rules` nombra un solo tema (falla al cargar si se repite); un término nuevo se mide con `tests/unit/test_topic_precision.py`.
- eBay, pendientes menores: `count(*)` del tope por página (y hasta +74 sobre el tope), sin filtro genérico de `external_id` en `process`, parámetros leídos del TOML al importar el adaptador, 100 auto-seguimientos cada 2 h no caben en 1.000 al día, tamaño del árbol completo sin medir contra el tope de 24 MB.
- ruff 0.17 formatea Markdown: `docs/` está excluido. La imagen `postgres:17` del CI no tiene digest fijado.
