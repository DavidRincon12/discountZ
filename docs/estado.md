# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`); cron-job.org dispara el ciclo cada ~2 min (medido el 2026-10-19) y el rastreo cada hora.
- Rastreo de catálogo (ADR 0004) en producción desde el 2026-10-17: VTEX ×4, tarjetas PNG; canales de Telegram (ADR 0005) y feeds de Amazon «sin verificar» (ADR 0006/0007) también.
- Best Buy y Walmart fuera por decisión de David. Amazon da CAPTCHA desde Actions: su breaker sigue abierto.
- eBay (`ebay_us`, ADR 0008) en producción desde el 2026-10-18: `/seguir`, `/buscar` y job `crawl` con `MODEL_LOW` (mediana del mismo `epid`). Esquema 6.
- Dos chats: el grupo (todo, con temas apagados) y el canal general (`TELEGRAM_CHANNEL_ID`, solo ofertas). Canales leídos: DescuentosTech, DescuentosHome, Clubgratis, cajitatech y OFERTU (solo con código). Suite: 940 pruebas en verde el 2026-10-20.

## Último cambio
- Rama `claude/project-thread-rlzaur` (auditoría del 2026-10-20): el rastreo repite tras 60 s, hasta 3 veces, la petición que falló por un corte pasajero en vez de cortar la corrida, y registra su `http_status`; cada corrida toma lo que queda del tope del día UTC repartido en las horas que faltan (`CrawlBudget.run_cap`); los canales de Telegram se leen cada 3 min (antes 10).

## Siguiente paso
- Alkosto (T1 de la auditoría): diseño corto para que David lo apruebe, luego spec, ADR y adaptador. Su web usa Algolia con clave pública de solo búsqueda en el HTML (app `QX5IPS1B1Q`, índice `alkostoIndexAlgoliaPRD`, ~14 mil productos); antes, leer sus términos y probar desde Actions.
- Spike del carril «más descuento primero» en VTEX (`O=OrderByBestDiscountDESC`, ordenado contra el `ListPrice` inflado: solo elige qué leer). Luego, confirmar tiendas VTEX nuevas (Jumbo, Metro, Colsubsidio, Decathlon, Pepe Ganga) desde Actions y el peso de la BD.
- David: check de healthchecks.io en el secreto `HEALTHCHECKS_PING_URL`; `/fuente off dealnews` mientras siga bloqueado; revisar el acuerdo de la API de eBay.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase Postgres, Telegram vía getUpdates (ADR 0001); histórico por segmentos (ADR 0002); disparo externo y modo caliente (ADR 0003); rastreo de catálogo completo (ADR 0004); canales de Telegram como fuente `channel` sin verificar (ADR 0005); feeds de Amazon como canales sin orden de ids (ADR 0006), DealNews sin tope de antigüedad (ADR 0007) y eBay por la Browse API con comparación por modelo (ADR 0008).
- Sin costo puesto ni TRM: las alertas USD dicen «🇺🇸 Compra por casillero». Si spec y plan difieren, manda spec §0.2 y luego el plan.
- La confirmación exige ±1 %, stock, misma variante y mismo vendedor; en búsquedas sin vendedor visible (Amazon, Newegg) la ficha decide.
- Rastreo: descuento solo contra precios observados (nunca `ListPrice`); la alerta exige «antes» estable 24 h y 2 lecturas, confirmación en la ficha y que otra tienda no haya alertado el mismo EAN en 6 h.
- Sin temas guardados todo va a *General* sin aviso. Con la BD en ≥ 450 MB se pausan las búsquedas y el rastreo de Éxito, Carulla y eBay.
- Slickdeals: un solo task `feed` por ciclo; sus leads a Amazon se descartan con el breaker abierto: los cubre `slickdeals_amazon`.
- No se sortea ningún CAPTCHA ni detección de bots (camelcamelcamel descartado por Cloudflare; Keepa en pausa por costo; Falabella y Homecenter detrás de Cloudflare).

## Pendientes y gotchas
- BD: una caída del pooler a mitad del rastreo termina la corrida con `db_unavailable` (18-oct 20:42 en las 4 VTEX; 19-oct 11:00 eBay) y la siguiente sigue desde el cursor; falta reconectar dentro de la misma corrida. Supabase: el migrador necesita `GRANT CREATE ON DATABASE postgres TO discountz_owner`. Postgres en la nube: `service postgresql start` y `ALTER USER postgres PASSWORD 'postgres'`.
- `bestbuy_us` sigue en la matriz de `crawl.yml` (lo exige `tests/arch/test_smoke.py`); sin clave sale al instante. El `schedule` de respaldo suma 8 corridas al día: `run_cap` las reparte.
- Desde la nube de la sesión: Newegg da 403 CAPTCHA, Best Buy 502/timeout y Amazon el intersticial de Akamai; `*.vtexassets.com` y los dominios de Jumbo, Metro, Colsubsidio, Decathlon y Pepe Ganga están bloqueados por el proxy (no desde Actions). `record-fixture` no sigue redirecciones y en Windows escribe el `.meta.json` con CRLF.
- DealNews da 406 desde Actions desde el 18-oct (breaker abierto; el código del bloqueo dice `vtex_406`, nombre engañoso). Vuelve a destacar ofertas viejas y vigentes (ADR 0007).
- Ofertas con el tema equivocado: agregar el término a `channel_rules` y la oferta a `tests/fixtures/topics/ofertas-etiquetadas.jsonl` (cada término nombra un solo tema; se mide con `tests/unit/test_topic_precision.py`). Las fixtures de `t.me` llevan los usuarios cambiados por «Usuario».
- Fechas del calendario de eventos 2026-2027 estimadas: David debe confirmarlas (Hot Sale y Cyber Lunes de Colombia).
- Falta: `setup-bot`, `pg_terminate_backend` tras 3 `skipped_locked`, descarte G10 contra `R_listing`, mensaje agrupado de tormenta y `config/supabase-ca.crt` para `verify-full`. En `/estado`, los códigos de más de 14 letras descuadran la columna.
- VTEX: el WAF rechaza `+` como espacio (`infra/http` usa `%20`); una página de 50 productos pesa hasta ~43 MB (se pide por mitades sobre 24 MB); `products/search` con varios `skuId` devuelve 10 sin `_from`/`_to`.
- Pendientes menores del rastreo: reloj fijo por corrida, `min_seen` sin ventana de 90 d, tope diario por día UTC, orden del barrido VTEX, C07 sin unir tiendas, `crawl` sin verificar `SCHEMA_VERSION`, código 3 compartido, borrado del catálogo tras 30 d sin rastreo.
- eBay, pendientes menores: `count(*)` del tope por página, parámetros leídos del TOML al importar el adaptador, 100 auto-seguimientos cada 2 h no caben en 1.000 al día. Vigilar la cuota (`Unavailable quota`) y el peso de la BD; si satura: `/fuente off ebay_us`.
- ruff 0.17 formatea Markdown: `docs/` está excluido. La imagen `postgres:17` del CI no tiene digest fijado.
