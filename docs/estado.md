# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`); cron-job.org dispara el ciclo cada ~2 min (medido el 2026-10-19) y el rastreo cada hora.
- Rastreo de catálogo (ADR 0004) en producción desde el 2026-10-17: VTEX ×4, tarjetas PNG; canales de Telegram (ADR 0005) y feeds de Amazon «sin verificar» (ADR 0006/0007) también.
- Best Buy y Walmart fuera por decisión de David. Amazon da CAPTCHA desde Actions: su breaker sigue abierto.
- eBay (`ebay_us`, ADR 0008) en producción desde el 2026-10-18: `/seguir`, `/buscar` y job `crawl` con `MODEL_LOW` (mediana del mismo `epid`).
- Alkosto (`alkosto`, ADR 0009) en producción desde el 2026-10-20 (PR #21, esquema 7).
- Jumbo y Pepe Ganga (VTEX, migración 0008) en producción desde el 2026-10-20 (PR #22, esquema 8): `/seguir`, `/buscar`, canario y `crawl`.
- Dos chats: el grupo (todo, con temas apagados) y el canal general (`TELEGRAM_CHANNEL_ID`, solo ofertas). Canales leídos: DescuentosTech, DescuentosHome, Clubgratis, cajitatech y OFERTU (solo con código). Suite: 891 unitarias y 190 de integración en verde el 2026-10-20.

## Último cambio
- Rama `claude/project-thread-tl4xoa`: el evento `maintenance` (08:00 UTC) registra `db_mb`, el peso de la BD, para vigilar el tope de 450 MB desde los logs de Actions; la imagen `postgres:17` del CI queda fijada por digest. PR #18, #28 y #29 ya fusionados; `setup-bot` corrido; el resumen diario del 2026-10-21 salió sin `maintenance_failed`.

## Siguiente paso
- G10 en `/buscar`: descartar el lote constante contra `R_listing` (`check_batch` existe en `domain/guards.py` pero nadie lo llama; hoy el lote constante no alerta pero entra al histórico). Prueba: búsqueda de 5 ofertas al mismo precio con 3 listings con histórico a > 30 % → sin segmentos nuevos y `parse_errors=1`.
- David: código de afiliado de Amazon cuando lo tenga. Hot Sale 2027 sin fecha publicada por la CCCE (revisado 2026-10-21).

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase Postgres, Telegram vía getUpdates (ADR 0001); histórico por segmentos (ADR 0002); disparo externo y modo caliente (ADR 0003); rastreo de catálogo completo (ADR 0004); canales de Telegram como fuente `channel` sin verificar (ADR 0005); feeds de Amazon como canales sin orden de ids (ADR 0006), DealNews sin tope de antigüedad (ADR 0007) eBay por la Browse API con comparación por modelo (ADR 0008) y Alkosto por su índice de Algolia, asumiendo el riesgo de sus términos (ADR 0009); sin carril por descuento en VTEX porque ordena por el `ListPrice` inflado (ADR 0010); enlace de la tienda sin referido en los canales con `joylink.io` (ADR 0011; cajitatech, DescuentosTech y DescuentosHome no se pueden).
- Sin costo puesto ni TRM: las alertas USD dicen «🇺🇸 Compra por casillero». Si spec y plan difieren, manda spec §0.2 y luego el plan.
- La confirmación exige ±1 %, stock, misma variante y mismo vendedor; en búsquedas sin vendedor visible (Amazon, Newegg) la ficha decide.
- Rastreo: descuento solo contra precios observados (nunca `ListPrice`); la alerta exige «antes» estable 24 h y 2 lecturas, confirmación en la ficha y que otra tienda no haya alertado el mismo EAN en 6 h.
- Sin temas guardados todo va a *General* sin aviso. Con la BD en ≥ 450 MB se pausan las búsquedas y el rastreo de Éxito, Carulla, eBay, Alkosto y Jumbo.
- Slickdeals: un solo task `feed` por ciclo; sus leads a Amazon se descartan con el breaker abierto: los cubre `slickdeals_amazon`.
- No se sortea ningún CAPTCHA ni detección de bots (camelcamelcamel descartado por Cloudflare; Keepa en pausa por costo; Falabella y Homecenter detrás de Cloudflare).

## Pendientes y gotchas
- BD: una caída del pooler a mitad del rastreo reconecta (log `db_reconnect`); a la tercera caída la corrida termina con `db_unavailable` y la siguiente sigue desde el cursor. El ciclo (`run`) no reconecta: dura ~1 min y el siguiente arranca a los 2. Supabase: el migrador necesita `GRANT CREATE ON DATABASE postgres TO discountz_owner`. Postgres en la nube: `service postgresql start` y `ALTER USER postgres PASSWORD 'postgres'`.
- `bestbuy_us` sigue en la matriz de `crawl.yml` (lo exige `tests/arch/test_smoke.py`); sin clave sale al instante. El `schedule` de respaldo suma 8 corridas al día: `run_cap` las reparte.
- Desde la nube de la sesión: Newegg da 403 CAPTCHA, Best Buy 502/timeout y Amazon el intersticial de Akamai; `*.vtexassets.com` y los dominios de Jumbo y Pepe Ganga están bloqueados por el proxy (no desde Actions): sus fixtures se graban con un workflow temporal que las sube a la rama. `record-fixture` no sigue redirecciones y en Windows escribe el `.meta.json` con CRLF.
- DealNews: apagado por David (`/fuente off dealnews`) porque da 406 desde Actions desde el 18-oct (el código dice `vtex_406`, nombre engañoso).
- Ofertas con el tema equivocado: agregar el término a `channel_rules` y la oferta a `tests/fixtures/topics/ofertas-etiquetadas.jsonl` (cada término nombra un solo tema; se mide con `tests/unit/test_topic_precision.py`). Las fixtures de `t.me` llevan los usuarios cambiados por «Usuario».
- Calendario CO según la CCCE: Hot Sale 16-23 oct 2026 (con Drops); Cyberlunes ya no existe. Falta el Hot Sale de 2027 (marzo y octubre, sin fecha publicada). Black Days y los eventos de EE. UU. siguen estimados.
- Términos de la API de eBay (revisados 2026-10-20): §9.5 prohíbe usar su contenido para «modelar precios» (roza `MODEL_LOW`) y §3.1(b)/§9.7 solo permiten copias intermedias (roza el histórico de `catalog_item`). David decidió dejarlo igual (uso personal; el riesgo es que revoquen las claves).
- Falta: `pg_terminate_backend` tras 3 `skipped_locked` (antes separar el `application_name` del ciclo y del rastreo, que hoy comparten `discountz-<run_id>`). Certificado de Supabase descartado: se queda `sslmode=require`.
- Resumen diario (13:00Z): `catalog_daily_stats` pasa del `statement_timeout` de 15 s algunos días (`maintenance_failed error=QueryCanceled`); se reintenta y sale a las 13:02. El peso sale en el log `maintenance db_mb=` de las 08:00 UTC. Si el resumen empieza a fallar seguido, contar `catalog_item` con estimación o índice.
- VTEX: el WAF rechaza `+` como espacio (`infra/http` usa `%20`); una página de 50 productos pesa hasta ~43 MB (se pide por mitades sobre 24 MB); `products/search` con varios `skuId` devuelve 10 sin `_from`/`_to`.
- Pendientes menores del rastreo: reloj fijo por corrida, tope diario por día UTC, orden del barrido VTEX (sin `O=`: sin medir si salta productos), C07 sin unir tiendas. Códigos de salida: 3 BD, 4 rastreo bloqueado.
- eBay, pendientes menores: parámetros leídos del TOML al importar el adaptador. Los seguimientos automáticos usan como mucho la mitad del tope diario (`test_auto_watches_leave_half_the_daily_budget_for_the_user`). Vigilar la cuota (`Unavailable quota`) y el peso de la BD; si satura: `/fuente off ebay_us`.
