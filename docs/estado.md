# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`, cron-job.org cada 5 min).
- Rastreo de catálogo (ADR 0004, PR #6 y #7) en producción desde el 2026-10-17: migración `0002`, grupo con 14 temas, `DISCOUNTZ_RASTREO=true`, disparo horario. VTEX ×4 por categoría raíz; tarjetas PNG en el tema de su categoría; `/temas`, `/seguir` hasta 20 URLs, `/dejar aNN|auto`; seguimientos automáticos desde Slickdeals.
- Best Buy y Walmart fuera por decisión de David (2026-10-17): sin `BESTBUY_API_KEY` y `/fuente off walmart_us`. El código sigue en el repo.
- SP-6 canales de Telegram (spec `docs/superpowers/specs/2026-10-17-canales-telegram-design.md`, plan `docs/superpowers/plans/2026-10-17-canales-telegram.md`, Tasks 1-6 hechas en Native): migración `0003_canales` (`SCHEMA_VERSION = 3`, `channel_cursor`, `channel_post`), adaptador `tg_descuentostech`, `tg_clubgratis` y `tg_cajitatech` (`kind = "channel"`), duplicados entre canales en 24 h, tema por hashtags y palabras del título, tarjeta «sin verificar» y línea «Canales» en el resumen diario.
- Suite: 684 pruebas (unitarias + integración) en verde el 2026-10-17.

## Último cambio
- Rama `claude/discountz-puesta-en-marcha-mlcmm6`: SP-6. El ciclo de 5 min lee cada canal como mucho cada 10 min (hasta 3 páginas de `t.me/s/<canal>?after=<id>`), guarda los posts y encola las ofertas nuevas en `outbox` con prioridad 9 y `dedup_key = canal:<código>:<id>`. ADR 0005.

## Siguiente paso
- Al fusionar: *Actions → migrate* con `to = head` (sin la `0003` el ciclo se salta con aviso). La primera lectura de cada canal solo fija el cursor; vigilar que en las siguientes lleguen tarjetas a sus temas y que `/estado` no marque los canales como rotos.
- eBay (Browse API): esperar el App ID de David y probarlo antes de diseñar la fuente.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase Postgres, Telegram vía getUpdates (ADR 0001); histórico por segmentos (ADR 0002); disparo externo y modo caliente (ADR 0003); rastreo de catálogo completo por corridas horarias (ADR 0004); canales de Telegram como fuente `channel` sin verificar (ADR 0005).
- Sin costo puesto ni TRM: las alertas USD dicen «🇺🇸 Compra por casillero».
- Si spec y plan difieren, manda spec §0.2 y luego el plan.
- La confirmación exige ±1 %, stock, misma variante y mismo vendedor; en búsquedas sin vendedor visible (Amazon, Newegg) el vendedor es provisional y la ficha decide.
- Rastreo: el descuento se mide solo contra precios observados (nunca `ListPrice`); una alerta exige precio «antes» estable 24 h y 2 lecturas, confirmación en la ficha y que otra tienda no haya alertado el mismo EAN en 6 h.
- Sin temas guardados todo va a *General* sin aviso; con temas, una alerta cuyo tema falta va a *General* y se avisa una vez al día que corra `/temas`.
- Con la BD en ≥ 450 MB se pausan las búsquedas y el rastreo de Éxito y Carulla (misma marca `discovery_paused`).
- Slickdeals: un solo task `feed` por ciclo para todas sus búsquedas, sin reserva de confirmación; el lead se lee en el SourceRun de la tienda (se crea si no tenía tareas) y se omite si el listing se leyó hace < 6 h.

## Pendientes y gotchas
- Supabase: el migrador necesita `GRANT CREATE ON DATABASE postgres TO discountz_owner` (el `CREATE SCHEMA IF NOT EXISTS` revisa el permiso antes que la existencia) y `postgres` debe ser miembro de `discountz_owner` para cederle el esquema. Varios intentos con clave errada pueden dar timeouts pasajeros en el pooler.
- Postgres en la nube: `service postgresql start` y, si pide clave, `ALTER USER postgres PASSWORD 'postgres'`.
- Desde la nube de la sesión: Newegg da 403 CAPTCHA (httpx y curl_cffi) y las fichas de Best Buy 502/timeout; Amazon con `curl_cffi` recibe el intersticial de Akamai (fixtures con httpx).
- La búsqueda de Newegg (`/p/pl?d=…`) da 403 CAPTCHA también desde la red de David, con ambos clientes: está desactivada y su fixture sigue sintética. Las fichas sí responden (buy box de un tercero: el vendedor de un Item# no es fijo). No intentar sortear el CAPTCHA. `record-fixture` no sigue redirecciones y en Windows escribe el `.meta.json` con CRLF.
- Slickdeals ya no publica `u2=` (usa `u3=` cifrado): hoy solo los leads de Amazon (`data-aps-asin`) llegan a una tienda.
- Fechas del calendario de eventos 2026-2027 estimadas: David debe confirmarlas (Hot Sale y Cyber Lunes de Colombia, sobre todo).
- Falta: `setup-bot`, `pg_terminate_backend` tras 3 `skipped_locked`, descarte G10 contra `R_listing`, mensaje agrupado de tormenta y `config/supabase-ca.crt` para `sslmode=verify-full`.
- Una alerta de un ciclo que murió antes de componer sale con vista reducida (sin línea de histórico).
- VTEX: el WAF responde 400 «Scripts are not allowed!» si el espacio va como `+` en `fq=P:[a TO b]`; `infra/http` codifica con `%20`. Una página de catálogo de 50 productos pesa hasta ~7 MB en Olímpica (tope del rastreo: `CATALOG_MAX_BODY_BYTES`, 24 MB). `products/search` con varios `fq=skuId:` devuelve solo 10 productos sin `_from`/`_to` (medido 2026-10-17): los lotes de más de 10 piden el rango. Afectaba también a los seguimientos de la v1.
- Pendientes menores de la revisión del rastreo: reloj fijo en toda la corrida, `min_seen` sin ventana de 90 d, tope diario por día UTC, orden del barrido VTEX, fotos sin mirar el plazo, C07 sin unir tiendas, desalojo de automáticos por antigüedad, `crawl` sin verificar `SCHEMA_VERSION`, código 3 compartido, borrado del catálogo tras 30 d sin rastreo.
- `*.vtexassets.com` (fotos VTEX) está bloqueado por el proxy de la sesión en la nube, no desde Actions. Las fixtures de las API de Best Buy y Walmart son sintéticas (`"nota"`). Un producto que ya se sigue (manual o automático) y reaparece en Slickdeals con lectura de hace < 6 h no da `COMMUNITY_DEAL` (regla de la v1).
- Canales: el formato de `t.me/s` puede cambiar sin aviso; una página sin posts ni marca de vacío, o con ≥ 5 precios y 0 ofertas, cuenta como `PARSE_ERROR` (3 seguidos abren el breaker). Los nombres de usuario de las fixtures se cambiaron por «Usuario».
- ruff 0.17 formatea Markdown: `docs/` está excluido. La imagen `postgres:17` del CI no tiene digest fijado.
