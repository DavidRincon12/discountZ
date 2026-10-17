# 0006. Leer las ofertas de Amazon de DealNews y Slickdeals como canales sin orden de ids

- Fecha: 2026-10-17
- Estado: Aceptada
- Diseño corto aprobado por David en el hilo «Puesta en marcha» (sin spec: reutiliza ADR 0005)

## Contexto
Amazon da CAPTCHA desde Actions y su breaker sigue abierto, así que los leads de Slickdeals a Amazon se descartan: un lead solo alerta si se lee la ficha. El RSS de Amazon de DealNews responde desde la nube (43 ofertas, 33 de las últimas 24 h el 2026-10-17) con precio, foto, categoría y cupón; la portada de Slickdeals trae unas 5 ofertas de Amazon por hora. Sus ids no crecen con la publicación: DealNews vuelve a subir ofertas viejas y la portada de Slickdeals promueve hilos creados antes.

## Decisión
- Dos fuentes `channel` más, `dealnews` y `slickdeals_amazon`, por el camino de los canales de Telegram (ADR 0005): duplicados en 24 h, tema por palabras (con palabras en inglés), tarjeta «sin verificar», horas de silencio y resumen diario.
- `SourceDescriptor.ordered_ids = False`: el feed se relee entero sin `after`; lo ya guardado en `channel_post` se salta por id. La primera lectura con alguna oferta legible no envía nada: guarda lo que hay con `channel_post.baseline`, que no cuenta en el resumen ni sirve de original para duplicados. Una primera lectura sin ofertas (feed vacío o formato roto) no fija nada.
- DealNews pide no modificar sus enlaces ni sus códigos de referido y atribuirle el contenido: «Ver en DealNews» lleva el enlace `?iref=rss` tal cual y la tarjeta dice «Visto en DealNews». Un enlace que `SafeUrl` cambiaría al validarlo (quita `utm_*` y `ref`, recodifica la query) no se usa. El id de la oferta sale del `<guid>`: en las ofertas «product», el `<link>` lleva el id del producto. `robots.txt` pide 2 s entre peticiones: una sola por lectura.
- Un ítem ilegible (sin id, enlace, hilo, título o fecha) cuenta como formato roto y un feed sin ítems es un `ParseError`: como en los canales, el breaker pausa la fuente y `/estado` la muestra rota.
- `slickdeals_amazon` no tiene hosts: los enlaces a slickdeals.net siguen siendo de `slickdeals`.

## Alternativas consideradas
- Dejar las ofertas de Amazon en el flujo de leads de Slickdeals: verifica el precio en la ficha · es justo lo que Amazon bloquea · descartada.
- Cursor por fecha de publicación en vez de ids guardados: no necesita filas · pierde los hilos viejos que la portada de Slickdeals promueve después · descartada.
- Marcar la primera lectura con un valor especial en `duplicate_of`: sin cambio de esquema · mezcla dos significados en una columna · descartada.
- camelcamelcamel: desafío de Cloudflare incluso en el feed · descartada. Keepa: de pago · en pausa por decisión de David.

## Consecuencias
- Ofertas de Amazon sin leer Amazon, marcadas «sin verificar» en cada tarjeta.
- Hasta ~140 peticiones al día a DealNews y ~48 a Slickdeals, con breaker y presupuesto propios. `slickdeals` (leads) baja el mismo RSS por su lado, sin ritmo compartido: ~96 al día en total a slickdeals.net.
- Como en los canales, no se envía lo publicado hace más de 6 h. DealNews vuelve a destacar ofertas viejas y vigentes con su `pubDate` original (6 de 39 el 2026-10-17, unas 13 al día): esas no llegan.
- Si el breaker de Amazon se cierra algún día, la misma oferta puede llegar dos veces: verificada por el lead y sin verificar por el canal.
- Se apagan con `/fuente off dealnews` o `/fuente off slickdeals_amazon`.

## Cuándo revisar
- Se sostiene con (hoy): ~33 ofertas al día de DealNews y ~5 por hora de Slickdeals a Amazon, 0 bloqueos.
- Revisar si: las tarjetas de los feeds pasan de 250 al día durante una semana (el estimado es ~150), DealNews empieza a bloquear o cambia sus términos, o el breaker de Amazon se cierra.
