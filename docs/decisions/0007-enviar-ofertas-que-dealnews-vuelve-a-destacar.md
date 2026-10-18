# 0007. Enviar las ofertas vigentes que DealNews vuelve a destacar

- Fecha: 2026-10-17
- Estado: Aceptada
- Reemplaza la consecuencia de la ADR 0006 sobre las ofertas viejas de DealNews («esas no llegan»). David respondió «Sí, enviarlas» en el hilo «Puesta en marcha».

## Contexto
DealNews vuelve a subir a su feed ofertas vigentes con su `pubDate` original: en la grabación del 2026-10-17, 7 de 43 tenían fechas entre el 23 de julio y el 16 de octubre. Por el tope de 6 h de los canales no llegaban (unas 13 al día). Las vencidas ya se descartan con `dealnews:expires`.

## Decisión
- `SourceDescriptor.refeatures = True` en `dealnews`: su lectura no aplica el tope de 6 h. Lo ya guardado en `channel_post` se sigue saltando por id, así que una oferta enviada o marcada no se repite al volver a destacarse.
- La primera lectura marca también las viejas (`baseline`), para no enviarlas de golpe.
- `slickdeals_amazon` y los canales de Telegram conservan el tope.

## Alternativas consideradas
- Un tope más largo (p. ej. 6 días, menor que los 7 de retención de `channel_post`): evita reenvíos tras la purga · deja fuera justo las re-destacadas, que tienen hasta 3 meses · descartada.
- Guardar `last_seen_at` para que la purga respete lo que sigue en el feed: dedupe sin límite · migración y un `UPDATE` por lectura para un caso que no pasa (con ~33 ofertas al día, una oferta sale de la ventana de ~43 en uno o dos días) · descartada.

## Consecuencias
- Llegan las ofertas vigentes que DealNews vuelve a destacar, con la misma tarjeta «sin verificar».
- La primera lectura tras el despliegue envía una vez las re-destacadas que hoy están en el feed y nunca se marcaron (unas 7).
- Si una oferta siguiera en el feed más de 7 días después de verse por primera vez, se enviaría otra vez tras la purga.

## Cuándo revisar
- Se sostiene con (hoy): ~13 re-destacadas al día, ventana del feed de ~43 ofertas.
- Revisar si: aparecen tarjetas repetidas de DealNews con más de 7 días entre una y otra, o el volumen de DealNews pasa de 60 al día.
