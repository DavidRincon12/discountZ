"""Temas por categoría y asignación de rutas de tienda (spec del rastreo §6)."""

from __future__ import annotations

import json

import pytest

from discountz.config import load_topics
from discountz.domain.topics import (
    CATEGORY_TOPIC,
    TOPIC_CODES,
    TopicRule,
    assign_topic,
    normalize_path,
)
from discountz.sources.fixtures import load_fixture

STORES = ("exito", "carulla", "olimpica", "panamericana")


def rule(source: str, prefix: str, topic: str) -> TopicRule:
    return TopicRule(source, normalize_path(prefix), topic)


def test_normalize_path_lowercases_strips_accents_and_ends_with_slash() -> None:
    assert normalize_path("/Tecnología/Electrodomésticos") == "/tecnologia/electrodomesticos/"
    assert normalize_path("  /Moda  y accesorios/ ") == "/moda y accesorios/"


def test_longest_prefix_wins() -> None:
    rules = [
        rule("*", "/tecnologia/", "tecnologia"),
        rule("*", "/tecnologia/electrodomesticos/", "electro"),
    ]
    assert assign_topic(rules, "exito", "/Tecnología/Electrodomésticos/Neveras/") == "electro"
    assert assign_topic(rules, "exito", "/Tecnología/Celulares/") == "tecnologia"


def test_source_rule_beats_wildcard_at_same_length() -> None:
    rules = [rule("*", "/hogar/", "hogar"), rule("panamericana", "/hogar/", "otros_hogar")]
    assert assign_topic(rules, "panamericana", "/Hogar/Decoración/") == "otros_hogar"
    assert assign_topic(rules, "exito", "/Hogar/Decoración/") == "hogar"


def test_unknown_path_goes_to_otros() -> None:
    rules = [rule("*", "/tecnologia/", "tecnologia")]
    assert assign_topic(rules, "exito", "/Servicios/") == "otros"


def test_category_topic_maps_v1_categories() -> None:
    assert dict(CATEGORY_TOPIC) == {
        "tecnologia": "tecnologia",
        "componentes": "tecnologia",
        "hogar": "hogar",
        "ropa": "moda",
        "otros": "otros",
    }


def test_topics_toml_matches_spec() -> None:
    topics = load_topics()
    assert tuple(topics.params) == TOPIC_CODES
    assert TOPIC_CODES == (
        "errores", "tecnologia", "electro", "hogar", "moda", "belleza", "mercado",
        "deportes", "bebes", "ferreteria", "mascotas", "libros", "comunidad_us", "otros",
    )  # fmt: skip
    p = topics.params
    assert (p["tecnologia"].t_bp, p["tecnologia"].g_bp) == (1200, 5000)
    assert (p["electro"].t_bp, p["electro"].g_bp) == (1500, 5000)
    assert (p["moda"].t_bp, p["moda"].g_bp) == (3000, 7500)
    assert (p["mercado"].t_bp, p["mercado"].g_bp) == (2500, 7000)
    assert (p["belleza"].t_bp, p["belleza"].g_bp) == (2500, 7000)
    assert (p["otros"].t_bp, p["otros"].g_bp) == (2000, 6000)
    assert (p["errores"].t_bp, p["errores"].g_bp) == (None, None)
    assert (p["comunidad_us"].t_bp, p["comunidad_us"].g_bp) == (None, None)
    v1 = {"tecnologia", "componentes", "hogar", "ropa", "otros"}
    assert {t.category for t in p.values()} <= v1
    assert all(r.topic in p for r in topics.rules)


@pytest.mark.parametrize("store", STORES)
def test_every_first_level_category_is_mapped(store: str) -> None:
    topics = load_topics()
    tree = json.loads(load_fixture("vtex", f"{store}-category-tree").body)
    unmapped = []
    for cat in tree:
        path = f"/{cat['name']}/"
        intentional = f"{store}:{normalize_path(path)}" in topics.intentional_otros
        if assign_topic(topics.rules, store, path) == "otros" and not intentional:
            unmapped.append(cat["name"])
    assert unmapped == []


# Categorías de primer nivel de Best Buy según su sitio y la documentación de la API; se revisan
# con el árbol real al grabar las fixtures con la clave de David.
BESTBUY_TOP = (
    "Appliances", "Audio", "Cameras, Camcorders & Drones", "Car Electronics & GPS",
    "Cell Phones", "Computers & Tablets", "Health, Fitness & Beauty",
    "Home, Furniture & Office", "Movies & Music", "Office Supplies & Ink", "Outdoor Living",
    "Smart Home, Security & Wi-Fi", "Toys, Games & Collectibles", "TV & Home Theater",
    "Video Games", "Wearable Technology",
)  # fmt: skip


@pytest.mark.parametrize("name", BESTBUY_TOP)
def test_bestbuy_first_level_categories_are_mapped(name: str) -> None:
    assert assign_topic(load_topics().rules, "bestbuy_us", f"/{name}/") != "otros"


def test_bestbuy_appliances_go_to_electro() -> None:
    rules = load_topics().rules
    assert assign_topic(rules, "bestbuy_us", "/Appliances/Washers & Dryers/") == "electro"
    assert assign_topic(rules, "bestbuy_us", "/Computers & Tablets/Laptops/") == "tecnologia"
