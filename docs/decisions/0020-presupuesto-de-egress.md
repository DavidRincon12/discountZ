# 0020. Cuidar el egress de Supabase y medirlo cada día

- Fecha: 2026-11-21
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-11-21-egress-supabase-design.md`

## Contexto
El plan gratis de Supabase da 5 GB de egress al mes (ciclo del 28 al 28), y cuenta todo lo que la BD envía, también por el pooler. Del 28-oct al 21-nov se gastaron 11,48 GB, y el ciclo anterior también se pasó: Supabase anunció que restringe el proyecto desde el 23-nov. Según `pg_stat_statements`, lo más pesado era:
- `load_catalog_states` del rastreo: 49,5 M filas en texto.
- Los posts de canales de 24 h: 15 M filas, releídos en cada ciclo.
- El resumen de la noche: 10,8 M filas. Se reintentaba en cada ciclo porque su HTML pasaba el CHECK de 8.000 caracteres.

Además, el mantenimiento fallaba desde el 19-nov (`alert_queued_ck` contra la purga de la `outbox`) y nadie lo vio: el log decía `maintenance_failed` y Actions seguía en verde.

## Decisión
- Leer menos:
  - `load_catalog_states` va en binario.
  - Los posts recientes de los canales se leen solo cuando un post nuevo los necesita.
  - El resumen se corta también por el largo del HTML.
- Los pasos diarios (mantenimiento, resumen diario y resumen de la noche) van en `try` separados.
- `alert_queued_ck` pasa a `outbox_id IS NULL OR delivery = 'queued'` (migración `0019`). La alerta enviada conserva `queued` cuando su `outbox` se purga.
- **Medidor diario:** el mantenimiento suma las filas de los `SELECT`/`WITH` del rol `discountz_app` en `pg_stat_statements` y las compara con la lectura anterior (`bot_state.select_rows_*`). Estima MB al día con 170 bytes por fila, lo registra como `egress_mb=` y avisa en Telegram si un mes de 31 días pasaría del 80 % de 5 GB (desde 133 MB al día).
- El rol necesita `GRANT USAGE ON SCHEMA extensions TO discountz_app`. Sin ese permiso, el medidor devuelve `None` y no avisa.

## Alternativas consideradas
- **API de gestión de Supabase:** no publica el egress (solo `usage.api-counts`) y exigiría un token personal con acceso a toda la cuenta.
- **Contar bytes en psycopg:** no expone los bytes recibidos; habría que recorrer cada celda de cada consulta.
- **Plan Pro (US$25 al mes):** rompe la meta de costo $0.

## Consecuencias
- El medidor es una estimación: cuenta filas, no bytes. Los 170 bytes por fila salen del ciclo medido y el binario los baja, así que tiende a sobrestimar. Eso sirve de margen.
- Las estadísticas de `pg_stat_statements` se reinician con un reinicio de la BD; ese día no hay estimación.

## Cuándo revisar
- Se sostiene con (hoy): unos 280 MB al día antes del arreglo, con el rastreo de las tiendas grandes en pausa por los 450 MB.
- Revisar si `egress_mb=` pasa de 133 en dos mantenimientos seguidos, o si el panel de uso de Supabase y la estimación difieren en más de un 30 %.
