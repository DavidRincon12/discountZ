from datetime import date

import pytest

from discountz.domain.commands import (
    Category,
    CommandRejected,
    Drop,
    Follow,
    Help,
    ListCmd,
    Search,
    Silence,
    SourceToggle,
    Status,
    Target,
    TokenExpiry,
    Topics,
    parse_command,
)

URL = "https://www.exito.com/tv-55/p"


def test_follow_one_url_with_target_and_category() -> None:
    cmd = parse_command(f"/seguir {URL} 120 #componentes")
    assert cmd == Follow((URL,), "120", "componentes")


def test_follow_accepts_20_urls_rejects_21() -> None:
    urls = [f"https://www.exito.com/p{i}/p" for i in range(21)]
    cmd = parse_command("/seguir\n" + "\n".join(urls[:20]))
    assert cmd == Follow(tuple(urls[:20]), None, None)
    assert isinstance(parse_command("/seguir\n" + "\n".join(urls)), CommandRejected)


def test_temas_and_drop_auto() -> None:
    assert parse_command("/temas") == Topics()
    assert isinstance(parse_command("/temas ya"), CommandRejected)
    assert parse_command("/dejar a12") == Drop("a12")
    assert parse_command("/dejar auto") == Drop("auto")


def test_follow_without_url_is_rejected() -> None:
    assert isinstance(parse_command("/seguir 120"), CommandRejected)


def test_follow_unknown_category_is_rejected() -> None:
    assert isinstance(parse_command(f"/seguir {URL} #juguetes"), CommandRejected)


def test_search_full_syntax() -> None:
    cmd = parse_command("/buscar exito ssd 1tb -carcasa -case max:400.000 #componentes")
    assert cmd == Search("exito", "ssd 1tb", ("carcasa", "case"), None, "400.000", "componentes")


def test_search_defaults_to_otros_and_normalizes_query() -> None:
    cmd = parse_command("/buscar slickdeals  990   EVO  min:50")
    assert cmd == Search("slickdeals", "990 evo", (), "50", None, "otros")


def test_search_requires_query() -> None:
    assert isinstance(parse_command("/buscar exito"), CommandRejected)
    assert isinstance(parse_command("/buscar exito -caso"), CommandRejected)


def test_list_drop_and_category() -> None:
    assert parse_command("/lista #componentes") == ListCmd("#componentes")
    assert parse_command("/lista") == ListCmd(None)
    assert parse_command("/dejar p12") == Drop("p12")
    assert parse_command("/dejar b3") == Drop("b3")
    assert isinstance(parse_command("/dejar x1"), CommandRejected)
    assert parse_command("/categoria L1234 tecnologia") == Category("L1234", "tecnologia")
    assert isinstance(parse_command("/categoria p1 juguetes"), CommandRejected)


def test_target_variants() -> None:
    assert parse_command("/objetivo p12 280000") == Target("p12", "280000")
    assert parse_command("/objetivo p9 99,99") == Target("p9", "99,99")
    assert parse_command("/objetivo p12 no") == Target("p12", None)


def test_target_in_cop_for_usd_is_rejected() -> None:
    assert isinstance(parse_command("/objetivo p9 450.000cop"), CommandRejected)


def test_silence_status_help_source_and_token() -> None:
    assert parse_command("/silenciar todo 1") == Silence("todo", 1)
    assert parse_command("/silenciar p12 30") == Silence("p12", 30)
    assert isinstance(parse_command("/silenciar p12 91"), CommandRejected)
    assert parse_command("/estado") == Status()
    assert parse_command("/ayuda") == Help()
    assert parse_command("/fuente off amazon_us") == SourceToggle("amazon_us", False)
    assert parse_command("/fuente on walmart_us") == SourceToggle("walmart_us", True)
    assert isinstance(parse_command("/fuente talvez exito"), CommandRejected)
    assert parse_command("/token_vence 2027-10-01") == TokenExpiry(date(2027, 10, 1))
    assert isinstance(parse_command("/token_vence mañana"), CommandRejected)


def test_bot_suffix_is_ignored() -> None:
    assert parse_command("/estado@discountz_bot") == Status()


@pytest.mark.parametrize("text", ["/peso 4200", "/hola", "hola", "", "/seguir@"])
def test_unknown_or_removed_commands_are_rejected(text: str) -> None:
    assert isinstance(parse_command(text), CommandRejected)
