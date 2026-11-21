# BD que se cuida sola (espacio y egress)

- Fecha: 2026-11-21
- Pedido de David: «que nunca vuelva a ocurrir… que se vayan borrando datos cada cierto umbral para evitar apagar cualquier servicio», y quitar las ofertas de herramientas y ferretería (Homecenter y otras).
- Diseño aprobado en el hilo («procede a eso»).

## Contexto (medido el 2026-11-21)
- `catalog_item`: 708 mil filas, 160 MB de tabla y 255 MB de índices. `catalog_item_seen_ix` pesa 125 MB (debería pesar unos 20). Causa: `save_catalog_page` reescribe `last_seen_at` en cada lectura; como es una columna indexada, ninguna actualización es HOT y cada una deja entradas muertas en los 7 índices.
- Borrar filas no baja `pg_database_size`, así que la pausa de 450 MB (desde el 12-nov) no se levanta sola.
- El aviso de egress (ADR 0020) le pide a David que apague el rastreo.

## Metas y no metas
- Meta: que la BD y el egress se regulen solos, con la pausa solo como último recurso, y que no lleguen ofertas de ferretería.
- No meta: dar al rol de la app permisos de dueño (el `REINDEX` automático necesitaría ser dueño de las tablas).

## Enfoques
- **Cortar la hinchazón en el origen + reconstrucción única + escalones + ritmo del rastreo (elegido).** Ataca la causa medida; la reconstrucción va en una migración porque la corre el dueño.
- **REINDEX periódico desde un workflow con la clave del dueño:** saca la clave del migrador de su entorno protegido. Descartado.
- **Solo bajar la retención:** borrar no achica el archivo ni detiene la hinchazón. Descartado.

## Diseño
1. **Ferretería apagada:** `topics.ferreteria.mode = "off"`: el rastreo deja de pedir sus particiones y los canales dejan sus posts vistos sin enviar. Los intereses (🎯) siguen pasando.
2. **«Visto» una vez al día:** `save_catalog_page` solo mueve `last_seen_at` si avanza 1 día o más (`CATALOG_SEEN_STEP`). La retención (30 d), los pares de modelo de eBay (7 d) y la relectura (usa `checked_at`) toleran ese día de error.
3. **Migración `0020`:** `REINDEX INDEX` de los índices de `catalog_item` (sin `CONCURRENTLY`, que no corre en la transacción del migrador; bloquea las escrituras del catálogo uno o dos minutos), con su propio `statement_timeout`. Agrega `bot_state.crawl_pace_bp smallint NOT NULL DEFAULT 10000 CHECK (1000..10000)`.
4. **Escalones por tamaño en el mantenimiento:** 300 MB aviso; 400 MB poda de descubrimientos y segmentos (como hoy) y el catálogo no visto se borra a los 14 días en vez de 30; la pausa del descubrimiento pasa de 450 a 480 MB y se levanta por debajo de 400.
5. **Ritmo del rastreo:** con la estimación diaria de egress, el mantenimiento ajusta `crawl_pace_bp` = ritmo × meta ÷ MB del día (meta = 80 % de 5 GB ÷ 31 ≈ 132 MB), entre 10 % y 100 %; sin estimación no cambia. Cada corrida del rastreo multiplica su tope de peticiones por ese ritmo. El aviso de Telegram dice el ritmo nuevo en vez de pedir que se apague.
6. **Docs:** ADR 0021, `CLAUDE.md` y `docs/estado.md`.

## A cargo del implementador
- Nombres y forma de las pruebas; el texto de los avisos.
