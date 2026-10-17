# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`, cron-job.org cada 5 min).
- Rastreo de catálogo (ADR 0004) en producción desde el 2026-10-17: VTEX ×4 por categoría raíz, grupo con 14 temas, tarjetas PNG, `/temas`, `/seguir` hasta 20 URLs, `/dejar aNN|auto`, seguimientos automáticos desde Slickdeals.
- Canales de Telegram (ADR 0005, PR #8) en producción desde el 2026-10-17: `tg_descuentostech`, `tg_clubgratis` y `tg_cajitatech`, tarjetas «sin verificar» y línea «Canales» en el resumen diario.
- Best Buy y Walmart fuera por decisión de David (sin `BESTBUY_API_KEY`, `/fuente off walmart_us`). Amazon da CAPTCHA desde Actions: su breaker sigue abierto.
- Suite: 767 pruebas (unitarias, integración y e2e) en verde el 2026-10-17.

## Último cambio
- Rama `claude/discountz-puesta-en-marcha-mlcmm6` (ADR 0006): ofertas de Amazon «sin verificar» desde el RSS de DealNews (`dealnews`, cada 10 min) y la portada de Slickdeals (`slickdeals_amazon`, cada 30 min), como canales con `ordered_ids=False`: se releen enteros y saltan los ids guardados; la primera lectura con ofertas marca con `channel_post.baseline` sin enviar. Migración `0004` (`SCHEMA_VERSION = 4`), botones «Ver en DealNews» y «Ver en Slickdeals», palabras en inglés y reglas de Mascotas, Mercado y Libros en `channel_rules`.
- Tras la revisión: el id de DealNews sale del `<guid>`, un enlace que `SafeUrl` cambiaría no se usa, y los ítems ilegibles o un feed vacío cuentan como formato roto.

## Siguiente paso
- David: fusionar el PR con Squash and merge y correr *Actions → migrate* con `to = head`; hasta entonces el ciclo se salta con aviso. Criterio: tras la primera lectura de cada feed (que solo marca), llegan tarjetas nuevas de Amazon en sus temas.
- Mirar el volumen de la primera semana (estimado: ~33 al día de DealNews y ~5 por hora de Slickdeals). Si satura: `/fuente off slickdeals_amazon`. Pendiente que David decida si quiere también las ofertas que DealNews vuelve a destacar (ver gotchas).
- eBay (Browse API): esperar el App ID de David y probarlo antes de diseñar la fuente.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase Postgres, Telegram vía getUpdates (ADR 0001); histórico por segmentos (ADR 0002); disparo externo y modo caliente (ADR 0003); rastreo de catálogo completo (ADR 0004); canales de Telegram como fuente `channel` sin verificar (ADR 0005); feeds de Amazon como canales sin orden de ids (ADR 0006).
- Sin costo puesto ni TRM: las alertas USD dicen «🇺🇸 Compra por casillero». Si spec y plan difieren, manda spec §0.2 y luego el plan.
- La confirmación exige ±1 %, stock, misma variante y mismo vendedor; en búsquedas sin vendedor visible (Amazon, Newegg) la ficha decide.
- Rastreo: descuento solo contra precios observados (nunca `ListPrice`); la alerta exige «antes» estable 24 h y 2 lecturas, confirmación en la ficha y que otra tienda no haya alertado el mismo EAN en 6 h.
- Sin temas guardados todo va a *General* sin aviso; con temas, una alerta cuyo tema falta va a *General* y se avisa una vez al día que corra `/temas`. Con la BD en ≥ 450 MB se pausan las búsquedas y el rastreo de Éxito y Carulla.
- Slickdeals: un solo task `feed` por ciclo, sin reserva de confirmación; el lead se omite si el listing se leyó hace < 6 h. Sus leads a Amazon se descartan con el breaker abierto: los cubre `slickdeals_amazon`.
- No se sortea ningún CAPTCHA ni detección de bots (camelcamelcamel descartado por Cloudflare; Keepa en pausa por costo).

## Pendientes y gotchas
- Supabase: el migrador necesita `GRANT CREATE ON DATABASE postgres TO discountz_owner` y `postgres` debe ser miembro de `discountz_owner`; varias claves erradas dan timeouts pasajeros en el pooler. Postgres en la nube: `service postgresql start` y `ALTER USER postgres PASSWORD 'postgres'`.
- Desde la nube de la sesión: Newegg da 403 CAPTCHA, Best Buy 502/timeout y Amazon con `curl_cffi` el intersticial de Akamai (fixtures con httpx); `*.vtexassets.com` está bloqueado por el proxy (no desde Actions). La búsqueda de Newegg da CAPTCHA también desde la red de David: desactivada, fixture sintética; sus fichas sí responden (buy box de un tercero). `record-fixture` no sigue redirecciones y en Windows escribe el `.meta.json` con CRLF.
- Slickdeals ya no publica `u2=` (usa `u3=` cifrado): solo Amazon (`data-aps-asin`) llega a una tienda. Su fixture de portada es del 2026-10-13. `slickdeals` y `slickdeals_amazon` bajan el mismo RSS por separado (~96 peticiones al día en total).
- DealNews responde `x-dn-is-bot-user-agent: Yes` pero sirve normal (`s-maxage=300`); `robots.txt` pide 2 s entre peticiones. Vuelve a destacar ofertas viejas y vigentes con su `pubDate` original (~13 al día): por el tope de 6 h no llegan. Si el breaker de Amazon se cierra, una oferta puede llegar dos veces (lead verificado y feed sin verificar).
- Fechas del calendario de eventos 2026-2027 estimadas: David debe confirmarlas (Hot Sale y Cyber Lunes de Colombia).
- Falta: `setup-bot`, `pg_terminate_backend` tras 3 `skipped_locked`, descarte G10 contra `R_listing`, mensaje agrupado de tormenta y `config/supabase-ca.crt` para `verify-full`. Una alerta de un ciclo que murió antes de componer sale con vista reducida. En `/estado`, los códigos de más de 14 letras (`tg_descuentostech`, `slickdeals_amazon`) descuadran la columna.
- VTEX: el WAF rechaza `+` como espacio (`infra/http` usa `%20`); una página de catálogo pesa hasta ~7 MB; `products/search` con varios `skuId` devuelve 10 sin `_from`/`_to`.
- Pendientes menores del rastreo: reloj fijo por corrida, `min_seen` sin ventana de 90 d, tope diario por día UTC, orden del barrido VTEX, fotos sin mirar el plazo, C07 sin unir tiendas, desalojo de automáticos por antigüedad, `crawl` sin verificar `SCHEMA_VERSION`, código 3 compartido, borrado del catálogo tras 30 d sin rastreo. Un producto ya seguido que reaparece en Slickdeals con lectura de hace < 6 h no da `COMMUNITY_DEAL`.
- Canales y feeds: un cambio de formato (≥ 5 ilegibles y 0 ofertas, una página sin posts o un feed vacío) cuenta como `PARSE_ERROR` (3 seguidos abren el breaker). Las fixtures de `t.me` llevan los usuarios cambiados por «Usuario»: anonimizar al regrabar. En los temas, «board», «monitor», «mouse», «control» y «switch» mandan a Tecnología títulos en inglés como «Cutting Board».
- ruff 0.17 formatea Markdown: `docs/` está excluido. La imagen `postgres:17` del CI no tiene digest fijado.
