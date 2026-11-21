# 0021. Que la BD y el egress se regulen solos

- Fecha: 2026-11-21
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-11-21-bd-autorregulada-design.md`

## Contexto
David pidió que la BD «nunca vuelva» a llenarse y que no se apague ningún servicio. El 2026-11-21 `catalog_item` tenía 708 mil filas, 160 MB de tabla y 255 MB de índices; `catalog_item_seen_ix` pesaba 125 MB, cuando debería pesar unos 20. Cada lectura del rastreo movía `last_seen_at`. Como es una columna indexada, ninguna actualización era HOT y cada una dejaba entradas muertas en los 7 índices. Borrar filas no baja `pg_database_size`, así que la pausa de 450 MB llevaba desde el 12-nov sin levantarse. El aviso de egress (ADR 0020) pedía apagar el rastreo a mano.

## Decisión
- `save_catalog_page` mueve `last_seen_at` como mucho una vez al día (`CATALOG_SEEN_STEP`). La retención, que cuenta en días, y los pares de modelo de eBay (7 días) toleran ese desfase.
- La migración `0020` reconstruye una sola vez los índices de `catalog_item` (`REINDEX INDEX`, dentro de la transacción del migrador, porque la corre el dueño de las tablas) y agrega `bot_state.crawl_pace_bp`.
- Escalones de tamaño en el mantenimiento:
  - 300 MB: aviso.
  - 400 MB: poda; además, el catálogo no visto se borra a los 14 días en vez de 30.
  - 480 MB: pausa del descubrimiento, como último recurso (antes era a los 450 MB). Se levanta por debajo de 400.
- Ritmo del rastreo:
  - Con la estimación diaria de egress, `crawl_pace_bp = ritmo × 132 ÷ MB del día`, entre 10 % y 100 %.
  - El tope de cada corrida se multiplica por ese ritmo, y el aviso de Telegram dice el ritmo nuevo.
- El tema `ferreteria` queda en `off` por pedido de David.

## Alternativas consideradas
- **REINDEX periódico desde un workflow con la clave del dueño:** recupera espacio sin migraciones, pero saca la clave del migrador de su entorno protegido. El costo de revertir es bajo, pero el riesgo no compensa: descartada.
- **Solo bajar la retención:** no achica el archivo ni detiene la hinchazón.
- **Apagar fuentes al pasar el umbral (lo de antes):** es justo lo que David no quiere.

## Consecuencias
- Si los índices se vuelven a hinchar por otra causa, hace falta otra migración o un `REINDEX` a mano: la app no tiene permiso para hacerlo.
- El ritmo reacciona una vez al día y con un día de retraso. Si el egress que no viene del rastreo pasa la meta, el ritmo baja hasta el 10 % y el aviso sigue saliendo.

## Cuándo revisar
- Se sostiene con (hoy): unos 330 MB de BD estimados tras la reconstrucción, con 708 mil productos.
- Revisar si `catalog_item_seen_ix` vuelve a pasar de 50 MB, si la BD pasa de 450 MB o si `crawl_pace_bp` queda en 1000 más de una semana.
