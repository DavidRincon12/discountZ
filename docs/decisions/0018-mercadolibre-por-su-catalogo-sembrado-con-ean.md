# 0018. Seguir Mercado Libre por su catálogo, sembrado con los EAN de las tiendas

- Fecha: 2026-10-29
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-29-mercadolibre-design.md` (§0.2 M1-M6, §1 y §2)

## Contexto
Mercado Libre Colombia (COP) es la tienda más grande del país y no la cubría ninguna fuente. David creó una app de desarrollador de solo lectura y subió `MELI_CLIENT_ID` y `MELI_CLIENT_SECRET` como secretos (2026-10-29). Medido ese día desde Actions con el token de la app (`client_credentials`, 6 h): la búsqueda general (`/sites/MCO/search`), la lectura de publicaciones (`/items/{id}`) y `/highlights` responden 403 aun con permisos; sí responden el catálogo de productos (`/products/search`, `/products/{id}`), sus vendedores con precio (`/products/{id}/items`) y la reputación de cada vendedor (`/users/{id}`). La búsqueda de productos exige palabras o un código, se corta en 300 resultados, y de lo hallado por palabras solo entre el 10 y el 24 % tiene vendedores; por EAN, 11 de 18 EAN de nuestras fixtures dieron producto y 10 tenían vendedores. La web responde 302 a una verificación de cuenta desde Actions.

## Decisión
- Fuente `mercadolibre` (`sources/mercadolibre.py`) que solo usa la API, con el token en memoria como eBay. Implementa el protocolo nuevo `EanCatalogSource` (`CrawlSource` más `find_by_ean`).
- Las referencias son los EAN de los temas en tarjeta que publican las tiendas colombianas rastreadas, con precio ≥ `min_price_cop`, del más caro al más barato (tarjeta de decisión del 2026-10-29). Cada EAN buscado queda 30 días en `ean_lookup`; cada producto hallado entra a `catalog_item` con el EAN y el tema de la semilla.
- Para estas fuentes, el rastreo cambia la capa caliente y la vuelta por la relectura de lo seguido (lo no pedido en 6 h, por `catalog_item.checked_at`) y la capa de semillas, que para con la BD en ≥ 450 MB o con 12 mil productos seguidos.
- Precio = el del vendedor elegible más barato: nuevo, sin envío internacional, en COP, y tienda oficial (`FIRST_PARTY`) o MercadoLíder Platinum o Gold con reputación `5_green` (`MARKETPLACE`). `original_price` nunca se lee: el descuento sale del histórico propio.
- `/seguir` solo con enlaces de producto (`/p/MCO…`); sin `/buscar` ni intereses.

## Alternativas consideradas
- Búsqueda por palabras con `domain_id`: corta en 300 resultados y solo el 10-24 % trae vendedores · descartada.
- Recorrer el catálogo por categorías: la API no lo permite (`domain_id` solo filtra) · descartada.
- Búsqueda general, `/items` y `/highlights`: 403 aun con los permisos de la app · descartadas.
- Leer la web: responde 302 a una verificación de cuenta desde Actions, y no se sortean bloqueos · descartada.
- Spike: ¿sirve el EAN como llave entre nuestras tiendas y el catálogo? · criterio: más de la mitad de los EAN de electrónica con producto y vendedores · medido 10 de 18 (Actions, 2026-10-29) · sí.

## Consecuencias
- Cubre muchas referencias sin recorrer el sitio, con unas 20 mil peticiones al día y a lo sumo ~17 MB más en la BD (12 mil filas de catálogo y ~100 mil de `ean_lookup`).
- Solo sigue productos que también vende alguna tienda colombiana rastreada con EAN; lo exclusivo de Mercado Libre queda fuera.
- Los términos permiten la API «únicamente para facilitar el uso del Sitio» y solo copias intermedias: el riesgo es de David, como en eBay (ADR 0008). El peor caso es que suspendan la app; entonces, `/fuente off mercadolibre`.

## Cuándo revisar
- Se sostiene con (hoy): 20 mil peticiones al día, tope de 12 mil productos seguidos, relectura cada ~24 h y ninguna cabecera de cuota.
- Revisar si: aparece un 429, el tope se llena y la relectura pasa de un día, la BD llega a 450 MB, o `/products/{id}/items` empieza a responder 403.
