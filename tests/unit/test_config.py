"""Configuración validada: variables de entorno, TOML de parámetros y canarios (Task 18)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from functools import partial
from pathlib import Path

import psycopg
import pytest

from discountz.__main__ import main
from discountz.config import (
    CANARIES_PATH,
    PARAMS_PATH,
    Settings,
    load_canaries,
    load_interests,
    load_params,
    load_settings,
    load_topics,
)
from discountz.domain.errors import ConfigError
from discountz.domain.interests import interest_key
from discountz.domain.money import Currency
from discountz.domain.params import DEFAULT_CATEGORIES, GlobalParams
from discountz.domain.topics import TOPIC_CODES

SECRET = "123456:SECRETO-de-prueba"  # noqa: S105 - falso, de prueba
DB_SECRET = "postgresql://u:CANARYPASS@localhost:5432/postgres"  # noqa: S105
ENV = {
    "DISCOUNTZ_DATABASE_URL": DB_SECRET,
    "DISCOUNTZ_TELEGRAM_BOT_TOKEN": SECRET,
    "DISCOUNTZ_TELEGRAM_CHAT_ID": "42",
    "DISCOUNTZ_TELEGRAM_OWNER_ID": "42",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def test_repo_toml_loads_with_spec_defaults() -> None:
    p = load_params(PARAMS_PATH)
    assert dict(p.categories) == dict(DEFAULT_CATEGORIES)
    assert p.glob == GlobalParams()
    assert set(p.budgets) == {
        "exito",
        "carulla",
        "olimpica",
        "panamericana",
        "jumbo",
        "pepeganga",
        "amazon_us",
        "newegg",
        "walmart_us",
        "bestbuy_us",
        "ebay_us",
        "alkosto",
        "slickdeals",
        "tg_descuentostech",
        "tg_clubgratis",
        "tg_cajitatech",
        "tg_descuentoshome",
        "tg_ofertu",
        "dealnews",
        "slickdeals_amazon",
        "woot",
    }
    assert p.budgets["tg_clubgratis"].max_per_run == 3  # hasta 3 páginas por lectura
    assert p.budgets["dealnews"].min_pause_s == 2  # su robots.txt pide `Crawl-delay: 2`
    # Mismo RSS que `slickdeals`, a su mismo ritmo, con presupuesto aparte.
    assert p.budgets["slickdeals_amazon"].group != p.budgets["slickdeals"].group
    assert p.budgets["slickdeals_amazon"].watch_interval == p.budgets["slickdeals"].watch_interval
    assert p.budgets["exito"].group == p.budgets["carulla"].group == "grupo_exito"
    assert p.budgets["amazon_us"].max_per_run == 12
    assert p.budgets["amazon_us"].max_per_24h == 400
    assert p.budgets["exito"].max_batch == 50
    # Woot (ADR 0015): 3 feeds cada 15 min a 1 petición por segundo, lejos de las 1.000 al día.
    woot = p.budgets["woot"]
    assert (woot.min_pause_s, woot.max_per_run, woot.max_per_24h) == (1, 3, 400)
    assert woot.watch_interval.total_seconds() == 15 * 60
    assert p.budgets["bestbuy_us"].max_batch == 100  # API: 100 SKU por consulta
    assert p.budgets["bestbuy_us"].min_pause_s == 1
    assert p.quiet_start == time(23, 0)
    assert p.quiet_end == time(7, 0)
    names = {e.name for e in p.events}
    assert {"Black Friday", "Cyber Monday", "Prime Day"} <= names
    for e in p.events:
        assert e.starts_at.utcoffset() is not None
        assert e.starts_at < e.ends_at
        assert e.sources <= set(p.budgets)


def test_event_times_are_bogota_converted_to_utc() -> None:
    bf = next(e for e in load_params(PARAMS_PATH).events if e.name == "Black Friday")
    assert bf.starts_at.astimezone(UTC).hour == 5  # 00:00 de Bogotá


def test_co_events_include_the_new_vtex_stores() -> None:
    events = load_params(PARAMS_PATH).events
    for name in ("Black Days", "Hot Sale"):
        event = next(e for e in events if e.name == name)
        assert {"jumbo", "pepeganga", "alkosto"} <= set(event.sources)


def test_hot_sale_october_2026_is_on_the_calendar() -> None:
    # CCCE: Drops del 16 al 18 y Hot Sale del 19 al 23 de octubre de 2026; Cyberlunes ya no existe.
    events = load_params(PARAMS_PATH).events
    names = {e.name for e in events}
    assert "Cyber Lunes" not in names
    middle = datetime(2026, 10, 21, 17, 0, tzinfo=UTC)
    assert any(e.name == "Hot Sale" and e.active("exito", middle) for e in events)


def _bad_toml(tmp_path: Path, old: str, new: str) -> Path:
    text = PARAMS_PATH.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "bad.toml"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


@pytest.mark.usefixtures("env")
def test_negative_threshold_fails_without_leaking(tmp_path: Path) -> None:
    path = _bad_toml(tmp_path, "t_good_bp = 1200", "t_good_bp = -1200")
    with pytest.raises(ConfigError) as info:
        load_params(path)
    msg = str(info.value)
    assert "t_good_bp" in msg
    assert "-1200" not in msg
    assert SECRET not in msg and "CANARYPASS" not in msg


def test_quality_floors_come_from_the_toml(tmp_path: Path) -> None:
    path = _bad_toml(tmp_path, "min_price_cop = 100000", "min_price_cop = 150000")
    glob = load_params(path).glob
    assert glob.min_price[Currency.COP] == 150_000
    assert glob.min_price[Currency.USD] == 2_500
    assert glob.catalog_min_saving == {Currency.COP: 30_000, Currency.USD: 500}


def test_daily_cards_come_from_the_toml(tmp_path: Path) -> None:
    assert load_params().daily_cards == 8
    path = _bad_toml(tmp_path, "daily_per_source = 8", "daily_per_source = 5")
    assert load_params(path).daily_cards == 5


def test_unknown_category_fails(tmp_path: Path) -> None:
    path = _bad_toml(tmp_path, "[categories.ropa]", "[categories.juguetes]")
    with pytest.raises(ConfigError, match="categor"):
        load_params(path)


def test_event_with_unknown_source_fails(tmp_path: Path) -> None:
    path = _bad_toml(tmp_path, 'sources = ["amazon_us"]', 'sources = ["tienda_x"]')
    with pytest.raises(ConfigError, match="fuente"):
        load_params(path)


def test_settings_load_and_hide_secrets(env: None) -> None:
    s = load_settings()
    assert isinstance(s, Settings)
    assert s.telegram_owner_id == 42
    assert s.soft_deadline_s == 480
    assert s.healthchecks_url is None
    assert SECRET not in repr(s) and "CANARYPASS" not in repr(s)


def test_channel_id_is_optional(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_settings().telegram_channel_id is None
    monkeypatch.setenv("DISCOUNTZ_TELEGRAM_CHANNEL_ID", " ")  # secreto sin definir en Actions
    assert load_settings().telegram_channel_id is None
    monkeypatch.setenv("DISCOUNTZ_TELEGRAM_CHANNEL_ID", "-1001234")
    assert load_settings().telegram_channel_id == -1001234


def test_bad_setting_message_has_no_values(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCOUNTZ_TELEGRAM_CHAT_ID", "no-es-numero")
    with pytest.raises(ConfigError) as info:
        load_settings()
    msg = str(info.value)
    assert "telegram_chat_id" in msg
    assert "no-es-numero" not in msg and SECRET not in msg and "CANARYPASS" not in msg


def test_missing_setting_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError, match="database_url"):
        load_settings()


def test_canaries_cover_vtex_stores() -> None:
    canaries = load_canaries(CANARIES_PATH)
    assert {"exito", "carulla", "olimpica", "panamericana", "jumbo", "pepeganga"} <= set(canaries)
    for items in canaries.values():
        assert 1 <= len(items) <= 2
        for c in items:
            assert 0 < c.min_minor < c.max_minor


def test_check_config_exits_zero(env: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config"]) == 0
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert "CANARYPASS" not in out.out + out.err


def test_check_config_fails_with_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    assert main(["check-config"]) == 2
    assert "database_url" in capsys.readouterr().err


def test_crawl_budgets_match_spec() -> None:
    c = load_params(PARAMS_PATH).crawl
    assert set(c) == {
        "exito",
        "carulla",
        "olimpica",
        "panamericana",
        "bestbuy_us",
        "ebay_us",
        "alkosto",
        "jumbo",
        "pepeganga",
    }
    alkosto = c["alkosto"]
    assert (alkosto.min_pause_s, alkosto.jitter_s) == (2, 1)
    assert (alkosto.max_per_run, alkosto.max_per_24h, alkosto.hot_share_bp) == (60, 1000, 2500)
    assert (c["exito"].min_pause_s, c["exito"].jitter_s, c["exito"].max_per_24h) == (1, 1, 24_000)
    assert (c["carulla"].min_pause_s, c["carulla"].max_per_24h) == (1, 12_000)
    assert (c["olimpica"].min_pause_s, c["olimpica"].max_per_24h) == (2, 6_000)
    assert (c["panamericana"].min_pause_s, c["panamericana"].max_per_24h) == (2, 6_000)
    for code in ("jumbo", "pepeganga"):
        assert (c[code].min_pause_s, c[code].max_per_run, c[code].max_per_24h) == (2, 250, 6_000)
    assert (c["bestbuy_us"].min_pause_s, c["bestbuy_us"].max_per_24h) == (0.25, 40_000)
    ebay = c.pop("ebay_us") if isinstance(c, dict) else c["ebay_us"]
    assert (ebay.max_per_run, ebay.max_per_24h, ebay.hot_share_bp) == (170, 4000, 3000)
    assert all(b.hot_share_bp == 2500 for code, b in c.items() if code != "ebay_us")
    assert all(b.max_per_run <= b.max_per_24h for b in c.values())


def test_crawl_section_must_name_a_known_source(tmp_path: Path) -> None:
    path = _bad_toml(tmp_path, "[crawl.exito]", "[crawl.desconocida]")
    with pytest.raises(ConfigError):
        load_params(path)


def test_channel_rules_point_to_existing_topics() -> None:
    from discountz.config import load_topics

    topics = load_topics()
    assert topics.channel_rules
    assert all(rule.topic in topics.params for rule in topics.channel_rules)
    tech = next(r for r in topics.channel_rules if r.topic == "tecnologia")
    assert {"reloj inteligente", "audifono"} <= tech.words
    moda = next(r for r in topics.channel_rules if r.topic == "moda")
    assert "banador" in moda.words


def test_telegram_channels_are_read_every_three_minutes() -> None:
    """El ciclo corre cada ~2 min: con 3 min de espera, cada canal se lee en ciclos alternos."""
    from datetime import timedelta

    p = load_params(PARAMS_PATH)
    assert p.channel_interval == timedelta(minutes=3)
    reads_per_day = timedelta(days=1) // p.channel_interval
    channels = [b for code, b in p.budgets.items() if code.startswith("tg_")]
    assert len(channels) == 5
    for b in channels:
        assert b.watch_interval <= p.channel_interval  # el presupuesto no alarga la espera
        assert b.max_per_run * reads_per_day <= b.max_per_24h  # el tope diario no corta lecturas


def test_params_ebay_defaults() -> None:
    p = load_params(PARAMS_PATH)
    e = p.ebay
    assert e.ship_to_zip == "33166"
    assert (e.items_per_partition, e.max_items) == (75, 150_000)
    assert (e.min_feedback_pct, e.min_feedback_score) == (98, 100)
    assert e.excluded_roots == frozenset(
        {1, 99, 237, 260, 316, 550, 870, 1305, 3252, 10542, 11116, 20081, 45100, 64482, 172008}
    )
    b = p.budgets["ebay_us"]
    # `getItems` sin acceso: una ficha por petición; barrido + ciclo caben en 5.000 al día.
    assert (b.max_batch, b.max_per_run, b.max_per_24h) == (1, 10, 1000)
    assert b.max_per_24h + p.crawl["ebay_us"].max_per_24h <= 5000


def test_channel_term_in_two_topics_is_rejected(tmp_path: Path) -> None:
    from discountz.config import load_topics

    src = Path("config/topics.toml").read_text(encoding="utf-8")
    path = tmp_path / "topics.toml"
    path.write_text(src + '\n[[channel_rules]]\ntopic = "moda"\nwords = ["Audífonos"]\n', "utf-8")
    with pytest.raises(ConfigError, match="audifonos"):
        load_topics(path)


def test_auto_watch_caps_stay_with_their_source() -> None:
    b = load_params(PARAMS_PATH).budgets
    assert b["ebay_us"].max_auto_watches == 40
    assert b["alkosto"].max_auto_watches == 0  # sin leads que lleven a Alkosto


@pytest.mark.parametrize("code", ["amazon_us", "newegg", "ebay_us"])
def test_auto_watches_leave_half_the_daily_budget_for_the_user(code: str) -> None:
    """Los automáticos compiten con /seguir y /buscar: si pasan del tope, esos se atrasan."""
    b = load_params(PARAMS_PATH).budgets[code]
    reads_per_day = b.max_auto_watches * (timedelta(days=1) // b.watch_interval) // b.max_batch
    assert reads_per_day <= b.max_per_24h // 2


def test_setup_bot_sets_the_menu_for_the_group(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakeTelegram:
        def __init__(self, token: object, http: object) -> None:
            pass

        def delete_webhook(self) -> None:
            calls.append(("deleteWebhook", None))

        def set_my_commands(self, chat_id: int, commands: object) -> None:
            calls.append(("setMyCommands", chat_id))

    monkeypatch.setattr("discountz.__main__.TelegramClient", FakeTelegram)
    assert main(["setup-bot"]) == 0
    assert calls == [("deleteWebhook", None), ("setMyCommands", 42)]


def test_setup_bot_without_settings_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    assert main(["setup-bot"]) == 2


def test_crawl_start_waits_out_a_pooler_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A las 13:00Z el pooler rechazó el arranque de Éxito y eBay: se espera y se reintenta."""
    import psycopg

    import discountz.__main__ as cli

    calls: list[int] = []
    sleeps: list[float] = []
    conn = object()

    def flaky(_settings: object, _rnd: object) -> object:
        calls.append(1)
        if len(calls) < 3:
            raise psycopg.OperationalError("rechazo")
        return conn

    monkeypatch.setattr(cli, "_connect_with_retry", flaky)
    monkeypatch.setattr("discountz.__main__.time.sleep", sleeps.append)
    assert cli._connect_crawl(None, None, "exito") is conn  # type: ignore[arg-type]
    assert sleeps == [30.0, 30.0]


def test_crawl_start_gives_up_after_the_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    import discountz.__main__ as cli

    def down(_settings: object, _rnd: object) -> object:
        raise psycopg.OperationalError("caída")

    sleeps: list[float] = []
    monkeypatch.setattr(cli, "_connect_with_retry", down)
    monkeypatch.setattr("discountz.__main__.time.sleep", sleeps.append)
    with pytest.raises(psycopg.OperationalError):
        cli._connect_crawl(None, None, "exito")  # type: ignore[arg-type]
    assert len(sleeps) == 2


def test_load_topics_reads_profile_modes() -> None:
    """Fija el perfil de `config/topics.toml` (README §17): quien cambie un `mode`, cambia esto."""
    modes = {code: p.mode for code, p in load_topics().params.items()}
    off = {"belleza", "mercado", "bebes", "mascotas"}
    interest = {"hogar", "deportes", "otros"}
    assert {c for c, m in modes.items() if m == "off"} == off
    assert {c for c, m in modes.items() if m == "interest"} == interest
    assert {c for c, m in modes.items() if m == "card"} == set(modes) - off - interest


def _topics_with_unknown_mode(folder: Path) -> Path:
    """Copia de `config/topics.toml` con su primer `mode` cambiado por uno que no existe."""
    src = Path("config/topics.toml").read_text(encoding="utf-8")
    bad, n = re.subn(r'^mode = "[a-z]+"$', 'mode = "quiza"', src, count=1, flags=re.MULTILINE)
    assert n == 1
    path = folder / "topics.toml"
    path.write_text(bad, "utf-8")
    return path


def test_load_topics_rejects_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\.mode"):
        load_topics(_topics_with_unknown_mode(tmp_path))


def test_check_config_fails_on_unknown_topic_mode(
    env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Un `mode` desconocido es un error de configuración (spec del perfil §2)."""
    bad = _topics_with_unknown_mode(tmp_path)
    monkeypatch.setattr("discountz.__main__.load_topics", partial(load_topics, bad))
    assert main(["check-config"]) == 2
    assert ".mode" in capsys.readouterr().err


def test_seed_interests_load() -> None:
    """La semilla carga y es coherente; no fija cuántos ni cuáles: David la edita."""
    cfg = load_interests()
    assert cfg.daily_cards_per_interest >= 1
    assert cfg.seeds
    assert len({interest_key(i.name) for i in cfg.seeds}) == len(cfg.seeds)
    assert {i.topic for i in cfg.seeds} <= set(TOPIC_CODES) - {"errores", "comunidad_us"}
    assert all(i.personal_id is None for i in cfg.seeds)


def _interests_file(folder: Path, second: str) -> Path:
    """Un `interests.toml` mínimo: un interés válido y `second` como el segundo."""
    path = folder / "interests.toml"
    path.write_text(
        "daily_cards_per_interest = 3\n\n"
        '[[interests]]\nname = "Multímetro"\ntopic = "ferreteria"\nterms = ["multimetro"]\n\n'
        f"[[interests]]\n{second}\n",
        "utf-8",
    )
    return path


def test_load_interests_reads_a_minimal_file(tmp_path: Path) -> None:
    second = (
        'name = "Taladro"\ntopic = "ferreteria"\nterms = ["Taladros", "drill"]\nexclude = ["3d"]'
    )
    seeds = load_interests(_interests_file(tmp_path, second)).seeds
    assert [i.name for i in seeds] == ["Multímetro", "Taladro"]
    assert seeds[1].terms == (("taladro",), ("drill",))
    assert seeds[1].exclude == (("3d",),)  # en `exclude` vale una palabra corta


BAD_INTERESTS = {
    "unknown_topic": ('name = "Taladro"\ntopic = "jardin"\nterms = ["taladro"]', "tema"),
    "reserved_topic": ('name = "Taladro"\ntopic = "errores"\nterms = ["taladro"]', "tema"),
    "no_terms": ('name = "Taladro"\ntopic = "ferreteria"\nterms = []', r"\.terms"),
    "many_terms": (
        f'name = "Taladro"\ntopic = "ferreteria"\nterms = {[f"taladro {n}" for n in range(11)]}',
        r"\.terms",
    ),
    "repeated_name": ('name = "MULTIMETRO"\ntopic = "ferreteria"\nterms = ["tester"]', "repetido"),
    "long_term": (f'name = "Taladro"\ntopic = "ferreteria"\nterms = ["{"x" * 41}"]', "40"),
    "short_term": ('name = "Taladro"\ntopic = "ferreteria"\nterms = ["ti"]', "3 letras"),
}


@pytest.mark.parametrize("problem", sorted(BAD_INTERESTS))
def test_load_interests_rejects(problem: str, tmp_path: Path) -> None:
    second, reason = BAD_INTERESTS[problem]
    with pytest.raises(ConfigError, match=reason):
        load_interests(_interests_file(tmp_path, second))


def test_check_config_fails_on_bad_interests(
    env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Un interés mal escrito en la semilla es un error de configuración (spec del perfil §5)."""
    bad = _interests_file(tmp_path, BAD_INTERESTS["short_term"][0])
    monkeypatch.setattr("discountz.__main__.load_interests", partial(load_interests, bad))
    assert main(["check-config"]) == 2
    assert "3 letras" in capsys.readouterr().err


def test_run_fails_on_bad_interests_before_connecting(
    env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """El ciclo lee los intereses con el resto de la configuración: falla antes de tocar la BD."""
    bad = _interests_file(tmp_path, BAD_INTERESTS["short_term"][0])
    monkeypatch.setattr("discountz.__main__.load_interests", partial(load_interests, bad))

    def no_db(*_: object) -> None:
        raise psycopg.OperationalError("sin BD")

    monkeypatch.setattr("discountz.__main__._connect_with_retry", no_db)
    assert main(["run"]) == 2
    assert "3 letras" in capsys.readouterr().err
