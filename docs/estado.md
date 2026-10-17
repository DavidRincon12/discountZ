# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`, cron-job.org cada 5 min).
- Rastreo de catálogo (spec `docs/superpowers/specs/2026-10-14-rastreo-catalogo-design.md`, plan `docs/superpowers/plans/2026-10-14-rastreo-catalogo.md`, Tasks 1-15) fusionado (PR #6) y en producción desde el 2026-10-17 (migración `0002` aplicada, grupo con 14 temas, `DISCOUNTZ_RASTREO=true`, disparo horario en cron-job.org):
  - Migración `0002_catalogo` (`SCHEMA_VERSION = 2`): `catalog_item`, `catalog_price_change`, `crawl_cursor`, `crawl_lease`, `topic`; `outbox.topic/card`; `watch.origin/expires_at`.
  - `python -m discountz crawl --source <tienda>` y workflow `crawl.yml` (matriz exito, carulla, olimpica, panamericana, bestbuy_us) detrás de `DISCOUNTZ_RASTREO=true`.
  - VTEX ×4 por categoría raíz con bandas de precio; Best Buy por su API oficial (`BESTBUY_API_KEY`); Walmart lista para su API de afiliados (4 secretos).
  - Alertas del catálogo como tarjeta PNG con foto en el tema de su categoría; las de la v1 también van a su tema (Slickdeals a «Comunidad EE. UU.»). Comandos `/temas`, `/seguir` hasta 20 URLs, `/dejar aNN|auto`.
  - Seguimientos automáticos desde Slickdeals (Amazon 300, Newegg 100), mantenimiento y resumen diario del catálogo.
- Best Buy y Walmart fuera por decisión de David (2026-10-17): sin `BESTBUY_API_KEY` y `/fuente off walmart_us`. El código sigue en el repo.
- Suite: 616 pruebas (unitarias + integración) en verde el 2026-10-17.

## Último cambio
- 2026-10-17: arreglo del primer rastreo en producción. Olímpica y Panamericana guardaban 0 productos y Éxito se cortaba: el filtro de precio `P:[a TO b]` iba con `+` y el WAF de VTEX respondía 400, y las páginas de 50 productos pesan hasta ~7 MB (tope de 5 MB). Ahora los parámetros van con `%20` y el rastreo acepta cuerpos de hasta 24 MB.

## Siguiente paso
- Vigilar las primeras vueltas de *crawl* (estado `ok` y `count` > 0 en las cuatro tiendas) y el resumen diario del catálogo.
- Después: SP-6 (canales comunitarios de Telegram: DescuentosTech, Clubgratis, cajitatech).

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase Postgres, Telegram vía getUpdates (ADR 0001); histórico por segmentos (ADR 0002); disparo externo y modo caliente (ADR 0003); rastreo de catálogo completo por corridas horarias (ADR 0004).
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
- La búsqueda de Newegg (`/p/pl?d=…`) da 403 CAPTCHA también desde la red de David, con ambos clientes: está desactivada y su fixture sigue sintética. Las fichas sí responden. No intentar sortear el CAPTCHA.
- Las dos fichas reales de Newegg (`N82E…`) tienen el buy box de un tercero (`m`): el vendedor de un Item# no es fijo. `record-fixture` no sigue redirecciones y en Windows escribe el `.meta.json` con CRLF.
- Slickdeals ya no publica `u2=` (usa `u3=` cifrado): hoy solo los leads de Amazon (`data-aps-asin`) llegan a una tienda.
- Best Buy: el HTML de la búsqueda no trae precios (GraphQL) y el «$49.99» es la membresía Plus. Walmart ya no trae JSON-LD Product: su ruta independiente es el precio héroe.
- Fechas del calendario de eventos 2026-2027 estimadas: David debe confirmarlas (Hot Sale y Cyber Lunes de Colombia, sobre todo).
- Falta: `setup-bot`, `pg_terminate_backend` tras 3 `skipped_locked`, descarte G10 contra `R_listing`, mensaje agrupado de tormenta y `config/supabase-ca.crt` para `sslmode=verify-full`.
- Una alerta de un ciclo que murió antes de componer sale con vista reducida (sin línea de histórico).
- VTEX: el WAF responde 400 «Scripts are not allowed!» si el espacio va como `+` en `fq=P:[a TO b]`; `infra/http` codifica con `%20`. Una página de catálogo de 50 productos pesa hasta ~7 MB en Olímpica (tope del rastreo: `CATALOG_MAX_BODY_BYTES`, 24 MB).
- VTEX `products/search` con varios `fq=skuId:` devuelve solo 10 productos sin `_from`/`_to` (medido 2026-10-17): los lotes de más de 10 piden el rango. Afectaba también a los seguimientos de la v1.
- Pendientes menores de la revisión del rastreo: reloj fijo en toda la corrida, `min_seen` sin ventana de 90 d, tope diario por día UTC, orden del barrido VTEX, fotos sin mirar el plazo, C07 sin unir tiendas, desalojo de automáticos por antigüedad, `crawl` sin verificar `SCHEMA_VERSION`, código 3 compartido, borrado del catálogo tras 30 d sin rastreo.
- `*.vtexassets.com` (fotos VTEX) está bloqueado por el proxy de la sesión en la nube, no desde Actions. Las fixtures de las API de Best Buy y Walmart son sintéticas (`"nota"`).
- Un producto que ya se sigue (manual o automático) y reaparece en Slickdeals con lectura de hace < 6 h no da `COMMUNITY_DEAL` (regla de la v1).
- ruff 0.17 formatea Markdown: `docs/` está excluido. La imagen `postgres:17` del CI no tiene digest fijado.
