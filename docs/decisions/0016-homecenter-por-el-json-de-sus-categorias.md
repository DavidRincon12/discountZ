# 0016. Rastrear Homecenter por el JSON de sus páginas de categoría

- Fecha: 2026-10-24
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-24-homecenter-design.md` (§0.2 H1-H3, §1 y §2)

## Contexto
David quiere más tiendas de herramientas y tecnología. Homecenter no tiene API pública ni catálogo VTEX: su web es Next.js y cada categoría trae sus 40 productos en `__NEXT_DATA__` (~1,9 MB). Medido el 2026-10-23 desde la nube y desde Actions: 200 sin desafío, paginación `?currentpage=N` sin tope, `robots.txt` permite `/category/` y cierra `/search`, `/browse` y `?queryId=`. Sus términos limitan el contenido a «revisión, compra y consulta» y no mencionan lectores automáticos.

## Decisión
- Fuente `homecenter` (`CrawlSource`, COP) en `sources/homecenter.py`. Se lee solo `/homecenter-co/category/…?currentpage=N`; las fichas solo para confirmar candidatos.
- Precio = `INTERNET`, o `NORMAL` si no hay; `CMR` (tarjeta) nunca. Sin «antes» de la tienda.
- Solo lo que vende Homecenter: la ficha trae `is_marketplace_product` y la confirmación descarta lo del marketplace.
- Una partición por categoría del perfil (13, ≈ 14,8 mil productos); sin capa caliente; 800 peticiones al día.
- Sin `Capability.SEARCH`: la búsqueda de la tienda está cerrada por `robots.txt`.

## Alternativas consideradas
- Sitemap de productos y fichas: ~0,9 MB por ficha y 15 mil fichas por vuelta · más tráfico y sin ventaja · descartada.
- API interna de la web (`/s/…`): no está documentada ni permitida por `robots.txt` · descartada.
- Spike: ¿responde Homecenter desde Actions sin desafío? · criterio 200 en todas las categorías · medido 200 en 5 de 5 (2026-10-23, run 40041191094) · sí.

## Consecuencias
- Cada vuelta pesa ≈ 700 MB de HTML; con 800 peticiones al día son ≈ 1,5 GB, gratis en Actions.
- El riesgo de términos es de David (H1), igual que en Alkosto: si Homecenter bloquea o reclama, se apaga con `/fuente off homecenter`.
- No hay EAN: la regla del mismo EAN entre tiendas no cruza Homecenter.

## Cuándo revisar
- Se sostiene con (hoy): ~14,8 mil productos, ≈ 370 páginas por vuelta, 800 peticiones al día.
- Revisar si: `__NEXT_DATA__` cambia de forma, Homecenter pone un desafío o reclama, o la BD pasa de 450 MB.
