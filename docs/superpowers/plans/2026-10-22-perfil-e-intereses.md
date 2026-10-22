# Perfil de temas e intereses Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que solo lleguen en tarjeta las ofertas de los temas del perfil de David o las que coinciden con un interés suyo, y que el rastreo deje de recorrer lo que no le sirve.

**Architecture:** Cada tema de `config/topics.toml` gana un modo (`card`, `interest`, `off`). El dominio decide qué particiones se rastrean (`crawl_wanted`) y qué título coincide con un interés (`match_interest`); `app/crawl.py`, `app/channels.py` y `app/held.py` aplican esas decisiones antes de confirmar o encolar. Los intereses vienen de `config/interests.toml` y de la tabla `interest` (migración `0012`), que llena `/interes`.

**Tech Stack:** Python 3.13 síncrono, pydantic v2, psycopg 3, pytest, uv. Sin dependencias nuevas.

**Spec:** `docs/superpowers/specs/2026-10-22-perfil-e-intereses-design.md`

## Global Constraints

- Regla de capas `app → sources → infra → domain` (import-linter); `domain/` sigue puro.
- Modos (spec §1.1): `tecnologia`, `electro`, `ferreteria`, `moda`, `libros` = `card`; `hogar`, `deportes`, `otros` = `interest`; `belleza`, `mercado`, `bebes`, `mascotas` = `off`; `errores` y `comunidad_us` quedan en el `card` por defecto.
- Intereses (spec §3): `daily_cards_per_interest = 3`; hasta 100 personales, 10 términos por interés, 40 caracteres por término, nombre de 1 a 40 caracteres y único sin importar mayúsculas ni tildes; un término de una sola palabra con menos de 3 letras se rechaza (en `exclude` sí vale).
- Los logs solo usan los campos de `ALLOWED_FIELDS` (`infra/log.py`); nunca nombres ni términos de intereses.
- Texto del producto en español de Colombia. Commits `tipo(alcance): descripción en español`, a nombre de David y sin atribución a IA; `bash .claude/hooks/git-guard.sh check` antes de cada push.
- Verificación completa antes de cada commit: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest -q`; las tareas con BD corren también `DISCOUNTZ_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest -q -m integration`.
- PR 1 = Tasks 1-5 (perfil). PR 2 = Tasks 6-12 (intereses), en la rama de la sesión reiniciada desde `main` después de fusionar el PR 1.

## Review Focus

- Una tienda con todas sus particiones fuera del perfil: el barrido termina en `ok` sin pedir páginas ni quedarse en bucle (prueba en Task 3).
- Un cursor guardado antes del despliegue (particiones como texto) sigue funcionando: el rastreo continúa desde ahí (prueba en Task 3).
- Números y signos en títulos y términos: «RTX-5070» coincide con `rtx 5070` y «SSD 1 TB» con `ssd 1tb` (prueba en Task 6).
- Un interés personal con el nombre de uno de la semilla, con otras mayúsculas o tildes, se rechaza (prueba en Task 12).
- Un post de canal que coincide con un interés pero es duplicado de otro ya enviado no se reenvía (prueba en Task 11).

---

## PR 1: perfil de temas

### Task 1: Modo por tema y ADR 0013

**Files:**
- Modify: `src/discountz/domain/topics.py` (`TopicParams`)
- Modify: `src/discountz/config.py:443-521` (`_Topic`, `load_topics`)
- Modify: `config/topics.toml` (`mode` en cada `[topics.*]`)
- Create: `docs/decisions/0013-perfil-de-temas-e-intereses.md`
- Modify: `docs/architecture.md` (flujo del rastreo y de los canales: una línea sobre el perfil)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `TopicMode = Literal["card", "interest", "off"]` en `domain/topics.py`; `TopicParams.mode: TopicMode = "card"` (último campo, con valor por defecto para no romper las construcciones posicionales).

- [ ] **Step 1: Write the failing tests**

```python
def test_load_topics_reads_profile_modes() -> None:
    params = load_topics().params
    assert {c for c, p in params.items() if p.mode == "card"} >= {
        "tecnologia", "electro", "ferreteria", "moda", "libros"}
    assert {c for c, p in params.items() if p.mode == "interest"} == {"hogar", "deportes", "otros"}
    assert {c for c, p in params.items() if p.mode == "off"} == {
        "belleza", "mercado", "bebes", "mascotas"}

def test_load_topics_rejects_unknown_mode(tmp_path: Path) -> None:
    # copia de config/topics.toml con mode = "quiza" en [topics.hogar]
    with pytest.raises(ConfigError, match="topics.hogar"):
        load_topics(bad)
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/unit/test_config.py -q -k mode`
Expected: FAIL (`TopicParams` no tiene `mode`).

- [ ] **Step 3: Implement**

`_Topic.mode: Literal["card", "interest", "off"] = "card"`, pasado a `TopicParams`. En `config/topics.toml`, `mode = "…"` en cada tema según Global Constraints. ADR 0013 con la plantilla de `docs/decisions/` (contexto: ~60 tarjetas al día de cualquier tema; decisión: modo por tema, rastreo por perfil e intereses que saltan perfil, tope y precio mínimo; alternativas: puntuación ponderada y todo por comandos en la BD; cuándo revisar: los umbrales del spec §9). Enlaza el spec.

- [ ] **Step 4: Run them and see them pass**

Run: `uv run pytest tests/unit/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discountz/domain/topics.py src/discountz/config.py config/topics.toml docs/decisions/0013-perfil-de-temas-e-intereses.md docs/architecture.md tests/unit/test_config.py
git commit -m "feat(perfil): modo por tema y ADR 0013"
```

### Task 2: Qué particiones se rastrean

**Files:**
- Modify: `src/discountz/domain/topics.py`
- Test: `tests/unit/test_topics.py`

**Interfaces:**
- Consumes: `TopicParams.mode` (Task 1), `assign_topic`, `normalize_path`.
- Produces: `crawl_wanted(rules: Sequence[TopicRule], params: Mapping[str, TopicParams], source: str, path: str) -> bool`.

- [ ] **Step 1: Write the failing tests** (reglas y modos armados en la prueba, sin leer el TOML)

```python
def test_crawl_wanted_root_path_is_always_crawled() -> None:
    assert crawl_wanted(RULES, PARAMS, "exito", "/")

def test_crawl_wanted_skips_off_topic_root() -> None:
    assert not crawl_wanted(RULES, PARAMS, "exito", "/Mercado/")

def test_crawl_wanted_keeps_card_and_interest_roots() -> None:
    assert crawl_wanted(RULES, PARAMS, "exito", "/Tecnología/")
    assert crawl_wanted(RULES, PARAMS, "exito", "/Hogar/")  # interest

def test_crawl_wanted_keeps_root_with_deeper_wanted_rule() -> None:
    # regla `panamericana:/belleza/herramientas/` → ferreteria (card) bajo una raíz `off`
    assert crawl_wanted(RULES, PARAMS, "panamericana", "/Belleza/")
    assert not crawl_wanted(RULES, PARAMS, "exito", "/Belleza/")  # la regla es de otra fuente
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/unit/test_topics.py -q -k crawl_wanted`
Expected: FAIL (`crawl_wanted` no existe).

- [ ] **Step 3: Implement `crawl_wanted`**

Ruta normalizada `/` → `True`. Si no, `True` si el modo del tema de `assign_topic(rules, source, path)` no es `off`, o si alguna regla de `source` o `*` con `prefix` más largo que la ruta y que empieza por ella lleva a un tema que no es `off`.

- [ ] **Step 4: Run them and see them pass**

Run: `uv run pytest tests/unit/test_topics.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discountz/domain/topics.py tests/unit/test_topics.py
git commit -m "feat(perfil): decide qué categorías rastrear según el perfil"
```

### Task 3: Particiones con ruta y barrido por perfil

**Files:**
- Modify: `src/discountz/sources/base.py:160-190` (`CatalogPartition`, `CatalogSource.catalog_partitions`)
- Modify: `src/discountz/sources/vtex.py` (`_CategoryNode.name`, `catalog_partitions`)
- Modify: `src/discountz/sources/ebay.py` (`_TreeCategory.categoryName`, `_partitions`, `catalog_partitions`)
- Modify: `src/discountz/sources/alkosto.py`, `src/discountz/sources/bestbuy.py` (`catalog_partitions`)
- Modify: `src/discountz/app/crawl.py:206-222` (`sweep`)
- Test: `tests/sources/test_vtex_catalog.py`, `tests/sources/test_ebay.py`, `tests/sources/test_alkosto.py`, `tests/sources/test_bestbuy.py`, `tests/integration/test_crawl.py` (y su fuente falsa en la línea 158)

**Interfaces:**
- Consumes: `crawl_wanted` (Task 2).
- Produces: `@dataclass(frozen=True, slots=True) class CatalogPartition: key: str; path: str = "/"`; `catalog_partitions(ctx) -> Sequence[CatalogPartition]`. El cursor y `CatalogPage.split` siguen con claves `str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/sources/test_vtex_catalog.py (reemplaza test_catalog_partitions_are_root_categories)
def test_catalog_partitions_are_root_categories_with_their_path() -> None:
    parts = VtexSource(STORES["exito"]).catalog_partitions(ctx(fake))
    assert CatalogPartition("C:/34185084/", "/Tecnología/") in parts
    assert len(parts) == len(json.loads(tree.body))

# tests/sources/test_ebay.py: cada partición de nivel 3 lleva la ruta de nombres de su raíz a ella
assert CatalogPartition("<id nivel 3>", "/<raíz>/<nivel 2>/<nivel 3>/") in parts
# tests/sources/test_alkosto.py y test_bestbuy.py
assert list(src.catalog_partitions(ctx)) == [CatalogPartition("1-999999999")]

# tests/integration/test_crawl.py
def test_crawl_skips_partitions_outside_the_profile(db) -> None:
    # fuente falsa con "/tecnologia/" y "/mercado/": solo se pide la página de la primera
def test_crawl_with_every_partition_off_ends_ok_without_requests(db) -> None:
    # todas "/mercado/": status "ok", 0 páginas, sin bucle
def test_crawl_resumes_a_cursor_saved_with_plain_keys(db) -> None:
    # cursor guardado con partitions=("C:/1/", "C:/2/") y partition_idx=1: sigue en "C:/2/"
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/sources -q -k partitions` y `… -m integration tests/integration/test_crawl.py -k "profile or every_partition or plain_keys"`
Expected: FAIL.

- [ ] **Step 3: Implement**

VTEX: `CatalogPartition(f"C:/{n.id}/", f"/{n.name}/")`. eBay: `_partitions(node, names)` acumula `categoryName` desde los hijos de `rootCategoryNode` y devuelve `CatalogPartition(id, "/" + "".join(f"{n}/" for n in names))`, igual que `_category_path`. Alkosto `CatalogPartition(f"1-{PRICE_MAX}")`; Best Buy `CatalogPartition("all")`. En `sweep`: `keys = tuple(p.key for p in parts if crawl_wanted(topics.rules, topics.params, code, p.path))`; si se saltó alguna, `event(log, "crawl_partitions_skipped", source=code, count=n)`; si `keys` queda vacío, `return "ok"` sin guardar un barrido vacío.

- [ ] **Step 4: Run them and see them pass**

Run: la verificación completa de Global Constraints (unitarias e integración).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discountz/sources src/discountz/app/crawl.py tests/sources tests/integration/test_crawl.py
git commit -m "feat(perfil): el rastreo salta las categorías fuera del perfil"
```

### Task 4: El rastreo no confirma lo que está fuera del perfil

**Files:**
- Modify: `src/discountz/app/crawl.py:342-372` (`process`), `:709-719` (`crawl_done`)
- Test: `tests/integration/test_crawl.py`

**Interfaces:**
- Consumes: `TopicParams.mode` (Task 1).
- Produces: contador `CrawlRun.off_profile: int`; `crawl_done` registra `skipped=off_profile`.

- [ ] **Step 1: Write the failing tests**

```python
def test_drop_in_a_non_card_topic_is_not_confirmed(db) -> None:
    # bajada real en "/belleza/…" con historia: sin llamada a fetch_catalog_listings,
    # sin fila en alert, la fila del catálogo sí se guarda y el resultado cuenta 0 alertas
def test_drop_in_a_card_topic_still_alerts(db) -> None:
    # la misma bajada en "/tecnologia/…" sale como hoy
```

- [ ] **Step 2: Run them and see them fail**

Run: `DISCOUNTZ_TEST_DATABASE_URL=… uv run pytest -q -m integration tests/integration/test_crawl.py -k "non_card or card_topic"`
Expected: FAIL (hoy confirma y alerta en belleza).

- [ ] **Step 3: Implement**

En `process`, tras `best_candidate`: si `self.deps.topics.params[topic].mode != "card"`, el candidato se descarta y `self.off_profile += 1`. El estado del catálogo se guarda igual que sin candidato.

- [ ] **Step 4: Run them and see them pass**

Run: la verificación completa.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discountz/app/crawl.py tests/integration/test_crawl.py
git commit -m "feat(perfil): el rastreo solo confirma ofertas de temas en tarjeta"
```

### Task 5: Canales por perfil, docs y estado

**Files:**
- Modify: `src/discountz/app/channels.py:328-346` (`_handle`)
- Modify: `README.md` (sección «17. Perfil de temas» tras la 16), `CLAUDE.md` (una línea en Convenciones y gotchas), `docs/estado.md` (reescrito)
- Test: `tests/integration/test_channels.py`

**Interfaces:**
- Consumes: `TopicParams.mode` (Task 1).

- [ ] **Step 1: Write the failing tests**

```python
def test_post_in_a_non_card_topic_is_marked_without_sending(db) -> None:
    # post de "/hogar/" (una silla sin interés): queda en channel_post con baseline, sin outbox,
    # y el resumen de la pasada lo cuenta en skipped
def test_post_in_a_card_topic_is_still_sent(db) -> None:
```

- [ ] **Step 2: Run them and see them fail**

Run: `… uv run pytest -q -m integration tests/integration/test_channels.py -k card_topic`
Expected: FAIL.

- [ ] **Step 3: Implement**

En `_handle`, después de `assign_channel_topic`: si el modo del tema no es `card`, `_mark(step, code, post, topic)`, `tally.skipped += 1` y `return`. README: qué temas llegan, cuáles no y dónde se cambia (`mode` en `config/topics.toml`). `docs/estado.md`: perfil activo, Slickdeals encendido por decisión de David pese a sus términos (spec §7), siguiente paso = PR 2 (Task 6 de este plan, Native).

- [ ] **Step 4: Run them and see them pass**

Run: la verificación completa.
Expected: PASS.

- [ ] **Step 5: Commit y PR 1**

```bash
git add src/discountz/app/channels.py tests/integration/test_channels.py README.md CLAUDE.md docs/estado.md
git commit -m "feat(perfil): los canales solo envían temas en tarjeta"
```

Revisión final del PR 1 (`superpowers:requesting-code-review` con el ajuste de David), `git-guard.sh check`, push y PR con la plantilla del repo. Sin migración.

---

## PR 2: intereses

### Task 6: Coincidencia de intereses

**Files:**
- Create: `src/discountz/domain/interests.py`
- Test: `tests/unit/test_interests.py`

**Interfaces:**
- Consumes: `normalize_title` (`domain/model.py`), `stem` (`domain/community.py`).
- Produces:
  - `@dataclass(frozen=True, slots=True) class Interest: name: str; topic: str; terms: tuple[tuple[str, ...], ...]; exclude: tuple[tuple[str, ...], ...]; personal_id: int | None = None` (términos ya normalizados y en singular).
  - `term_problem(term: str) -> str | None`: motivo en español si el término no vale (vacío al normalizar, más de 40 caracteres, una sola palabra de menos de 3 letras).
  - `compile_interest(name: str, topic: str, terms: Sequence[str], exclude: Sequence[str] = (), personal_id: int | None = None) -> Interest`.
  - `match_interest(title: str, interests: Sequence[Interest]) -> Interest | None`.
  - `interest_key(name: str) -> str`: nombre normalizado para comparar repetidos.

- [ ] **Step 1: Write the failing tests**

```python
def test_matches_spanish_and_english_terms() -> None:
    i = compile_interest("Multímetro", "ferreteria", ["multimetro", "multimeter"])
    assert match_interest("Multímetro Digital UNI-T", [i]) == i
    assert match_interest("AstroAI Digital Multimeter", [i]) == i

def test_matches_plural_and_accents() -> None:
    i = compile_interest("Audífonos", "tecnologia", ["audifonos"])
    assert match_interest("Audífono Bluetooth", [i]) == i

def test_phrase_must_be_contiguous() -> None:
    i = compile_interest("Silla", "hogar", ["silla ergonomica"])
    assert match_interest("Silla Ergonómica de Oficina", [i]) == i
    assert match_interest("Silla de comedor ergonómica", [i]) is None

def test_exclude_cancels_the_match() -> None:
    i = compile_interest("Monitor", "tecnologia", ["monitor"], exclude=["bebe", "baby"])
    assert match_interest("Monitor de bebé con cámara", [i]) is None

def test_longest_term_wins_then_file_order() -> None:
    mon = compile_interest("Monitor", "tecnologia", ["monitor"])
    arm = compile_interest("Soporte", "hogar", ["monitor arm"])
    assert match_interest("Dual Monitor Arm", [mon, arm]) == arm

def test_numbers_and_punctuation_normalize_alike() -> None:
    gpu = compile_interest("RTX 5070", "tecnologia", ["rtx 5070"])
    ssd = compile_interest("SSD", "tecnologia", ["ssd 1tb"])
    assert match_interest("Tarjeta RTX-5070 12GB", [gpu]) == gpu
    assert match_interest("SSD 1 TB NVMe", [ssd]) == ssd

def test_empty_title_matches_nothing() -> None:
    assert match_interest("", [compile_interest("SSD", "tecnologia", ["ssd"])]) is None

def test_term_problem() -> None:
    assert term_problem("ti") is not None and term_problem("ti 84") is None
    assert term_problem("x" * 41) is not None and term_problem("¿?") is not None
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/unit/test_interests.py -q`
Expected: FAIL (módulo inexistente).

- [ ] **Step 3: Implement `domain/interests.py`**

Cada término: `tuple(stem(t) for t in normalize_title(term).split())`. `match_interest` tokeniza el título igual, busca cada término como subsecuencia contigua, descarta los intereses con algún `exclude` presente y elige por `(más palabras del término, menor posición en la lista)`.

- [ ] **Step 4: Run them and see them pass**

Run: `uv run pytest tests/unit/test_interests.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discountz/domain/interests.py tests/unit/test_interests.py
git commit -m "feat(intereses): coincidencia de intereses en español e inglés"
```

### Task 7: Semilla de intereses

**Files:**
- Create: `config/interests.toml` (los 55 intereses del spec §4, agrupados por tema con un comentario por grupo, y `daily_cards_per_interest = 3`)
- Modify: `src/discountz/config.py` (`load_interests`), `src/discountz/__main__.py` (`check-config` la carga)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `compile_interest`, `term_problem`, `interest_key` (Task 6), `TOPIC_CODES`.
- Produces: `@dataclass(frozen=True, slots=True) class InterestsConfig: daily_cards_per_interest: int; seeds: tuple[Interest, ...]`; `load_interests(path: Path = INTERESTS_PATH) -> InterestsConfig`.

- [ ] **Step 1: Write the failing tests**

```python
def test_seed_interests_load() -> None:
    cfg = load_interests()
    assert cfg.daily_cards_per_interest == 3
    assert len(cfg.seeds) == 55
    assert {i.topic for i in cfg.seeds} <= set(TOPIC_CODES) - {"errores", "comunidad_us"}

@pytest.mark.parametrize("problem", ["unknown_topic", "no_terms", "repeated_name", "long_term", "short_term"])
def test_load_interests_rejects(problem: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_interests(write_bad(tmp_path, problem))
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/unit/test_config.py -q -k interests`
Expected: FAIL.

- [ ] **Step 3: Implement** el modelo pydantic estricto (`_Interest`: `name`, `topic`, `terms`, `exclude = []`; `_Interests`: `daily_cards_per_interest` entre 1 y 20, `interests`) y la validación con `term_problem` e `interest_key`. El archivo copia la tabla del spec §4 tal cual.

- [ ] **Step 4: Run them and see them pass**

Run: `uv run pytest tests/unit/test_config.py -q && uv run python -m discountz check-config`
Expected: PASS y `check-config` en verde.

- [ ] **Step 5: Commit**

```bash
git add config/interests.toml src/discountz/config.py src/discountz/__main__.py tests/unit/test_config.py
git commit -m "feat(intereses): lista inicial de 55 intereses en dos idiomas"
```

### Task 8: Migración 0012 y acceso a datos

**Files:**
- Create: `migrations/0012_intereses.up.sql`, `migrations/0012_intereses.down.sql` (el SQL del spec §3.5, con `SET LOCAL lock_timeout`, `search_path` y el `GRANT` a `discountz_app` como en `0010`)
- Modify: `src/discountz/infra/db.py` (`SCHEMA_VERSION = 12`, `OfferEntry.interest`, `log_offer`, `count_cards`, funciones nuevas)
- Test: `tests/integration/test_migrations.py`, `tests/integration/test_held.py`, `tests/integration/test_db.py`

**Interfaces:**
- Produces:
  - `OfferEntry.interest: str | None = None` (último campo).
  - `count_cards(...)` cuenta solo `interest IS NULL`.
  - `count_interest_cards(conn, name: str, day: date) -> int`.
  - `@dataclass(frozen=True, slots=True) class InterestRow: id: int; name: str; topic: str; terms: tuple[str, ...]`.
  - `list_interests(conn) -> list[InterestRow]` (por `id`), `add_interest(conn, name: str, topic: str, terms: Sequence[str], now: datetime) -> int`, `delete_interest(conn, interest_id: int) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
def test_migration_0012_up_and_down(db) -> None:  # tabla interest y columna offer_log.interest van y vuelven
def test_interest_cards_do_not_count_for_the_source_cap(db) -> None:
    # 2 tarjetas con interest="SSD" y 1 sin interés el mismo día: count_cards == 1,
    # count_interest_cards("SSD") == 2
def test_add_list_and_delete_interests(db) -> None:
```

- [ ] **Step 2: Run them and see them fail**

Run: `… uv run pytest -q -m integration -k "0012 or interest"`
Expected: FAIL.

- [ ] **Step 3: Implement** la migración y las funciones (consultas parametrizadas).

- [ ] **Step 4: Run them and see them pass**

Run: la verificación completa.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add migrations/0012_intereses.up.sql migrations/0012_intereses.down.sql src/discountz/infra/db.py tests/integration
git commit -m "feat(intereses): migración 0012 con la tabla de intereses"
```

### Task 9: Tope por interés en la admisión

**Files:**
- Modify: `src/discountz/app/held.py` (`Slot`, `admit`, `_hold_reason`)
- Test: `tests/integration/test_held.py`

**Interfaces:**
- Consumes: `count_interest_cards`, `OfferEntry.interest` (Task 8).
- Produces: `Slot.interest: str | None = None`; `admit(conn, slot, now, daily_cards, line, per_interest: int = 3) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
def test_interest_slot_skips_a_full_source_cap(db) -> None:
    # la fuente ya tiene 8 tarjetas hoy: un Slot con interest="SSD" sale en tarjeta
def test_interest_cap_holds_the_fourth_card(db) -> None:
    # 3 tarjetas de "SSD" hoy: la cuarta queda retenida (va al resumen)
def test_interest_slot_keeps_the_promotion_rule(db) -> None:
```

- [ ] **Step 2: Run them and see them fail**

Run: `… uv run pytest -q -m integration tests/integration/test_held.py -k interest`
Expected: FAIL.

- [ ] **Step 3: Implement**: en `_hold_reason`, después de la regla de promoción, un `Slot` con interés se retiene solo con `count_interest_cards(...) >= per_interest` (código `interest_cap`); `admit` guarda `interest` en `OfferEntry`.

- [ ] **Step 4: Run them and see them pass**

Run: la verificación completa.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discountz/app/held.py tests/integration/test_held.py
git commit -m "feat(intereses): tope de 3 tarjetas por interés al día"
```

### Task 10: Intereses en el rastreo

**Files:**
- Modify: `src/discountz/domain/catalog.py` (`CatalogObservation.title`, `observe(..., floors=True)`)
- Modify: `src/discountz/sources/vtex.py` (`parse_vtex_catalog`: `productName`), `src/discountz/sources/alkosto.py` (`ATTRIBUTES` + `name_text_es`), `src/discountz/sources/ebay.py` (`_observation`: `title`)
- Create: `src/discountz/app/interests.py`
- Modify: `src/discountz/app/crawl.py` (`CrawlDeps.interests`, `CrawlDeps.interest_cap`, `_Found.interest`, `process`, `confirm`, `_card`), `src/discountz/app/render.py` (`render_catalog_alert`), `src/discountz/__main__.py` (pasa los intereses activos al iniciar el rastreo)
- Test: `tests/unit/test_catalog.py`, `tests/sources/test_vtex_catalog.py`, `tests/sources/test_alkosto.py`, `tests/sources/test_ebay.py`, `tests/integration/test_crawl.py`, `tests/unit/test_render.py` + `tests/golden/alert_interest_cop.html`

**Interfaces:**
- Consumes: `match_interest`, `Interest` (Task 6), `Slot.interest` (Task 9).
- Produces: `CatalogObservation.title: str | None = None` (último campo); `observe(state, obs, now, topic, glob, *, floors: bool = True)`; `active_interests(conn: psycopg.Connection, cfg: InterestsConfig) -> tuple[Interest, ...]` en `app/interests.py` (semilla y luego personales por `id`); `CrawlDeps.interests: tuple[Interest, ...] = ()` y `CrawlDeps.interest_cap: int = 3` (de `cfg.daily_cards_per_interest`); la tarjeta (`card["interest"]`) y su línea «🎯 <nombre>» bajo el título.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_catalog.py
def test_observe_without_floors_accepts_a_cheap_real_drop() -> None:
    # «antes» 80.000 COP y precio 60.000 con 24 h estable: sin candidato con floors=True,
    # CATALOG_DROP con floors=False
# tests/sources/*: la observación trae el título de la fixture
# tests/integration/test_crawl.py
def test_interest_match_alerts_in_an_interest_topic(db) -> None:
    # "Silla Ergonómica" en "/hogar/" con bajada real → alerta con card["interest"] == "Silla de oficina"
def test_interest_match_skips_the_price_floor(db) -> None:
# tests/unit/test_render.py
def test_catalog_alert_with_interest_matches_golden() -> None:  # alert_interest_cop.html
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/unit/test_catalog.py tests/unit/test_render.py tests/sources -k "floors or title or interest"`
Expected: FAIL.

- [ ] **Step 3: Implement**: en `process`, `interest = match_interest(o.title or "", self.deps.interests)`; `observe(..., floors=interest is None)`; el candidato sigue si el modo es `card` o hay interés; `confirm` pasa `Slot(..., interest=name)`, `admit(..., per_interest=self.deps.interest_cap)` y `card["interest"] = name`. `render_catalog_alert` agrega `f"🎯 {esc(name)}"` después de la línea del título cuando `card.get("interest")`.

- [ ] **Step 4: Run them and see them pass**

Run: la verificación completa.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discountz tests
git commit -m "feat(intereses): el rastreo envía lo que coincide con un interés"
```

### Task 11: Intereses en los canales

**Files:**
- Modify: `src/discountz/app/channels.py` (`_Step.interests`, `_Step.interest_cap`, `_handle`), `src/discountz/app/render.py` (`render_channel_deal(post, topic, interest=None)`, `channel_card`), `src/discountz/app/cycle.py` (llena el paso con `active_interests`)
- Test: `tests/integration/test_channels.py`, `tests/unit/test_render.py` + `tests/golden/channel_deal_interest.html`

**Interfaces:**
- Consumes: `match_interest` (Task 6), `Slot.interest` (Task 9), `active_interests` (Task 10).
- Produces: `render_channel_deal(post: ChannelPost, topic: str, interest: str | None = None) -> RenderedMessage`.

- [ ] **Step 1: Write the failing tests**

```python
def test_interest_post_in_a_non_card_topic_is_sent(db) -> None:
    # "Escritorio elevable" (hogar) sale en tarjeta con «🎯 Escritorio»
def test_interest_post_skips_the_casillero_floor(db) -> None:
    # casillero a US$19 que coincide con un interés: se envía
def test_duplicate_interest_post_is_not_resent(db) -> None:
def test_channel_deal_with_interest_matches_golden() -> None:
```

- [ ] **Step 2: Run them and see them fail**

Run: `… uv run pytest -q -m integration tests/integration/test_channels.py -k interest`
Expected: FAIL.

- [ ] **Step 3: Implement**: `interest = match_interest(post.title, step.interests)` antes de los filtros de casillero y de perfil; ambos se saltan con interés; la detección de duplicados sigue igual; `admit` recibe `Slot(code, topic, COMMUNITY_DEAL, None, interest=name)` y `per_interest=step.interest_cap`.

- [ ] **Step 4: Run them and see them pass**

Run: la verificación completa.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discountz/app tests
git commit -m "feat(intereses): los canales envían lo que coincide con un interés"
```

### Task 12: Comando `/interes`, docs y estado

**Files:**
- Modify: `src/discountz/domain/commands.py` (`InterestList`, `InterestAdd`, `InterestDrop`, parser y ayuda), `src/discountz/domain/community.py` (`topic_for_title(title: str, rules: Sequence[ChannelRule]) -> str`), `src/discountz/app/inbox.py` (manejadores)
- Modify: `README.md` (sección «18. Intereses»), `CLAUDE.md`, `docs/estado.md`
- Test: `tests/unit/test_commands.py`, `tests/unit/test_community.py`, `tests/integration/test_inbox.py`

**Interfaces:**
- Consumes: `term_problem`, `interest_key` (Task 6), `InterestsConfig.seeds` (Task 7), `add_interest`, `list_interests`, `delete_interest` (Task 8).
- Produces: `InterestList()`, `InterestAdd(name: str, terms: tuple[str, ...], topic: str | None)`, `InterestDrop(number: int)` en la unión `Command`.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_interest_list() -> None:
    assert parse_command("/interes") == InterestList()
def test_parse_interest_add_with_synonyms_and_topic() -> None:
    assert parse_command("/interes rtx 5070, rtx5070 #tecnologia") == InterestAdd(
        "rtx 5070", ("rtx 5070", "rtx5070"), "tecnologia")
def test_parse_interest_drop() -> None:
    assert parse_command("/interes quitar 57") == InterestDrop(57)
def test_parse_interest_rejects_bad_terms() -> None:
    assert isinstance(parse_command("/interes ti"), CommandRejected)
    assert isinstance(parse_command("/interes " + ", ".join(f"t{i:03}" for i in range(11))), CommandRejected)
def test_topic_for_title_uses_channel_rules() -> None:  # «taladro percutor» → ferreteria
# tests/integration/test_inbox.py
def test_interest_add_list_and_drop(db) -> None:  # la lista agrupa por tema, numera la semilla y luego los personales
def test_interest_add_rejects_a_seed_name_with_other_case_or_accents(db) -> None:  # «multimetro» choca con «Multímetro»
def test_interest_add_rejects_the_101st(db) -> None:
def test_interest_drop_of_a_seed_is_rejected(db) -> None:
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/unit/test_commands.py tests/unit/test_community.py -k interes` y `… -m integration tests/integration/test_inbox.py -k interest`
Expected: FAIL.

- [ ] **Step 3: Implement**: `/interes` lista con números (semilla 1..S en orden del archivo; personales S+1.. por `id`) agrupada por el título del tema; agregar sin `#tema` usa `topic_for_title`; los rechazos dicen el motivo en español. Ayuda y menú: `("interes", "Intereses: /interes [frases, …] [#tema] · /interes quitar <n>")`. README: cómo agregar, listar y quitar, y que hay que correr *setup-bot* para ver el comando en el menú. `docs/estado.md` reescrito: siguiente paso = 9to5Toys (sub-proyecto aparte).

- [ ] **Step 4: Run them and see them pass**

Run: la verificación completa.
Expected: PASS.

- [ ] **Step 5: Commit y PR 2**

```bash
git add src/discountz tests README.md CLAUDE.md docs/estado.md
git commit -m "feat(intereses): comando /interes para agregar, listar y quitar"
```

Revisión final del PR 2, `git-guard.sh check`, push y PR. Al fusionar: *Actions → migrate* con `to = head` (esquema 12) y luego *setup-bot*.
