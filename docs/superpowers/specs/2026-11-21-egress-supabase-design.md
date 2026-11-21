# Bajar el egress de Supabase y vigilarlo

- Fecha: 2026-11-21
- Pedido de David: «arréglalo de la mejor forma que creas pertinente y haz algo para que no se vuelva a llenar tanto».

## Contexto (medido el 2026-11-21)
- El plan gratis da 5 GB de egress al mes (ciclo del 28 al 28). Este ciclo va en 11,48 GB: unos 650-700 MB al día hasta el 5-nov y unos 280 MB al día después. El ciclo anterior también se pasó, y Supabase avisa que restringe el proyecto desde el 23-nov. La BD pesa 482 MB de 500.
- Según `pg_stat_statements` (desde el 14-oct, rol `discountz_app`), las filas leídas vienen de:
  - `load_catalog_states`: 49,5 M filas, en texto (cuatro `timestamptz` de unos 29 bytes cada uno).
  - `recent_channel_posts`: 15 M filas. Se relee en cada ciclo aunque no llegue ningún post nuevo.
  - `held_offers`: 10,8 M filas, por el resumen de la noche que no sale.
  - Otras: 9 M en la capa caliente y 6 M en `channel_post_ids`.
- Dos bugs que se suman:
  1. **Resumen de la noche roto:** `render_held_digest` corta por 4.096 caracteres visibles, pero el HTML de un mensaje pasa de 8.000 (`outbox.body_html` CHECK) por los `href`. Cada ciclo después de las 21:00 relee las ofertas retenidas y falla con `maintenance_failed error=CheckViolation` (log del 15-nov a las 02:18Z). No hay ningún `digest` en `outbox` desde antes del 7-nov.
  2. **Mantenimiento roto desde el 19-nov:** `apply_retention` borra la `outbox` de más de 30 días, la FK `alert.outbox_id … ON DELETE SET NULL` deja alertas en `queued` sin `outbox_id` y eso viola `alert_queued_ck`. Hay 1.385 alertas en ese caso; la más vieja apunta a una `outbox` del 19-oct. Como el mantenimiento falla, `_daily` se salta también el resumen diario y el de la noche, y el mantenimiento se reintenta en cada ciclo.

## Metas y no metas
- Meta: quedar por debajo de unos 160 MB al día (5 GB entre 31 días) y que un exceso se vea en Telegram antes de que Supabase restrinja.
- No meta: pagar el plan Pro, cambiar la frecuencia del ciclo o del rastreo en cron-job.org (es una decisión de David) o reescribir el rastreo.

## Enfoques
- **Recortar en el código lo que se lee de más, más un medidor diario (elegido).** Cambio chico, sin dependencias nuevas, y ataca lo medido.
- **Medir el egress real con la API de gestión de Supabase:** la API pública no tiene un endpoint de egress (solo `usage.api-counts`), y pediría un token personal con acceso a toda la cuenta. Descartado.
- **Contar bytes en el cliente (psycopg):** psycopg no expone los bytes recibidos y habría que recorrer cada celda. Descartado.

## Diseño
1. **Migración `0019`:** `alert_queued_ck` pasa a `outbox_id IS NULL OR delivery = 'queued'`, porque la `outbox` se purga a los 30 días y la alerta conserva `queued`. No se toca `delivery`: con `'new'` se reenviaría. También agrega a `bot_state` las columnas `select_rows_total bigint` y `select_rows_at timestamptz`.
2. **`_daily`:** cada paso (mantenimiento, resumen diario, resumen de la noche) va en su propio `try`, para que la falla de uno no bloquee a los demás.
3. **`render_held_digest`:** corta el mensaje también cuando el HTML pasaría de `MAX_BODY_HTML = 8000`.
4. **Canales:** `recent_channel_posts` se lee la primera vez que un post la necesita (`find_duplicate`), no al empezar.
5. **Rastreo:** `load_catalog_states` pide el resultado en binario (`binary=True`): cada fila pesa cerca de un 40 % menos y se lee igual.
6. **Medidor:** el mantenimiento suma las filas de los `SELECT`/`WITH` del rol en `pg_stat_statements` (sin la extensión devuelve `None`) y las compara con la lectura anterior guardada en `bot_state`. Lo pasa a MB al día con 170 bytes por fila (11,48 GB entre las filas del ciclo), registra `egress_mb=` y avisa en Telegram (prioridad 0, uno por día) si a ese ritmo el mes pasa del 80 % de 5 GB. Si el total bajó (estadísticas reiniciadas) o pasaron menos de 12 h, no estima.
7. **Docs:** ADR 0020, una línea en `CLAUDE.md` y `docs/estado.md`.

## A cargo del implementador
- Nombres y la forma exacta de las pruebas.
- El texto del aviso: debe decir el ritmo, la proyección contra 5 GB y qué hacer.
