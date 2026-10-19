# discountZ

Monitor personal de ofertas para David (Colombia): rastrea tiendas CO (COP) y de EE. UU. (USD), valida descuentos contra un histórico propio y avisa por Telegram. Corre en GitHub Actions con Supabase Postgres, costo $0.

## Stack
- Python 3.13 síncrono, uv (versión fijada en `[tool.uv] required-version`)
- httpx y curl_cffi (HTTP), selectolax (HTML), defusedxml (RSS), pydantic v2 + pydantic-settings (bordes)
- psycopg 3 sobre PostgreSQL 17 (CI) / Supabase; migraciones SQL propias en `migrations/`
- pytest + hypothesis, ruff, mypy strict, import-linter, pre-commit + gitleaks

## Comandos
- Instalar: `uv sync`
- Verificación completa (debe quedar en verde): `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest -q`
- Una prueba: `uv run pytest tests/unit/test_money.py -q -k regional`
- Integración (Postgres): `DISCOUNTZ_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest -q -m integration`
- CLI: `uv run python -m discountz {run,crawl,migrate,check-config,record-fixture}` (`crawl --source exito [--minutes 45]`)

## Estructura
- `src/discountz/domain/`: núcleo puro (dinero, modelo, segmentos, estadísticos, guardas, evaluación, comandos, planificador, salud)
- `src/discountz/infra/`: HTTP, BD, migraciones, Telegram, logs
- `src/discountz/sources/`: contrato `Source`, registro y un adaptador por plataforma
- `src/discountz/app/`: casos de uso del ciclo
- `tests/{unit,sources,integration,e2e,arch}/`, fixtures en `tests/fixtures/<fuente>/`, HTML esperado de Telegram en `tests/golden/`
- `config/`: TOML público con parámetros, presupuestos, eventos y canarios; `config/topics.toml` con los temas de Telegram y las reglas ruta → tema
- `assets/fonts/`: Inter (OFL) para las tarjetas PNG (`infra/card.py`)

## Arquitectura
Monolito hexagonal-lite síncrono. Regla `app → sources → infra → domain`, comprobada por import-linter (contratos «Capas», «Dominio puro» y «Adaptadores independientes»). Detalle en `docs/architecture.md`; decisiones en `docs/decisions/`; diseño en `docs/superpowers/specs/2026-10-13-discountz-v1-design.md`.

## Estado
Estado actual, siguiente paso y pendientes en `docs/estado.md`: léelo al empezar y reescríbelo al cerrar cada PR.

## Convenciones y gotchas
- Idioma del producto (mensajes de Telegram, README): español (Colombia).
- Dinero solo en enteros de unidad menor (`Money`); COP exponente 0, USD exponente 2. Nunca `float`; JSON con `parse_float=Decimal`.
- Porcentajes en puntos básicos (`_bp`). Datetimes siempre con tz UTC; el reloj solo se lee en `__main__.py` e `infra/http.py` (ruff `TID251`).
- Logs con lista blanca de campos: nunca URLs con query, cuerpos, mensajes de pydantic/psycopg ni secretos.
- ruff 0.17 formatea Markdown: `docs/` está en `extend-exclude`.
- Sin costo puesto ni TRM: las alertas USD dicen «🇺🇸 Compra por casillero».
- Producción: el job de `cycle` solo corre con la variable de Actions `DISCOUNTZ_ACTIVO=true`. `/buscar` exige `Capability.SEARCH` (Newegg no la tiene).
- Rastreo: el `ListPrice` de VTEX viene inflado (nunca usarlo); su WAF rechaza `+` como espacio en la query (`infra/http` usa `%20`) y una página de 50 productos pesa hasta ~43 MB (Moda de Éxito): `sources/vtex.py` pide por mitades lo que pasa el tope de 24 MB; `*.vtexassets.com` está bloqueado por el proxy de la sesión en la nube (no desde Actions). El job `crawl` solo corre con `DISCOUNTZ_RASTREO=true`.
- Canales de Telegram (`sources/telegram_channels.py`, `app/channels.py`, migración `0003`): se leen de `t.me/s/<canal>?after=<id>` sin cuenta (20 posts por página); las ofertas salen «sin verificar» directo a `outbox` (sin `alert` ni histórico), y el enlace «Ver oferta» solo pasa con `LINK_HOSTS` y nunca se descarga. Cinco canales (migración `0006` agrega DescuentosHome y OFERTU); los de `REQUIRES_COUPON` (OFERTU) solo envían posts con código, y lo filtrado no cuenta como formato roto.
- Feeds de Amazon «sin verificar» (`sources/dealnews.py`, `slickdeals_amazon` en `sources/slickdeals.py`, migración `0004`, ADR 0006): canales con `ordered_ids=False` que se releen enteros; la primera lectura con ofertas marca con `channel_post.baseline` sin enviar. El enlace de DealNews (`?iref=rss`) no se toca, lo exigen sus términos: si `SafeUrl` lo cambiaría, el ítem se descarta. Su id sale del `<guid>`, no del `<link>`. DealNews (`refeatures=True`, ADR 0007) no tiene el tope de 6 h: vuelve a destacar ofertas viejas y vigentes.
- eBay (`sources/ebay.py`, migración `0005`, ADR 0008): Browse API con token OAuth de aplicación en memoria; `external_id` = id legado (`<legacy>` o `<legacy>V<var>`, la BD no admite `|`); precio = precio + envío a 33166; solo `conditionIds:{1000}`. `getItems` (lote) da 403 sin acceso: toda ficha va de a una (`max_batch=1`) y la cuota de 5.000 se reparte 4.000 rastreo / 1.000 ciclo. `MODEL_LOW` compara contra la mediana de ≥ 5 publicaciones del mismo `epid` (`catalog_item.model_key`). Sus fixtures se graban con un workflow temporal (la nube de la sesión no tiene las claves).
- Canal general (`DISCOUNTZ_TELEGRAM_CHANNEL_ID`, opcional): `app/deliver.py` copia cada `alert`/`digest` ya enviada al grupo, sin tema y sin botones de callback; de mejor esfuerzo (sin reintento, nunca cambia la fila del grupo). Los comandos solo se leen del grupo.
- Fixtures marcadas con `"nota"` en su `.meta.json` son sintéticas (la búsqueda de Newegg, que da CAPTCHA en todas las redes probadas, y las de las API de Best Buy y Walmart hasta grabarlas con las claves de David). `record-fixture` no sigue redirecciones: grabar la URL final.

## Reglas de David (prevalecen sobre las skills, incluida superpowers)
- Autoría: todo a nombre de David Santiago Rincón Bautista <davidsantiagorb@ufps.edu.co>, sin `Co-Authored-By`, `Claude-Session` ni menciones a IA. `.claude/hooks/git-guard.sh` fija la identidad al iniciar sesión; antes de push, `bash .claude/hooks/git-guard.sh check`.
- Idioma: todo lo redactado en español (commits, PRs, issues, comentarios, docs, specs y planes). En inglés solo los identificadores, el tipo de commit y los encabezados estructurales de superpowers (`### Task N:`). El texto del producto va en el idioma de su audiencia (ver Convenciones).
- Commits `tipo(alcance): descripción en español`; PRs con la plantilla del repo y Squash and merge; nada de push a `main` ni force-push sin confirmación.
- Proceso: superpowers. Las tareas triviales (typo, texto, formato, renombrar o un cambio que David ya especificó) van directo, sin brainstorming ni plan. Todo cambio de comportamiento lleva primero su prueba que falla (TDD estricto).
- Ejecución de planes: Native por defecto; subagentes solo en planes largos o riesgosos. El plan se commitea al aprobarlo.
- Ramas: en local, una rama `feat/…` (o `fix/`, `docs/`…) creada antes del commit del spec; worktree solo si David lo pide. En la nube (`CLAUDE_CODE_REMOTE=true`), solo la rama de la sesión: sin worktrees ni ramas nuevas, salvo la rama local de un spike, que no se publica.
- Una decisión de arquitectura de un spec aprobado va como ADR en `docs/decisions/`, en la primera tarea del plan.
- Leer este archivo antes de explorar el código y actualizarlo en 1-2 líneas cuando cambie la estructura o aparezca un gotcha.
- Skills: al empezar cualquier desarrollo, y antes de cualquier skill de superpowers, invoca `desarrollo-senior`, `arquitectura-y-datos` y `calidad-y-seguridad` (y `preparar-repo` si falta git-guard o `CLAUDE.md`). Tras una compactación, vuelve a invocarlas.
