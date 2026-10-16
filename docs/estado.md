# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`, cron-job.org cada 5 min).
- Rastreo de catálogo (spec `docs/superpowers/specs/2026-10-14-rastreo-catalogo-design.md`, plan `docs/superpowers/plans/2026-10-14-rastreo-catalogo.md`, Tasks 1-15) implementado en la rama `claude/discountz-puesta-en-marcha-mlcmm6`:
  - Migración `0002_catalogo` (`SCHEMA_VERSION = 2`): `catalog_item`, `catalog_price_change`, `crawl_cursor`, `crawl_lease`, `topic`; `outbox.topic/card`; `watch.origin/expires_at`.
  - `python -m discountz crawl --source <tienda>` y workflow `crawl.yml` (matriz exito, carulla, olimpica, panamericana, bestbuy_us) detrás de `DISCOUNTZ_RASTREO=true`.
  - VTEX ×4 por categoría raíz con bandas de precio; Best Buy por su API oficial (`BESTBUY_API_KEY`); Walmart lista para su API de afiliados (4 secretos).
  - Alertas del catálogo como tarjeta PNG con foto en el tema de su categoría; las de la v1 también van a su tema (Slickdeals a «Comunidad EE. UU.»). Comandos `/temas`, `/seguir` hasta 20 URLs, `/dejar aNN|auto`.
  - Seguimientos automáticos desde Slickdeals (Amazon 300, Newegg 100), mantenimiento y resumen diario del catálogo.
- Suite: 605 pruebas (unitarias + integración) en verde el 2026-10-16.

## Último cambio
- 2026-10-16: Tasks 11-15 del rastreo: entrega con foto y tema, comandos, seguimientos automáticos, mantenimiento, CLI `crawl`, workflow y guía de activación (README §9).

## Siguiente paso
- Fusionar el PR del rastreo y correr enseguida *migrate* (`to = head`): hasta entonces el ciclo se salta por versión de esquema.
- David sigue README §9: grupo con temas, `TELEGRAM_CHAT_ID` del grupo, `/temas`, `BESTBUY_API_KEY` + `/fuente on bestbuy_us`, `DISCOUNTZ_RASTREO=true` y el disparo horario de `crawl.yml`.
- Con la clave de Best Buy: grabar fixtures reales de la API (catálogo, cambios, ficha) y revisar las reglas de `topics.toml` contra sus categorías reales.
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
- `*.vtexassets.com` (fotos VTEX) está bloqueado por el proxy de la sesión en la nube, no desde Actions. Las fixtures de las API de Best Buy y Walmart son sintéticas (`"nota"`).
- Un producto que ya se sigue (manual o automático) y reaparece en Slickdeals con lectura de hace < 6 h no da `COMMUNITY_DEAL` (regla de la v1).
- ruff 0.17 formatea Markdown: `docs/` está excluido. La imagen `postgres:17` del CI no tiene digest fijado.
