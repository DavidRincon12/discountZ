# discountZ v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Monitor personal de ofertas que corre en GitHub Actions, guarda un histórico de precios por segmentos en Supabase Postgres, valida ofertas reales contra ese histórico y avisa por Telegram, con comandos para gestionar seguimientos y búsquedas.

**Architecture:** Monolito modular Python síncrono, hexagonal-lite: `domain/` puro (sin I/O, reloj ni azar), `infra/` (HTTP, BD, Telegram), `sources/` (un adaptador por plataforma) y `app/` (casos de uso del ciclo). Regla de dependencias `app → sources → infra → domain`, comprobada con import-linter.

**Tech Stack:** Python 3.13, uv, httpx, curl_cffi, selectolax, defusedxml, pydantic v2 + pydantic-settings, psycopg 3, pytest + hypothesis, ruff, mypy strict, import-linter, PostgreSQL 17 (CI) / Supabase.

**Spec:** `docs/superpowers/specs/2026-10-13-discountz-v1-design.md`. Cada tarea cita la sección que implementa; el implementador lee esa sección completa antes de empezar. Si el spec y este plan difieren, manda la sección 0.2 del spec (decisiones de David) y luego este plan.

## Global Constraints

- Python `==3.13.*`; dependencias fijadas con `==` en `pyproject.toml` y `uv.lock` versionado; uv con `required-version` fijo.
- Dinero solo en enteros de unidad menor (`Money(minor: int, currency)`): COP exponente 0, USD exponente 2. Nunca `float`; JSON con `json.loads(..., parse_float=Decimal)`; redondeo `ROUND_HALF_UP`.
- Porcentajes en puntos básicos (`_bp`, enteros). Fechas `timestamptz`/`datetime` con tz UTC; prohibidos los datetime naive (ruff `DTZ`).
- `domain/` no importa `httpx`, `curl_cffi`, `psycopg`, `selectolax`, `pydantic`, `pydantic_settings`, `defusedxml` (contrato import-linter «Dominio puro»).
- `datetime.now`, `utcnow`, `date.today`, `time.time`, `random.random` prohibidos (ruff `TID251`) salvo en `__main__.py` e `infra/http.py`.
- Sin costo puesto, TRM, `landed_cop`, `/peso` ni respaldo (spec §0.2 D7 y D10). Las alertas USD llevan la línea fija «🇺🇸 Compra por casillero».
- Categorías: `tecnologia`, `componentes`, `hogar`, `ropa`, `otros`. Parámetros por categoría y globales: spec §3.9.
- Todo texto (comentarios, docstrings, commits, mensajes de Telegram, docs) en español; identificadores en inglés.
- Commits `tipo(alcance): descripción` en español, ≤ 72 caracteres, sin atribución a IA; antes de cada push, `bash .claude/hooks/git-guard.sh check`.
- Logs: lista blanca de campos; nunca URLs con query, cuerpos de respuesta, mensajes de `pydantic`/`psycopg` ni secretos (spec §7.3).
- Comando de verificación completo (debe quedar en verde al final de cada tarea): `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest -q`.

## Review Focus

1. **Precio con formato regional**: `"1.299.900"`, `"$ 1.299.900"`, `"US$1,299.99"`, `"1299,90"` deben convertirse al entero correcto o fallar con `ParseError`, nunca en un valor ×100 o ÷100 silencioso → prueba en Task 4 (`test_parse_price_regional_formats`).
2. **Comandos de chats ajenos o mal formados**: un `/seguir` de otro `from.id`, una URL de host no admitido o con userinfo, 6 URLs en un mensaje → rechazados sin tocar la BD ni hacer HTTP → pruebas en Task 9 y Task 17.
3. **Ciclo interrumpido**: el job muere entre el commit de una alerta y el envío a Telegram → el siguiente ciclo la envía exactamente una vez (outbox) → prueba de integración en Task 19.
4. **Tienda que cambia su HTML**: el parser devuelve el mismo precio para todo un lote o ningún precio → G10/`SOFT_EMPTY`, cuarentena y aviso, sin alertas falsas → pruebas en Task 7 y Task 11.
5. **Ciclos solapados** (disparo externo + schedule + manual) → solo uno toma el lease; el otro termina `skipped_locked` sin escribir → prueba de integración en Task 13.

---

### Task 1: ADRs y arquitectura documentada

**Files:**
- Create: `docs/decisions/0001-monolito-sincrono-en-github-actions.md`
- Create: `docs/decisions/0002-historico-por-segmentos-y-algoritmo.md`
- Create: `docs/decisions/0003-frescura-disparo-externo-y-modo-caliente.md`
- Create: `docs/architecture.md`

**Interfaces:** ninguna (documentación).

- [ ] **Step 1: Escribir los 3 ADRs** con la plantilla de `arquitectura-y-datos` (Contexto, Decisión, Alternativas, Consecuencias, Cuándo revisar). Contenido: 0001 resume ADR 001, 005, 006, 009, 010, 016, 023 del spec §9 e incluye la matriz de §0.3 y el spike de §0.4; 0002 resume ADR 002, 003, 004, 007, 011, 012; 0003 resume ADR 024, 025, 026. «Cuándo revisar» con números: p95 del ciclo > 5 min, BD > 300 MB, Amazon con > 10 % de captchas en 48 h, latencia de alerta > 15 min.
- [ ] **Step 2: Escribir `docs/architecture.md`** (≤ 150 líneas) con la plantilla de `arquitectura-y-datos`: contexto, diagrama mermaid (Actions ← cron-job.org; Actions → tiendas, Supabase, Telegram), módulos y la regla de dependencias con el nombre de cada contrato de import-linter, modelo de datos (tablas de spec §2.2), flujo del ciclo (spec §1.3 en 10 líneas), atributos de calidad y riesgos (spec §8.4).
- [ ] **Step 3: Commit**

```bash
git add docs/decisions docs/architecture.md
git commit -m "docs(arquitectura): registra ADRs y arquitectura de la v1"
```

---

### Task 2: Scaffolding, tooling y CI

**Files:**
- Create: `pyproject.toml`, `uv.lock`, `.python-version`, `.editorconfig`, `.pre-commit-config.yaml`, `.env.example`
- Create: `src/discountz/__init__.py`, `src/discountz/{domain,infra,sources,app}/__init__.py`, `src/discountz/__main__.py`
- Create: `tests/arch/test_smoke.py`, `tests/conftest.py`
- Create: `.github/workflows/ci.yml`, `.github/dependabot.yml`

**Interfaces:**
- Produces: paquete `discountz` instalable; `python -m discountz --help` lista los subcomandos `run`, `migrate`, `check-config`, `record-fixture` (stubs que salen con código 2 y «no implementado»).

- [ ] **Step 1: Escribir la prueba de humo**

```python
# tests/arch/test_smoke.py
import subprocess, sys

def test_cli_help_lists_subcommands() -> None:
    out = subprocess.run([sys.executable, "-m", "discountz", "--help"], capture_output=True, text=True, check=True).stdout
    for cmd in ("run", "migrate", "check-config", "record-fixture"):
        assert cmd in out
```

- [ ] **Step 2: Verificar que falla** — `uv run pytest tests/arch/test_smoke.py -q` → FAIL (`No module named discountz`).
- [ ] **Step 3: Crear el proyecto.** `pyproject.toml` con `[project] requires-python = "==3.13.*"`, dependencias de runtime fijadas (`httpx`, `curl_cffi==0.16.3`, `selectolax`, `defusedxml`, `pydantic`, `pydantic-settings`, `psycopg[binary]`) y grupo dev (`pytest`, `hypothesis`, `ruff`, `mypy`, `import-linter`, `pre-commit`). `[tool.uv] required-version`. `[tool.ruff]` con `select = ["E","F","I","B","UP","DTZ","TID","S","RUF","SIM"]` y `[tool.ruff.lint.flake8-tidy-imports.banned-api]` según Global Constraints (excepciones por `per-file-ignores`). `[tool.mypy] strict = true, files = ["src","tests"]`. `[tool.importlinter]` con los 3 contratos de spec §1.2 (el de adaptadores independientes se agrega en Task 15, cuando existan). `[tool.pytest.ini_options] markers = ["integration"]`, `addopts = "-m 'not integration'"`. `__main__.py` con `argparse` y subcomandos stub. `.env.example` con los nombres de spec §7.5 sin valores.
- [ ] **Step 4: `.pre-commit-config.yaml`** con hooks locales `ruff check`, `ruff format --check`, `mypy`, `lint-imports` (`language: system`, `pass_filenames: false`) y `gitleaks`.
- [ ] **Step 5: `ci.yml`** según spec §7.5: `on: pull_request` y `push` a `main`; `permissions: contents: read`; job `checks` con `astral-sh/setup-uv`, `uv sync --locked`, el comando de verificación completo; job `integration` con servicio `postgres:17` y `uv run pytest -q -m integration` (variable `DISCOUNTZ_TEST_DATABASE_URL`). Actions fijadas por SHA. `dependabot.yml` para `github-actions` y `uv`.
- [ ] **Step 6: Verificar** — el comando de verificación completo queda en verde (1 prueba).
- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .python-version .editorconfig .pre-commit-config.yaml .env.example src tests .github
git commit -m "build: crea el proyecto Python con tooling y CI"
```

---

### Task 3: Convenciones del repo (preparar-repo, fase 2)

**Files:**
- Create: `CLAUDE.md` (plantilla de `preparar-repo` §2.2), `docs/estado.md`, `.github/pull_request_template.md`, `.claude/rules/python.md`, `README.md` (borrador: qué es, requisitos, comandos; la guía de despliegue llega en Task 21)

**Interfaces:** ninguna.

- [ ] **Step 1: Escribir los archivos.** `CLAUDE.md` ≤ 100 líneas con stack, los comandos de Global Constraints, estructura, regla de dependencias, idioma del producto = español (Colombia) y la sección «Reglas de David». `.claude/rules/python.md` con `paths: ["src/**/*.py","tests/**/*.py"]` y las reglas de Global Constraints. `docs/estado.md` con las 5 secciones de `desarrollo-senior` §7 (siguiente paso: Task 4).
- [ ] **Step 2: Rompe a propósito una regla de arquitectura** (añade `import httpx` en `src/discountz/domain/__init__.py`), corre `uv run lint-imports` y comprueba que falla con «Dominio puro»; revierte.
- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/estado.md .github/pull_request_template.md .claude/rules README.md
git commit -m "chore: agrega convenciones del proyecto y estructura de docs"
```

---

### Task 4: `Money` y parseo de precios

**Files:**
- Create: `src/discountz/domain/money.py`, `src/discountz/domain/errors.py`
- Test: `tests/unit/test_money.py`

**Interfaces:**
- Produces: `class Currency(StrEnum): COP; USD` · `EXPONENT: Mapping[Currency, int] = {COP: 0, USD: 2}` · `@dataclass(frozen=True, slots=True, order=True) class Money: minor: int; currency: Currency` con `__add__`/`__sub__` que lanzan `CurrencyMismatch` · `to_minor(value: Decimal, currency: Currency) -> int` (`ROUND_HALF_UP`) · `parse_price(text: str, currency: Currency) -> int` (texto regional → unidad menor) · `format_money(m: Money) -> str` (`$1.299.900`, `US$99,99`).
- Produces (`errors.py`): `DiscountzError`, `DomainError(DiscountzError)`, `CurrencyMismatch(DomainError)`, `ParseError(DomainError)` con atributos `code: str` y `loc: str | None` (jerarquía completa: spec §7.1).

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
@pytest.mark.parametrize(("text", "cur", "minor"), [
    ("1.299.900", Currency.COP, 1_299_900), ("$ 1.299.900", Currency.COP, 1_299_900),
    ("$1.299.900,00", Currency.COP, 1_299_900), ("US$1,299.99", Currency.USD, 129_999),
    ("$74.99", Currency.USD, 7_499), ("129", Currency.USD, 12_900),
])
def test_parse_price_regional_formats(text, cur, minor): assert parse_price(text, cur) == minor

@pytest.mark.parametrize("text", ["", "gratis", "1.2.3,4,5", "-5"])
def test_parse_price_rejects_garbage(text): pytest.raises(ParseError, parse_price, text, Currency.COP)

def test_money_never_mixes_currencies():
    with pytest.raises(CurrencyMismatch): Money(1, Currency.COP) + Money(1, Currency.USD)

def test_to_minor_rounds_half_up(): assert to_minor(Decimal("10.005"), Currency.USD) == 1001

def test_format_money(): assert format_money(Money(1_299_900, Currency.COP)) == "$1.299.900"; assert format_money(Money(9_999, Currency.USD)) == "US$99,99"
```

- [ ] **Step 2: Verificar que fallan** — `uv run pytest tests/unit/test_money.py -q` → FAIL (import).
- [ ] **Step 3: Implementar `money.py` y `errors.py`.** `parse_price`: en COP, `.` es separador de miles y `,` decimal; en USD, `,` es miles y `.` decimal; rechaza más de un separador decimal y negativos.
- [ ] **Step 4: Verificar que pasan** y correr el comando de verificación completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(dominio): agrega Money y parseo de precios regionales"`

---

### Task 5: Modelo de dominio y `SafeUrl`

**Files:**
- Create: `src/discountz/domain/model.py`, `src/discountz/domain/urls.py`
- Test: `tests/unit/test_urls.py`, `tests/unit/test_model.py`

**Interfaces:**
- Produces (`model.py`): `ListingRef(source: str, external_id: str)`, `SellerClass(StrEnum)` (`FIRST_PARTY="f"`, `MARKETPLACE="m"`, `UNKNOWN="u"`), `ExtractionPath(StrEnum)`, `Offer` y `Lead` exactamente como spec §4.2; `SearchSpec(source: str, query: str, exclude_terms: tuple[str, ...], min_minor: int | None, max_minor: int | None, category: str)` con `accepts(offer: Offer) -> bool` y `normalize_title(text: str) -> str`; `AlertKind(StrEnum)` (`price_error`, `historic_low`, `target_reached`, `good_deal`, `community_deal`) con `rank` (5…1); `Maturity(StrEnum)` (`cold`, `warm`, `mature`); `Flag(StrEnum)` con todos los valores del CHECK de `alert.flags` (spec §2.2) más `hot` y `unverified`.
- Produces (`urls.py`): `class SafeUrl` (frozen; `host`, `path`, `query: tuple[tuple[str,str],...]`) con `SafeUrl.parse(raw: str, allowed_hosts: frozenset[str]) -> SafeUrl` que lanza `UnsafeUrl(DomainError)`; `str(SafeUrl)` sin parámetros de rastreo.

- [ ] **Step 1: Escribir las pruebas que fallan**

```python
@pytest.mark.parametrize("raw", [
    "http://www.exito.com/x/p", "https://www.exito.com@evil.com/x", "https://192.168.0.1/x",
    "https://www.exito.com:8443/x", "https://exito.com.evil.com/x", "ftp://www.exito.com/x",
])
def test_safe_url_rejects(raw): pytest.raises(UnsafeUrl, SafeUrl.parse, raw, frozenset({"www.exito.com"}))

def test_safe_url_strips_tracking():
    u = SafeUrl.parse("https://WWW.Amazon.com/dp/B0EXAMPLE1?tag=x&utm_source=y&th=1&keep=1", frozenset({"www.amazon.com"}))
    assert str(u) == "https://www.amazon.com/dp/B0EXAMPLE1?keep=1"

def test_search_spec_accepts_normalized_tokens_and_excludes():
    spec = SearchSpec("exito", "ssd 1 TB", ("carcasa",), None, 40_000_000, "componentes")
    assert spec.accepts(offer(title="Disco SSD Kingston 1TB NVMe", price=30_000_000))
    assert not spec.accepts(offer(title="Carcasa SSD 1TB", price=100_000))
    assert not spec.accepts(offer(title="SSD 1TB", price=50_000_000))
```

(`offer(...)` es una factory en `tests/factories.py` que crea `Offer` con valores por defecto válidos; créala en este paso.)

- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar** según spec §4.3 (`SafeUrl`, normalización: minúsculas, sin tildes, `1 TB → 1tb`).
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(dominio): agrega modelo de ofertas y SafeUrl"`

---

### Task 6: Segmentos y estadísticos ponderados

**Files:**
- Create: `src/discountz/domain/segments.py`, `src/discountz/domain/stats.py`
- Test: `tests/unit/test_segments.py`, `tests/unit/test_stats.py`, `tests/unit/test_stats_properties.py`

**Interfaces:**
- Produces (`segments.py`): `SegmentStatus(StrEnum)` (`OPEN="o"`, `SUSPECT="s"`, `GLITCH="g"`); `@dataclass(frozen=True) Segment(listing_id: int, started_at, last_seen_at, ended_at: datetime | None, price_minor: int | None, list_price_minor: int | None, in_stock: bool, seller_class: SellerClass, unit_key: str, status: SegmentStatus, sample_count: int)`; `Observation(t, price_minor, list_price_minor, in_stock, seller_class, unit_key)`; `Extend | CloseAndOpen | Ignore` y `apply_sample(open_seg: Segment | None, obs: Observation, h: timedelta, status: SegmentStatus) -> Extend | CloseAndOpen | Ignore`; `gap_h(nominal_interval: timedelta) -> timedelta` = `max(12 h, 1,5 × intervalo)`.
- Produces (`stats.py`): `segment_weight_s`, `wq`, `rarity_bp`, `is_power10_shift` con la firma y el cuerpo de spec §3.10; `HistoryStats` (dataclass con `r`, `m90`, `m30`, `m_prior`, `m_rec`, `q10`, `q90`, `v_bp`, `coverage_s`, `days`, `m_robust`, `raw_min`, `max_charged`, `anchor`); `compute_stats(segments: Sequence[Segment], now: datetime, s_cur: datetime, cat: CategoryParams, glob: GlobalParams, h: timedelta, reset_at: datetime | None, alltime_min: int | None, last_alert_price: int | None) -> HistoryStats` con las fórmulas de spec §3.3 (conjunto elegible E incluido).
- Produces (`domain/params.py`, créalo aquí): `CategoryParams` y `GlobalParams` (dataclasses frozen) con los valores por defecto de spec §3.9; `DEFAULT_CATEGORIES: Mapping[str, CategoryParams]`.

- [ ] **Step 1: Escribir las pruebas de `apply_sample`**: mismo estado dentro de H → `Extend`; precio distinto → `CloseAndOpen`; hueco > H → `CloseAndOpen`; `obs.t ≤ last_seen_at` → `Ignore`; caso U19 de spec §3.11 (Walmart cada 12 h con huecos de 10,2-13,8 h → 1 segmento).
- [ ] **Step 2: Escribir las pruebas de estadísticos**: `wq([(100,1),(200,1)], 5000) == 100`; `wq([], 5000) is None`; `rarity_bp([(240,45),(300,45)], 240) == 5000`; `is_power10_shift(2_299_000, 229_900)`; y las propiedades 1-6 de spec §3.11 con Hypothesis en `test_stats_properties.py`.
- [ ] **Step 3: Verificar que fallan.**
- [ ] **Step 4: Implementar** `segments.py`, `params.py` y `stats.py`.
- [ ] **Step 5: Verificar que pasan** + comando completo.
- [ ] **Step 6: Commit** — `git commit -m "feat(dominio): agrega segmentos y estadísticos ponderados por tiempo"`

---

### Task 7: Guardas G1–G14

**Files:**
- Create: `src/discountz/domain/guards.py`
- Test: `tests/unit/test_guards.py`

**Interfaces:**
- Consumes: `Offer`, `Segment`, `HistoryStats`, `Money`, `ParseError`.
- Produces: `GuardResult(StrEnum)` (`OK`, `PARSE_SHIFT`, `UNIT_CHANGED`, `IDENTITY_RESET`, `NOT_BUYABLE`, `OUT_OF_STOCK`, `BATCH_CONSTANT`, `DECIMAL_SHIFT`, `JUMP`, `SELLER_RESTRICTED`); funciones puras `check_offer(offer, source_currency, prev_list_price) -> Offer` (G1–G5: lanza `ParseError` o devuelve el `Offer` con `list_price=None` si G4), `check_batch(offers: Sequence[Offer], r_by_listing: Mapping[ListingRef, int | None]) -> bool` (G10, `True` = descartar lote), `check_storm(candidates: int) -> bool` (G12, `True` si ≥ 2) y `guard_flags(offer, open_seg, stats, allow_marketplace) -> frozenset[GuardResult]` (G6–G9, G11, G13, G14).

- [ ] **Step 1: Escribir las pruebas que fallan**: un caso positivo y uno negativo por guarda, con las reglas y cotas exactas de spec §3.4 (COP ∈ [1.000; 100.000.000], USD ∈ [50; 5.000.000] ¢, tachado `L > 20·P`, ±3 % de 10^k, lote n ≥ 5 con moda ≥ 80 %, salto ×0,5/×2 en ≤ 7 d). Incluye `test_g10_constant_banner_price_discards_batch` con 8 ofertas a `20_000` (el falso positivo de Alkosto del spike, spec §0.4) y `test_g2_cop_text_in_usd_source_raises` (caso U14).
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar `guards.py`.**
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(dominio): agrega guardas de validación de ofertas"`

---

### Task 8: `evaluate()` — madurez, veredictos, confirmación y banderas

**Files:**
- Create: `src/discountz/domain/evaluate.py`
- Test: `tests/unit/test_evaluate.py` (tabla U01–U37), `tests/unit/series.py` (helper para construir series)

**Interfaces:**
- Consumes: Tasks 4–7.
- Produces: `EvalContext` y `Evaluation` con los campos exactos de spec §3.10 (sin `landed_cop`); `WatchTerms(target_minor: int | None, target_set_at: datetime | None)`; `ListingState(id, category, alltime_min_minor, history_reset_at, last_price_minor, hot_until)`; `AlertRecord(kind, price_minor, reference_minor, created_at, segment_started_at, delivery)`; `evaluate(ctx: EvalContext) -> Evaluation`.
- Produces (`series.py`): `seg(start: str, end: str | None, price: int, **kw) -> Segment` donde `start`/`end` aceptan `"d-90"`, `"t"` relativos a `T = 2026-10-13T15:00Z`.

- [ ] **Step 1: Escribir la tabla de pruebas.** Una prueba parametrizada por fila de spec §3.11 (U01–U37), con la serie, la lectura y el veredicto y banderas esperados tal como aparecen en el spec. Para las filas que dependen de la confirmación (U09, U10, U12, U13, U13b, U16, U17, U35, U36), la prueba de `evaluate()` verifica el campo `confirmation` (`independent`, `persistence` o `forbidden`) y el veredicto candidato; el resultado final tras confirmar se prueba en Task 19.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar `evaluate.py`** siguiendo spec §3.5 (madurez y veredictos, rango, etiquetas), §3.6 (qué requiere confirmación) y §3.7 (banderas). La bandera `hot` se marca cuando `listing.hot_until > now`.
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(dominio): agrega la evaluación de ofertas contra el histórico"`

---

### Task 9: `decide()` y comandos de Telegram

**Files:**
- Create: `src/discountz/domain/decide.py`, `src/discountz/domain/commands.py`
- Test: `tests/unit/test_decide.py`, `tests/unit/test_commands.py`

**Interfaces:**
- Produces (`decide.py`): `RearmEvidence(above_reference_s: int, above_target_s: int)`; `Send(kind: AlertKind, silent: bool)`; `Skip(reason: str)`; `decide(ev, last, rearm, listing, now, cat) -> Send | Skip` (spec §3.8; `silent=True` entre 23:00 y 07:00 de Bogotá salvo `price_error`); `group_summaries(sends: Sequence[PendingAlert]) -> list[list[PendingAlert]]` (≥ 3 `good_deal`/`community_deal` de una categoría en el ciclo → un grupo de hasta 10).
- Produces (`commands.py`): comandos tipados `Follow(urls: tuple[str,...], target_minor_text: str | None, category: str | None)`, `Search(source, query, exclude, min_text, max_text, category)`, `ListCmd(filter: str | None)`, `Drop(ref: str)`, `Target(ref: str, price_text: str | None)`, `Category(ref, category)`, `SourceToggle(source: str, enabled: bool)`, `TokenExpiry(day: date)`; `CommandRejected(reason: str)`; `parse_command(text: str) -> Command | CommandRejected`. Sintaxis exacta: spec §5.3, menos `/peso`, más `/fuente on|off <code>` y `/token_vence AAAA-MM-DD` (spec §6.1).

- [ ] **Step 1: Escribir las pruebas que fallan**: filas D01–D06 de spec §3.11; `test_quiet_hours_silent_except_price_error` (02:00 Bogotá); `test_summary_groups_three_good_deals`; en comandos: `/seguir` con 1 y 5 URLs (y 6 → `CommandRejected`), objetivo `120`, `#componentes`; `/buscar exito ssd 1tb -carcasa -case max:400.000 #componentes`; `/objetivo p12 no`; `/objetivo p9 450.000cop` → `CommandRejected` (ya no hay COP sobre USD); comando desconocido → `CommandRejected`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar** `decide.py` y `commands.py` (puros; la validación de URLs con `SafeUrl` ocurre en Task 17).
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(dominio): agrega deduplicación de alertas y comandos"`

---

### Task 10: Salud de fuentes y circuit breaker

**Files:**
- Create: `src/discountz/domain/health.py`
- Test: `tests/unit/test_health.py`

**Interfaces:**
- Produces: `BreakerState(state: Literal["closed","open","half_open"], open_until: datetime | None, trips: int, consecutive_unavailable: int, consecutive_parse: int, quarantine_cycles: int)`; `BreakerEvent(StrEnum)` (`OK`, `HARD_BLOCK`, `UNAVAILABLE`, `PARSE_ERROR`, `PROBE_OK`, `PROBE_FAIL`); `breaker_transition(state, event, now, rnd: random.Random) -> BreakerState` (spec §4.6: abre con 1 bloqueo, 5 `unavailable` o 3 `parse`; `open_until = now + min(12 h, 30 min·2^(trips−1))·U(1,0;1,1)`; `trips` a 0 tras 24 h); `HourStat(requests, ok, blocked, parse_errors, soft_empty, confirm_mismatch)`; `classify_health(stats_24h: Sequence[HourStat], breaker: BreakerState, canary_in_range: bool | None, consecutive_empty_searches: int) -> Literal["healthy","degraded","blocked","broken"]` (spec §4.7).

- [ ] **Step 1: Escribir las pruebas que fallan**: cada transición del diagrama de spec §4.6 con `random.Random(0)`; backoff 30 min → 1 h → 2 h → tope 12 h; una por estado de la tabla de spec §4.7 y `test_three_soft_empty_cycles_is_broken`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar `health.py`.**
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(dominio): agrega circuit breaker y salud de fuentes"`

---

### Task 11: Planificador: presupuestos, prioridades, modo caliente y eventos

**Files:**
- Create: `src/discountz/domain/scheduling.py`
- Test: `tests/unit/test_scheduling.py`

**Interfaces:**
- Consumes: `BreakerState`, `params.py`.
- Produces: `SourceBudget(group: str, min_pause_s: float, jitter_s: float, max_per_run: int, max_per_24h: int, watch_interval: timedelta, search_interval: timedelta, max_batch: int)`; `Event(name: str, sources: frozenset[str], starts_at: datetime, ends_at: datetime)`; `Task` (`kind: Literal["confirm","canary","resolve","hot","watch_target","watch","search","feed"]`, `source`, `payload_id`); `PlanInput` (watches/listings/searches vencidos con `next_check_at`, `hot_until`, `has_target`, breakers, uso de 24 h, `ramp_until` por fuente, eventos, `discovery_paused`); `plan_run(inp: PlanInput, now: datetime) -> dict[str, list[Task]]`; `next_check_at(now, interval, failures: int, not_found: bool, rnd) -> datetime` (spec §4.5); `effective_interval(base, has_target: bool, hot: bool, events_active: bool, cycle: timedelta) -> timedelta` (spec §6.2: caliente → 1 ciclo; con objetivo → mitad; evento → ÷3).

- [ ] **Step 1: Escribir las pruebas que fallan**: el orden de prioridades de spec §6.2 (`confirm` > `canary` > `resolve` > `hot` > `watch_target` > `watch` > `search/feed`); el presupuesto por ejecución y por 24 h no se supera nunca, ni tras un retraso del cron (`test_no_burst_after_cron_delay`); rampa ×0,5; reserva de confirmación `max(2, 20 %)`; breaker `open` → 0 tareas, `half_open` → exactamente 1 (`canary`); durante un `Event` el presupuesto por ejecución sube 50 % sin pasar el de 24 h; los watches VTEX se agrupan en tareas de hasta `max_batch` refs; `discovery_paused` elimina `search`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar `scheduling.py`.**
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(dominio): agrega el planificador con prioridades y modo caliente"`

---

### Task 12: Migración 0001 y ejecutor de migraciones

**Files:**
- Create: `migrations/0001_init.up.sql`, `migrations/0001_init.down.sql`, `src/discountz/infra/migrate.py`, `src/discountz/infra/db.py` (solo `connect(url: str) -> psycopg.Connection`)
- Modify: `src/discountz/__main__.py` (subcomando `migrate --to N|head --wait-lease 20m`)
- Test: `tests/integration/test_migrations.py`, `tests/integration/conftest.py`

**Interfaces:**
- Produces: esquema `discountz` de spec §2.2 **con** `source_hourly_stat`, sin `fx_rate`, sin `weight_lb`/`target_basis`/`landed_cop`/`epoch`, con `listing.hot_until` + índice `listing_hot_ix`, `bot_state.dispatch_token_expires_on date` y los flags `hot`/`unverified` en el CHECK de `alert.flags`; semilla de `source` con las 9 fuentes de spec §4.1 (`walmart_us` y `bestbuy_us` con `enabled=false`). `migrate(conn, to: int | Literal["head"]) -> list[int]` con sha256 por archivo en `schema_migrations` (spec §7.6). `SCHEMA_VERSION = 1` en `infra/db.py`.
- Produces (conftest): fixture `db` que crea una BD temporal en `DISCOUNTZ_TEST_DATABASE_URL`, aplica `migrate(head)` y entrega una conexión.

- [ ] **Step 1: Escribir las pruebas de integración que fallan**: `up → down hasta 0 → up` sin errores; un segundo segmento abierto del mismo listing viola `price_segment_open_uq`; `watch` en `pending` con `listing_id` viola `watch_resolved_ck`; `money_minor` rechaza 0; editar un `.up.sql` ya aplicado hace que `migrate` falle por sha256 distinto.
- [ ] **Step 2: Verificar que fallan** — `docker run -d -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:17` y `DISCOUNTZ_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest -q -m integration`.
- [ ] **Step 3: Escribir los SQL y `migrate.py`.**
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(bd): agrega la migración inicial y su ejecutor"`

---

### Task 13: Repositorio SQL y lease

**Files:**
- Modify: `src/discountz/infra/db.py`
- Test: `tests/integration/test_db.py`

**Interfaces:**
- Produces (funciones, una sola implementación, sin Protocol; spec §2.3): `acquire_lease(conn, holder: str) -> bool`, `release_lease(conn, holder) -> None`, `start_run(conn, now, trigger_kind, gh_run_id, gh_run_attempt) -> int`, `finish_run(conn, run_id, status, counters)`, `load_plan_input(conn, now) -> PlanInput`, `load_history(conn, listing_ids: Sequence[int], since: datetime) -> dict[int, list[Segment]]` (con `= ANY`), `upsert_listing(conn, offer, category) -> ListingState`, `write_sample(conn, listing_id, obs, status, h) -> Segment` (usa `apply_sample`), `insert_alert(conn, ...) -> int | None` (`None` si choca `alert_dedup_uq`), `enqueue(conn, kind, chat_id, payload, priority, dedup_key) -> int`, `claim_outbox(conn, limit) -> list[OutboxRow]`, `mark_sent(conn, id, message_id)`, `save_breaker(conn, source, BreakerState)`, `add_hour_stat(conn, source, hour, HourStat)`, `bump_hot(conn, listing_id, until)`, CRUD de `watch`/`search`.

- [ ] **Step 1: Escribir las pruebas que fallan**: `test_two_cycles_only_one_gets_lease` (dos conexiones; la segunda recibe `False`); lease vencido (`expires_at < now()`) se reasigna; `write_sample` extiende y cierra segmentos (reutiliza U19); `insert_alert` duplicado devuelve `None`; `claim_outbox` no entrega dos veces la misma fila en dos llamadas seguidas.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar `db.py`.** Transacciones cortas con `with conn.transaction()`; nunca HTTP dentro.
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(bd): agrega el repositorio SQL y el lease"`

---

### Task 14: Cliente HTTP con ritmo, reintentos y detección de bloqueo

**Files:**
- Create: `src/discountz/infra/http.py`
- Test: `tests/unit/test_http.py`

**Interfaces:**
- Produces: `HttpResponse(status: int, url: str, headers: Mapping[str,str], body: bytes, elapsed_ms: int)`; `class HttpClient(Protocol): def request(self, method: Literal["GET","POST"], url: str, *, params=None, json=None, headers=None, cookies=None) -> HttpResponse`; `HttpxClient` y `CurlClient` (`curl_cffi`, `impersonate="chrome"`); `PacedClient(inner: HttpClient, budget: SourceBudget, sleep: Callable[[float],None], monotonic: Callable[[],float], rnd: random.Random, detect_block: Callable[[HttpResponse], BlockKind | None])` (Decorator: pausa mínima + jitter, presupuesto por ejecución → `BudgetExhausted`, reintento solo en GET con backoff exponencial y *full jitter* — 2 reintentos, respeta `Retry-After` ≤ 60 s —, `HardBlock` sin reintento, sin redirecciones automáticas); `FakeHttp(routes: Mapping[str, HttpResponse])` para pruebas. Tamaño máximo de respuesta 5 MB; timeout 20 s.
- Produces (`domain/errors.py`): `SourceError` con `kind` y subclases `HardBlock`, `Unavailable`, `BudgetExhausted`, `NotFound` (spec §7.1).

- [ ] **Step 1: Escribir las pruebas que fallan** con `FakeHttp`, reloj y `sleep` falsos: respeta pausa mínima; `BudgetExhausted` al pasar `max_per_run`; 503 → reintenta 2 veces y luego `Unavailable`; 429 con `Retry-After: 5` duerme 5 s; página de captcha → `HardBlock` sin reintento; POST nunca se reintenta; respuesta > 5 MB → `Unavailable`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar `http.py`.**
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(http): agrega clientes con ritmo, reintentos y bloqueo"`

---

### Task 15: Contrato de fuentes, registro y detección de bloqueos

**Files:**
- Create: `src/discountz/sources/base.py`, `src/discountz/sources/registry.py`, `src/discountz/sources/blocks.py`, `src/discountz/sources/fixtures.py`
- Modify: `src/discountz/__main__.py` (subcomando `record-fixture <source> <kind> <url-or-id>`), `pyproject.toml` (contrato «Adaptadores independientes»)
- Test: `tests/sources/test_registry.py`, `tests/sources/test_blocks.py`

**Interfaces:**
- Produces: todo `sources/base.py` exactamente como spec §4.2 (`Capability`, `SourceDescriptor`, `CredentialStore`, `NoCredentials`, `SourceContext`, `NeedsResolution`, `ListingFound|ListingGone|ListingFailed`, `SearchPage`, `Source`); `BlockKind(StrEnum)` y `detect_block_common(resp: HttpResponse) -> BlockKind | None` con los marcadores del spike (spec §0.4: `account-verification`, `/blocked?`, `px-captcha`, `Robot or human`, `validateCaptcha`, `cf-chl-`, `Just a moment...`, `captcha-delivery.com`); `SOURCES: Mapping[str, Source]` y `route_url(raw: str) -> tuple[Source, ListingRef | NeedsResolution]` que lanza `UnsafeUrl`; `load_fixture(source, name) -> HttpResponse` (lee `tests/fixtures/<source>/<name>.{json,html,xml}.gz` + `.meta.json`).
- `record-fixture` usa el `HttpClient` de la fuente, guarda el cuerpo gzip y el `.meta.json` (status, URL canónica, fecha) y **nunca** cookies ni cabeceras de respuesta.

- [ ] **Step 1: Escribir las pruebas que fallan**: `route_url("https://www.amazon.com/dp/B0EXAMPLE1?tag=x")` → `amazon_us` con ref `B0EXAMPLE1`; host no registrado → `UnsafeUrl`; `a.co/d/xyz` → `NeedsResolution("amazon_us", "short:a.co/d/xyz")`; cada marcador de bloqueo devuelve su `BlockKind` y una página normal devuelve `None`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar.** Registro vacío al principio; cada adaptador se registra en su tarea.
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(fuentes): agrega el contrato de adaptadores y el registro"`

---

### Task 16: Adaptador VTEX (Éxito, Carulla, Olímpica, Panamericana) y mini-spikes M2/M3

**Files:**
- Create: `src/discountz/sources/vtex.py`
- Create: `tests/fixtures/vtex/*.json.gz` (+ `.meta.json`, `.expected.json`) grabadas con `record-fixture`
- Test: `tests/sources/test_vtex.py`

**Interfaces:**
- Consumes: Task 15.
- Produces: `VtexStore(code, host, api_base)` para las 4 tiendas (Éxito con `/io/api/...`); `parse_vtex_products(body: bytes, store: VtexStore, observed_at: datetime) -> list[Offer]`; `parse_vtex_simulation(body: bytes, store, sku) -> Offer | None`; `VtexSource(store)` que implementa `Source` con `fetch_listings` en lotes `fq=skuId:` (tamaño `max_batch`), `search` (`ft=`, `_from=0&_to=49`), `confirm` (POST `/api/checkout/pub/orderForms/simulation?sc=1`), `resolve` (slug → `/api/catalog_system/pub/products/search/<slug>/p`). Reglas: spec §4.2 «VTEX».

- [ ] **Step 1: Mini-spike M2** (desechable, no se commitea): pedir 3 SKUs reales con `fq=skuId:A&fq=skuId:B&fq=skuId:C` a Éxito y Olímpica; si devuelve los 3 ítems en < 3 s, `max_batch = 50`, si no `max_batch = 1`. **M3**: una simulación anónima por tienda; si responde con `sellingPrice` coherente, la ruta independiente existe para esa tienda, si no `confirm` devuelve `None` en esa tienda. Anota ambos resultados en `docs/estado.md`.
- [ ] **Step 2: Grabar fixtures reales** con `python -m discountz record-fixture` por tienda: búsqueda (`ssd`, `televisor`, `cafe`, `portatil`), ficha en stock, ficha sin stock, producto con varios SKU, producto de vendedor de marketplace, una simulación. Escribe cada `.expected.json` a mano verificando el precio en la web.
- [ ] **Step 3: Escribir las pruebas que fallan**: cada fixture contra su `.expected.json`; `Price == 0` → `price=None` e `in_stock=False`; `L = max(ListPrice, PriceWithoutDiscount)`; vendedor ≠ `"1"` → `m`; máx. 3 ítems en stock por producto en búsquedas; simulación en centavos ÷ 100 con `Decimal`; respuesta con JSON truncado → `ParseError`.
- [ ] **Step 4: Verificar que fallan.**
- [ ] **Step 5: Implementar `vtex.py`** y registrar las 4 tiendas.
- [ ] **Step 6: Verificar que pasan** + comando completo.
- [ ] **Step 7: Commit** — `git commit -m "feat(fuentes): agrega el adaptador VTEX para 4 tiendas"`

---

### Task 17: Telegram: cliente, render y bandeja de entrada

**Files:**
- Create: `src/discountz/infra/telegram.py`, `src/discountz/app/render.py`, `src/discountz/app/inbox.py`
- Create: `tests/golden/*.html`
- Test: `tests/unit/test_render.py`, `tests/integration/test_inbox.py`

**Interfaces:**
- Consumes: `parse_command`, `route_url`, funciones de `db.py`.
- Produces (`telegram.py`): `TelegramClient(token: SecretStr, http: HttpClient)` con `get_updates(offset: int, timeout_s: int = 0) -> list[Update]`, `send_message(chat_id, html, buttons, silent: bool) -> int` (lanza `TelegramRateLimited(retry_after)` y `TelegramForbidden`), `edit_message`, `answer_callback`.
- Produces (`render.py`): `render_alert(a: AlertView) -> RenderedMessage(html: str, buttons: list[list[Button]])`, `render_summary(items: Sequence[AlertView]) -> RenderedMessage`, `render_list(...)`, `render_health(...)`, `render_daily(...)`. Formato exacto: spec §5.2 con la línea «🇺🇸 Compra por casillero» en listings USD y sin costo puesto.
- Produces (`inbox.py`): `process_inbox(conn, tg: TelegramClient, owner_id: int, chat_id: int, now: datetime, max_seconds: int = 20) -> int` (spec §5.4: offset en la misma transacción que el efecto; update venenoso en una segunda transacción; ajenos descartados sin tocar la BD; callbacks `mute:`, `unf:`, `fol:`).

- [ ] **Step 1: Escribir las pruebas que fallan**: golden HTML de cada tipo de alerta y del resumen; título hostil `<script>&"` escapado; texto visible ≤ 4.096 caracteres con título de 300; USD lleva «🇺🇸 Compra por casillero» y COP no; en `inbox`: `/seguir` de otro `from.id` no escribe nada y avanza el offset; `/seguir https://www.exito.com@evil.com/x` responde con rechazo; un update que lanza excepción no bloquea los siguientes; procesar dos veces el mismo `update_id` produce un solo `watch`.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar** los tres módulos.
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(telegram): agrega render de alertas y comandos"`

---

### Task 18: Configuración y composición

**Files:**
- Create: `src/discountz/config.py`, `config/discountz.toml`, `config/canaries.toml`
- Modify: `src/discountz/__main__.py` (`check-config`)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Settings` (pydantic-settings, prefijo `DISCOUNTZ_`, `SecretStr` para `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`; `TELEGRAM_CHAT_ID`, `TELEGRAM_OWNER_ID: int`; `HEALTHCHECKS_URL: SecretStr | None`; `SOFT_DEADLINE_S: int = 480`; `hide_input_in_errors=True`); `load_params(path: Path) -> AppParams` (categorías, presupuestos de spec §4.5, eventos de spec §6.2, horas de silencio) que devuelve dataclasses de dominio; `load_canaries(path) -> Mapping[str, list[Canary]]`. `config/discountz.toml` con todos los valores por defecto del spec y el calendario de eventos 2026-2027 (Black Days, Cyber Lunes, Hot Sale, Prime Day, Black Friday, Cyber Monday; fechas a confirmar por David en la revisión del PR).

- [ ] **Step 1: Escribir las pruebas que fallan**: el TOML del repo carga sin errores; un umbral negativo o una categoría desconocida falla con un mensaje que **no** contiene el valor del secreto; `check-config` sale 0 con el TOML del repo y variables de prueba.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(config): agrega configuración validada y calendario de eventos"`

---

### Task 19: El ciclo completo

**Files:**
- Create: `src/discountz/app/cycle.py`, `src/discountz/app/collect.py`, `src/discountz/app/confirm.py`, `src/discountz/app/compose.py`, `src/discountz/app/deliver.py`, `src/discountz/infra/log.py`, `src/discountz/infra/healthchecks.py`
- Modify: `src/discountz/__main__.py` (`run`)
- Test: `tests/e2e/test_cycle.py` (marcada `integration`)

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: `run_cycle(deps: CycleDeps, now: datetime) -> CycleResult(status: Literal["ok","partial","skipped_locked","skipped_schema","failed"], exit_code: int)` siguiendo los pasos de spec §1.3 (sin el paso de TRM). `CycleDeps` agrupa conexión, `SOURCES`, clientes HTTP por fuente, `TelegramClient`, `AppParams`, `Settings`, `monotonic`, `sleep`, `rnd`. Al detectar una bajada de precio en stock, `bump_hot(listing, now + 2 h)`. Códigos de salida: spec §1.3. Logs con `infra/log.py` (lista blanca de campos, `excepthook` mínimo).

- [ ] **Step 1: Escribir las pruebas e2e que fallan** con BD real, `FakeHttp` cargado con fixtures VTEX y un `FakeTelegram`:
  - `test_new_follow_creates_listing_and_replies` (`/seguir` → primera lectura → respuesta);
  - `test_price_error_confirmed_by_simulation_sends_once` (U09 de punta a punta);
  - `test_price_error_simulation_mismatch_no_alert` (U10);
  - `test_crash_after_commit_before_send_delivers_once` (el ciclo se corta después de `insert_alert`; el siguiente ciclo envía exactamente un mensaje);
  - `test_source_failure_marks_partial_and_others_continue` (una fuente lanza `HardBlock`, la otra produce alerta; exit 0, `partial`, breaker abierto y aviso de salud);
  - `test_price_drop_marks_hot_and_next_plan_prioritizes_it`;
  - `test_lease_taken_returns_skipped_locked`;
  - `test_logs_contain_no_secrets` (con `capfd`, buscando el token de prueba y la URL de la BD).
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar** los módulos de `app/`, `log.py` y `healthchecks.py` (no-op si `HEALTHCHECKS_URL` es `None`).
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(ciclo): orquesta recolección, confirmación y entrega"`

---

### Task 20: Mantenimiento, canarios y resumen diario

**Files:**
- Create: `src/discountz/app/maintenance.py`
- Test: `tests/integration/test_maintenance.py`

**Interfaces:**
- Produces: `run_maintenance(conn, now, params) -> MaintenanceReport` (spec §2.4: orden de los 4 pasos, guardas de 300/400/450 MB, reclasificación `g → o` a los 7 d); `run_canaries(...)` (1 vez al día por fuente; fuera de rango → cuarentena); `daily_summary(conn, now) -> AlertView | None` (primer ciclo tras las 08:00 de Bogotá: ciclos en 24 h por vía `schedule`/`workflow_dispatch`, alertas, salud por fuente, MB de BD, aviso de vencimiento del token de disparo a 14 días).

- [ ] **Step 1: Escribir las pruebas que fallan**: segmentos de > 400 d se borran y `alltime_min` se conserva; segmento `g` de 8 d pasa a `o` (U37); con tamaño simulado ≥ 450 MB se activa `discovery_paused`; el resumen cuenta por separado ciclos de `schedule` y de `workflow_dispatch`; con `dispatch_token_expires_on = hoy + 10 d`, el resumen incluye el aviso.
- [ ] **Step 2: Verificar que fallan.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Verificar que pasan** + comando completo.
- [ ] **Step 5: Commit** — `git commit -m "feat(ciclo): agrega mantenimiento, canarios y resumen diario"`

---

### Task 21: Workflows de despliegue y guía de puesta en marcha

**Files:**
- Create: `.github/workflows/cycle.yml`, `.github/workflows/migrate.yml`, `.github/workflows/keepalive.yml`
- Modify: `README.md` (guía completa), `docs/estado.md`

**Interfaces:** workflows de spec §7.5 con `cycle.yml` en `schedule: '7,37 * * * *'` + `workflow_dispatch`, `concurrency: discountz-cycle` sin cancelar, `timeout-minutes: 15`; `keepalive.yml` solo con `cycle.yml` y `keepalive.yml`; `migrate.yml` manual con environment `production-db`.

- [ ] **Step 1: Validar los workflows** con `uvx zizmor .github/workflows` (sin hallazgos altos) y `actionlint` si está disponible.
- [ ] **Step 2: Escribir la guía en `README.md`** (paso a paso, en español):
  1. Crear el bot con @BotFather (`/newbot`), guardar el token; escribirle al bot y obtener `chat_id` y tu `from.id` con `https://api.telegram.org/bot<TOKEN>/getUpdates`.
  2. Crear el proyecto en Supabase, apagar la Data API, crear el esquema `discountz` y los roles `discountz_owner` y `discountz_app` (SQL de spec §7.3), copiar la URL del pooler en modo sesión (`:5432`).
  3. Crear los secretos del repo (`DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_OWNER_ID`, opcional `HEALTHCHECKS_PING_URL`) y el environment `production-db` con `DATABASE_URL_MIGRATOR` y revisor David.
  4. Correr `migrate` (Actions → migrate → Run workflow → `head`).
  5. Crear el token fine-grained (solo este repo, Actions: read and write, 12 meses) y el trabajo en cron-job.org: cada 5 min, `POST https://api.github.com/repos/DavidRincon12/discountZ/actions/workflows/cycle.yml/dispatches`, cuerpo `{"ref":"main"}`, cabeceras de spec §6.1. En Telegram: `/token_vence AAAA-MM-DD`.
  6. Primer seguimiento: `/seguir <url de Éxito>`.
- [ ] **Step 3: Commit** — `git commit -m "ci: agrega workflows del ciclo, migraciones y keepalive"`

---

### Task 22: Adaptador Amazon US

**Files:**
- Create: `src/discountz/sources/amazon.py`, `tests/fixtures/amazon_us/*`
- Test: `tests/sources/test_amazon.py`

**Interfaces:**
- Produces: `parse_amazon_detail(body: bytes, asin: str, observed_at) -> Offer | None`, `parse_amazon_independent(body, asin, observed_at) -> Offer | None` (`input#twister-plus-price-data-price`; `None` si no existe), `parse_amazon_search(body, observed_at) -> list[Offer]`, `AmazonSource` (cliente `curl`, cookies `i18n-prefs=USD`, `lc-main=en_US`, rampa 7 d). Reglas: spec §4.2 «Amazon».

- [ ] **Step 1: Grabar fixtures reales**: búsqueda `ssd 1tb`, ficha vendida por Amazon, ficha de vendedor tercero, ficha sin stock, ficha sin `twister-plus-price-data-price`, página de captcha.
- [ ] **Step 2: Escribir las pruebas que fallan**: cada fixture contra su `.expected.json`; patrocinados, «More Buying Choices», «See options» y usados se descartan; captcha → `detect_block` = `CAPTCHA`; resultados de búsqueda sin nodo de precio principal → `in_stock=False`; texto con `COP` → `ParseError` (U14).
- [ ] **Step 3: Verificar que fallan.**
- [ ] **Step 4: Implementar y registrar.**
- [ ] **Step 5: Verificar que pasan** + comando completo.
- [ ] **Step 6: Commit** — `git commit -m "feat(fuentes): agrega el adaptador de Amazon US"`

---

### Task 23: Adaptador Newegg

**Files:**
- Create: `src/discountz/sources/newegg.py`, `tests/fixtures/newegg/*`
- Test: `tests/sources/test_newegg.py`

**Interfaces:**
- Produces: `parse_newegg_detail` (JSON-LD `Product.offers`), `parse_newegg_independent` (`.price-current`), `parse_newegg_search`, `NeweggSource` (cliente `httpx`, rampa 7 d). «Sold by Newegg» → `f`.

- [ ] **Step 1: Grabar fixtures** (búsqueda, ficha Newegg, ficha de tercero, sin stock).
- [ ] **Step 2: Escribir las pruebas que fallan** (fixtures vs. `.expected.json`; JSON-LD y HTML coinciden en todas las fichas).
- [ ] **Step 3: Verificar que fallan.**
- [ ] **Step 4: Implementar y registrar.**
- [ ] **Step 5: Verificar que pasan** + comando completo.
- [ ] **Step 6: Commit** — `git commit -m "feat(fuentes): agrega el adaptador de Newegg"`

---

### Task 24: Feed de Slickdeals

**Files:**
- Create: `src/discountz/sources/slickdeals.py`, `tests/fixtures/slickdeals/*`
- Test: `tests/sources/test_slickdeals.py`, ampliar `tests/e2e/test_cycle.py`

**Interfaces:**
- Produces: `parse_slickdeals_rss(body: bytes, now: datetime) -> list[Lead]` (`defusedxml`; URL de tienda por `href` directo o parámetro `u2=` URL-decodificado sin red; descarta leads de > 24 h); `SlickdealsSource` (`kind="feed"`, `fetch_leads`). En `app/collect.py`: los leads que coinciden con una `search` de `source='slickdeals'` y cuya URL resuelve a una tienda activa encolan una lectura de ficha con prioridad `confirm` (omitida si se leyó hace < 6 h); veredicto `community_deal` (spec §3.5 y §4.3).

- [ ] **Step 1: Grabar el RSS real.**
- [ ] **Step 2: Escribir las pruebas que fallan**: extracción de título, precio y URL `u2=`; lead de Walmart con Walmart desactivado se ignora; U34 y U34b de punta a punta en e2e.
- [ ] **Step 3: Verificar que fallan.**
- [ ] **Step 4: Implementar y registrar.**
- [ ] **Step 5: Verificar que pasan** + comando completo.
- [ ] **Step 6: Commit** — `git commit -m "feat(fuentes): agrega el feed comunitario de Slickdeals"`

---

### Task 25: Walmart y Best Buy detrás de la puerta de activación

**Files:**
- Create: `src/discountz/sources/walmart.py`, `src/discountz/sources/bestbuy.py`, `tests/fixtures/walmart_us/*`, `tests/fixtures/bestbuy_us/*`
- Test: `tests/sources/test_walmart.py`, `tests/sources/test_bestbuy.py`

**Interfaces:**
- Produces: `parse_walmart_detail` (`__NEXT_DATA__`: `priceInfo.currentPrice.price`, sin nodos `warranty`/`protection`), `parse_walmart_independent` (JSON-LD), `parse_walmart_search`, `WalmartSource` (cliente `curl`); `parse_bestbuy_detail` (JSON-LD), `parse_bestbuy_independent` (`.priceView-customer-price span[aria-hidden=true]`), `parse_bestbuy_search`, `BestBuySource` (cliente `curl`, `intl=nosplash`). Ambas quedan `enabled=false` en la semilla; se activan con `/fuente on <code>` (spec §4.1).

- [ ] **Step 1: Grabar fixtures** incluyendo una ficha de Walmart con plan de protección (el `24,00` del spike) y una búsqueda de Best Buy (el `49,99` constante).
- [ ] **Step 2: Escribir las pruebas que fallan**: la ficha con plan de protección devuelve el precio del producto, no `2400`; la búsqueda de Best Buy no devuelve un precio constante para todos los ítems (G10 no se dispara); la ruta independiente coincide con la principal en el 100 % de las fichas.
- [ ] **Step 3: Verificar que fallan.**
- [ ] **Step 4: Implementar y registrar.**
- [ ] **Step 5: Verificar que pasan** + comando completo.
- [ ] **Step 6: Commit** — `git commit -m "feat(fuentes): agrega Walmart y Best Buy con puerta de activación"`

---

### Task 26: Cierre de la v1

**Files:**
- Modify: `docs/estado.md`, `docs/architecture.md`, `CLAUDE.md`

- [ ] **Step 1: Correr la suite completa**, incluida la de integración (`uv run pytest -q -m integration` con Postgres 17 en Docker), y reportar el conteo de pruebas.
- [ ] **Step 2: Actualizar `docs/estado.md`** (siguiente paso: puesta en marcha según README y, después, SP-6 canales de Telegram) y los gotchas descubiertos (resultados de M2/M3, fixtures).
- [ ] **Step 3: Commit** — `git commit -m "docs: actualiza el estado al cierre de la v1"`
