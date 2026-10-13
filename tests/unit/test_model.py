from discountz.domain.model import AlertKind, Flag, SearchSpec, normalize_title
from discountz.domain.money import Currency, Money
from tests.factories import offer


def test_normalize_title_lowercases_strips_accents_and_joins_units() -> None:
    normalized = normalize_title("Televisor LED 55 Pulgadas Ñandú 1 TB")
    assert normalized == "televisor led 55pulgadas nandu 1tb"


def test_search_spec_accepts_normalized_tokens_and_excludes() -> None:
    spec = SearchSpec("exito", "ssd 1 TB", ("carcasa",), None, 40_000_000, "componentes")
    assert spec.accepts(offer(title="Disco SSD Kingston 1TB NVMe", price=30_000_000))
    assert not spec.accepts(offer(title="Carcasa SSD 1TB", price=100_000))
    assert not spec.accepts(offer(title="SSD 1TB", price=50_000_000))


def test_search_spec_min_price_and_missing_price() -> None:
    spec = SearchSpec("exito", "ssd", (), 200_000, None, "componentes")
    assert not spec.accepts(offer(title="SSD", price=100_000))
    assert not spec.accepts(offer(title="SSD", price=None))
    unbounded = SearchSpec("exito", "ssd", (), None, None, "otros")
    assert unbounded.accepts(offer(title="SSD", price=None))


def test_search_spec_requires_every_token() -> None:
    spec = SearchSpec("exito", "ssd 2tb", (), None, None, "componentes")
    assert not spec.accepts(offer(title="Disco SSD 1TB"))


def test_alert_kind_rank_order() -> None:
    ranks = [k.rank for k in AlertKind]
    assert AlertKind.PRICE_ERROR.rank == 5
    assert AlertKind.COMMUNITY_DEAL.rank == 1
    assert sorted(ranks, reverse=True) == ranks


def test_flags_cover_db_check_values() -> None:
    expected = {
        "inflated_list",
        "advertised_gap",
        "pre_inflated",
        "flash_seen",
        "gap_before",
        "short_history",
        "marketplace",
        "target_above_typical",
        "ref_antigua",
        "unit_changed",
        "storm",
        "target_also",
        "persistence_only",
        "hot",
        "community",
        "unverified",
    }
    assert {f.value for f in Flag} == expected


def test_search_spec_matches_title_and_price() -> None:
    spec = SearchSpec("slickdeals", "990 evo", ("refurbished",), None, 10_000, "componentes")
    assert spec.matches("Samsung 990 EVO 1TB SSD $74.99", Money(7_499, Currency.USD))
    assert not spec.matches("Samsung 990 EVO 1TB Refurbished $59.99", Money(5_999, Currency.USD))
    assert not spec.matches("Samsung 990 EVO 2TB $129.99", Money(12_999, Currency.USD))
    assert not spec.matches("Samsung 990 EVO 1TB", None)  # con rango y sin precio: no
