"""Perfiles de temas para las pruebas: no dependen de los modos que David edite en el TOML.

Solo `test_load_topics_reads_profile_modes` fija los modos de `config/topics.toml`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from discountz.config import Topics, load_topics
from discountz.domain.topics import TOPIC_CODES, TopicMode

# Perfil del spec (§1.1); los temas que no aparecen (errores, comunidad_us) van en tarjeta.
SPEC_MODES: Mapping[str, TopicMode] = MappingProxyType(
    {
        "tecnologia": "card",
        "electro": "card",
        "ferreteria": "card",
        "moda": "card",
        "libros": "card",
        "hogar": "interest",
        "deportes": "interest",
        "otros": "interest",
        "belleza": "off",
        "mercado": "off",
        "bebes": "off",
        "mascotas": "off",
    }
)
SPEC_CARD = frozenset(c for c in TOPIC_CODES if SPEC_MODES.get(c, "card") == "card")


def with_modes(topics: Topics, modes: Mapping[str, TopicMode]) -> Topics:
    """`topics` con cada tema en el modo de `modes`; el que no aparece va en tarjeta."""
    params = {c: replace(p, mode=modes.get(c, "card")) for c, p in topics.params.items()}
    return replace(topics, params=MappingProxyType(params))


def spec_profile() -> Topics:
    return with_modes(load_topics(), SPEC_MODES)


def all_cards() -> Topics:
    return with_modes(load_topics(), {})
