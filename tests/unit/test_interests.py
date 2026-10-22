"""Coincidencia de intereses (spec del perfil §3.2)."""

from __future__ import annotations

from discountz.domain.interests import compile_interest, interest_key, match_interest, term_problem


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
    other = compile_interest("Pantalla", "tecnologia", ["monitor"])
    assert match_interest("Monitor 27 pulgadas", [mon, other]) == mon


def test_numbers_and_punctuation_normalize_alike() -> None:
    gpu = compile_interest("RTX 5070", "tecnologia", ["rtx 5070"])
    ssd = compile_interest("SSD", "tecnologia", ["ssd 1tb"])
    assert match_interest("Tarjeta RTX-5070 12GB", [gpu]) == gpu
    assert match_interest("SSD 1 TB NVMe", [ssd]) == ssd


def test_empty_title_matches_nothing() -> None:
    assert match_interest("", [compile_interest("SSD", "tecnologia", ["ssd"])]) is None


def test_term_that_normalizes_to_nothing_matches_nothing() -> None:
    blank = compile_interest("Signos", "otros", ["¿?"])
    assert match_interest("Cualquier oferta", [blank]) is None
    cut = compile_interest("SSD", "tecnologia", ["ssd"], exclude=["¡!"])
    assert match_interest("SSD 1 TB", [cut]) == cut


def test_term_problem() -> None:
    assert term_problem("ti") is not None and term_problem("ti 84") is None
    assert term_problem("x" * 41) is not None and term_problem("¿?") is not None


def test_interest_key_ignores_case_and_accents() -> None:
    assert interest_key("Audífonos ") == interest_key("AUDIFONOS")
    assert interest_key("Audífonos") != interest_key("Audífono")
