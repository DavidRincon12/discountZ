# 0010. No agregar un carril «más descuento primero» al rastreo de VTEX

- Fecha: 2026-10-20
- Estado: Aceptada
- Origen: auditoría del 2026-10-20 (Éxito tarda ~10 h en volver a ver un producto).

## Contexto
El rastreo de Éxito recorre ~31,6 mil productos por hora y da la vuelta completa en ~10 h, así que una bajada puede tardar horas en verse. VTEX ordena la búsqueda con `O=OrderByBestDiscountDESC`; si ese orden pusiera arriba las bajadas reales, bastaría leer las primeras páginas de cada categoría cada hora para verlas antes.

## Decisión
No se agrega el carril. El orden «más descuento» de VTEX se calcula contra el `ListPrice`, que en Éxito viene inflado (gotcha ya conocido: nunca se usa), así que sus primeras páginas las ocupan descuentos ficticios y una bajada real frente al histórico propio no llega ahí.

## Alternativas consideradas
- Carril horario con las 2 primeras páginas de «más descuento» por categoría: pocas peticiones · no encuentra bajadas reales · revertir: quitar el carril.
- Spike: ¿al menos el 50 % de las bajadas reales aparece en las 2 primeras páginas de «más descuento» de su categoría? · criterio ≥ 50 % · medido: en Tecnología, Mercado, Electrodomésticos y Hogar de Éxito (2026-10-20, `products/search` con `isAvailablePerSalesChannel_1:1`, 2 páginas de 50 por categoría), 378 de 381 productos con precio tienen `ListPrice ≥ 2 × Price` (un combo de teclado y mouse de $182.000 con lista de $95.480.000) y solo 2 tienen `PriceWithoutDiscount > Price` · no. No hizo falta medir los 2 días previstos: el orden lo dicta el `ListPrice`, no la bajada.

## Consecuencias
La latencia de Éxito sigue dependiendo del barrido completo y de la capa caliente. Se evita gastar presupuesto diario en páginas que no traen ofertas.

## Cuándo revisar
- Se sostiene con (hoy): `ListPrice` inflado en ~99 % de las primeras páginas de «más descuento» de Éxito.
- Revisar si: VTEX permite ordenar por `PriceWithoutDiscount`, o una muestra de esas páginas baja de 50 % de productos con `ListPrice ≥ 2 × Price`.
