# 0001. Correr un monolito modular síncrono en GitHub Actions con Supabase Postgres

- Fecha: 2026-10-13
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-13-discountz-v1-design.md` (§0.3, §0.4, §1, §7.5; ADR 001, 005, 006, 009, 010, 016 y 023 de §9)

## Contexto
discountZ es un monitor personal de ofertas (un solo usuario, decenas a pocos cientos de productos) con costo operativo de $0 y sin máquina en casa (spec §0.2, D1-D3). Necesita ejecutarse periódicamente, guardar un histórico de precios que sobreviva entre ejecuciones y avisar por Telegram.

Matriz de infraestructura a $0 evaluada (spec §0.3):

| Opción | Latencia | Riesgo de ban | Persistencia | Veredicto |
|---|---|---|---|---|
| GitHub Actions + disparo externo | ~5-8 min | Alto en HTML protegido, bajo en APIs | BD externa | **Elegida** |
| Cloudflare Workers + D1 | 1 min | Medio | D1 500 MB | Descartada: 10 ms de CPU no alcanzan para parsear HTML |
| Oracle Always Free | 1 min | Alto | Disco | Frágil: tarjeta, falta de capacidad, VMs ociosas recuperadas |
| Render / Koyeb | — | Alto | Efímera | Descartada: se duermen; cron de pago |
| Máquina en casa + Docker | 1 min | Bajo | SQLite | Plan B (D2 la excluye hoy) |

Spike de fuentes (2026-10-13, spec §0.4): 3 runners de Actions con IPs de Azure distintas × 4 peticiones por fuente y cliente. VTEX (Éxito, Carulla, Olímpica, Panamericana) 12/12; Amazon 12/12 con volumen bajo; Newegg 12/12; Slickdeals RSS 12/12; Walmart y Best Buy 12/12 solo con curl_cffi (0/12 con httpx) y extracción dudosa; Alkosto, Ktronix, Falabella y Mercado Libre no viables sin más trabajo (sub-proyectos).

## Decisión
- **Monolito modular hexagonal-lite, síncrono**, en Python 3.13: `domain/` puro, `infra/`, `sources/` (un adaptador por plataforma) y `app/`. Regla `app → sources → infra → domain` comprobada con import-linter. Unas 110 peticiones por ciclo con pausas de 2-12 s caben en 2-4 min en un hilo con turnos rotativos por fuente; asyncio agregaría cancelación entre tareas hermanas y una conexión compartida con candado (ADR 001 del spec).
- **GitHub Actions en un repo público en la cuenta de David** (minutos ilimitados; portafolio). Plan B: el mismo código en una VM o equipo local con cron, sin cambios (ADR 023).
- **Exclusión mutua con un lease con TTL de 16 min** en la tabla `run_lease`, más `concurrency` de Actions sin cancelar; plazo duro interno de 12 min < TTL, así que no hace falta fencing (ADR 005).
- **Migraciones propias** (SQL up/down con sha256), ejecutadas solo por `migrate.yml` con aprobación; el ciclo solo verifica `SCHEMA_VERSION` (ADR 006).
- **Supabase Postgres** vía Supavisor en modo sesión (`:5432`, IPv4) con `sslmode=verify-full`; roles `discountz_app` (DML) y `discountz_owner` (DDL, secreto de environment con revisor) (ADR 009). Esquema propio `discountz`, Data API apagada y `REVOKE` a `anon`/`authenticated` (ADR 010).
- **healthchecks.io opcional** como detector de inactividad y `keepalive.yml` aparte con solo `actions: write`; sin commits automáticos a `main` (ADR 016).

## Alternativas consideradas
- asyncio + TaskGroup: más concurrencia · más modos de falla · revertir: medio.
- `pg_try_advisory_lock`: simple · queda huérfano si se mata el job · revertir: bajo.
- Fencing por `epoch`: protege zombis · innecesario con plazo duro < TTL (recorte D7) · revertir: bajo.
- Alembic: estándar · arrastra SQLAlchemy · revertir: medio.
- Automigrar en cada ciclo: cómodo · el ciclo tendría permisos de DDL y un rollback se re-aplicaría.
- Conexión directa a Supabase: solo IPv6, que los runners no tienen.
- Spike: ¿las fuentes responden desde IPs de Actions? · criterio ≥ 11/12 · medido 12/12 en VTEX, Amazon, Newegg y Slickdeals (3 runners × 4 peticiones) · sí.

## Consecuencias
- Costo $0 y despliegue = merge a `main`. Sin servidor residente.
- La latencia mínima es la del disparo (~5-8 min); los relámpagos más cortos se pierden (riesgo aceptado).
- Las IPs de Azure pueden volverse inviables en cualquier tienda; el circuit breaker y la salud por fuente lo hacen visible.
- GitHub desactiva los workflows programados tras 60 días sin actividad; el resumen diario y healthchecks lo detectan.

## Cuándo revisar
- Se sostiene con (hoy): 1 usuario, ≤ 300 productos, ~110 peticiones por ciclo, BD < 150 MB el año 1, $0 al mes.
- Revisar si: el p95 de la duración del ciclo pasa de 5 min; la BD pasa de 300 MB; Amazon da más de 10 % de captchas en 48 h; o la latencia de una alerta pasa de 15 min.
