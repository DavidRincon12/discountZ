# 0013. Filtrar las tarjetas y el rastreo por un perfil de temas y por intereses

- Fecha: 2026-10-22
- Estado: Aceptada

## Contexto
Con el tope diario (ADR 0012) llegan unas 60 tarjetas al día, pero de cualquier tema: mercado, belleza, juguetes o mascotas pesan tanto como la tecnología. David es estudiante de ingeniería y quiere tecnología, herramientas, ropa y lo útil para estudiar y trabajar, en Colombia y en EE. UU. Además el rastreo gasta peticiones y espacio en la base (236 de 500 MB) en categorías que nunca le interesan. Spec: `docs/superpowers/specs/2026-10-22-perfil-e-intereses-design.md`.

## Decisión
- **Modo por tema** en `config/topics.toml`, con los mismos temas del grupo: `card` (sale en tarjeta), `interest` (se rastrea, pero solo sale si coincide con un interés) u `off` (no se rastrea). Hoy: tecnología, electrodomésticos, ferretería, moda y libros en `card`; hogar, deportes y otros en `interest`; belleza, mercado, bebés y mascotas en `off`.
- **Rastreo por perfil:** cada partición del catálogo trae su ruta; se salta la que cae en un tema `off`, salvo que una regla más profunda la lleve a un tema rastreado. Lo de un tema que no es `card` no se confirma ni se envía, ni siquiera al resumen de la noche. `/seguir` y `/buscar` no pasan por el perfil.
- **Intereses** (segundo PR): términos en español y en inglés; lo que coincide sale en tarjeta aunque su tema no sea `card`, aunque la fuente haya llenado su tope y sin el precio mínimo, pero con descuento real y como mucho 3 por interés al día.

## Alternativas consideradas
- Puntuación ponderada (el perfil suma puntos en lugar de filtrar): más fina, pero difícil de predecir y de explicar · costo de revertir: bajo.
- Todo por comandos guardados en la BD (`/perfil`, `/interes`): más flexible, pero más código y una migración solo para algo que cambia poco · costo de revertir: medio. Los intereses personales sí van en la BD, porque cambian seguido.

## Consecuencias
Llegan menos tarjetas y casi todas del perfil; el rastreo deja de recorrer mercado, belleza, bebés y mascotas en las tiendas VTEX y en eBay, y su catálogo se borra solo con la retención. Se pierde lo bueno que caiga fuera del perfil sin coincidir con un interés. Alkosto sigue entero: su barrido es por franjas de precio.

## Cuándo revisar
- Se sostiene con (hoy): base en 236 de 500 MB, ~60 tarjetas al día con el tope de 8 por fuente.
- Revisar si: las tarjetas bajan de 10 al día, un interés llena su tope 3 días seguidos o la base pasa de 400 MB.
