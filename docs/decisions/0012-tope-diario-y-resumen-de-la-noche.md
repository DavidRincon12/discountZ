# 0012. Limitar las tarjetas por fuente al día y mandar el resto en un resumen de la noche

- Fecha: 2026-10-22
- Estado: Aceptada

## Contexto
Medido del 15 al 21 de octubre de 2026 con todo lo enviado: unas 450 ofertas al día, al grupo y al canal, sin ningún tope. Una sola fuente mandaba hasta 85 al día (Éxito), y cada talla, color o tono de una misma promoción salía en su propia tarjeta (10 loncheras de la misma línea al −20 % en Panamericana). El informe «Calidad de las ofertas de discountZ» propuso filtros de precio, ahorro y mercado (ya aplicados en este PR), una tarjeta por promoción y un tope diario por fuente. David lo aprobó el 22 de octubre.

## Decisión
- **Tope:** cada fuente del rastreo o canal manda en tarjeta sus primeras `daily_per_source` ofertas (8, en `config/discountz.toml`) del día de Bogotá. Los errores de precio no cuentan y siempre salen. Los seguimientos (`/seguir`, `/buscar`) no tienen tope: los pidió David.
- **Una tarjeta por promoción (solo rastreo):** una bajada o un error de precio de la misma tienda, tema y clase, con el ahorro a ±0,5 % de otro que salió en tarjeta en las últimas 24 h, se tiene por la misma promoción y no sale en tarjeta. Las alertas de modelo de eBay (`MODEL_LOW`) no se agrupan: comparan vendedores distintos.
- **Resumen de la noche:** lo que no sale en tarjeta queda en `offer_log` con su línea ya armada. El primer ciclo después de las 21:00 de Bogotá manda lo retenido antes de esa hora, por fuente y de mayor a menor ahorro (los canales no traen el «antes»: van en su orden), partido en mensajes de 4.096 caracteres visibles. Va como `digest`, así que también llega al canal general. Lo retenido después de las 21:00 sale al día siguiente.
- `offer_log` (migración 0010) guarda 7 días: qué salió en tarjeta (cuenta para el tope y la promoción) y qué espera el resumen. Una alerta del rastreo retenida queda con `delivery = 'held'`.

## Alternativas consideradas
- Contar las tarjetas en `outbox`, `alert` y `channel_post`: tres consultas distintas y sin dónde guardar la línea del resumen. Descartado.
- Elegir las mejores del día en vez de las primeras: exige esperar al final del día para decidir y las ofertas de los canales duran horas. Descartado.
- Resumen a la mañana siguiente: lo retenido de la mañana llegaría casi un día tarde. Se eligió las 21:00, antes de las horas de silencio (23:00).
- Agrupar promociones también por título: más preciso, pero las tallas y colores cambian el título de formas distintas en cada tienda. Se deja el ahorro: lo agrupado de más no se pierde, va al resumen.

## Consecuencias
Se esperan unas 60 tarjetas al día y un resumen nocturno de varios mensajes. Las tarjetas de una tienda que rastrea de noche pueden gastarse en la madrugada, con sonido apagado; el resto del día va al resumen. Un resumen con más de 40 líneas de una fuente muestra las 40 de mayor ahorro y cuenta las demás.

## Cuándo revisar
- Se sostiene con (hoy): unas 15 fuentes activas, menos de 300 ofertas retenidas al día.
- Revisar si el resumen pasa de 5 mensajes seguido, o si David pide más (o menos) tarjetas de una fuente concreta: el tope podría ser por fuente.
