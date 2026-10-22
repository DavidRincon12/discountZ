"""Temas del grupo de Telegram y asignación de rutas de categoría (spec del rastreo §6).

Puro: las reglas vienen de `config/topics.toml` y se aplican por prefijo de ruta normalizada.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

TOPIC_CODES: tuple[str, ...] = (
    "errores",
    "tecnologia",
    "electro",
    "hogar",
    "moda",
    "belleza",
    "mercado",
    "deportes",
    "bebes",
    "ferreteria",
    "mascotas",
    "libros",
    "comunidad_us",
    "otros",
)
DEFAULT_TOPIC = "otros"
GROCERY_TOPIC = "mercado"  # ni el rastreo ni lo que llega por casillero lo alertan
ANY_SOURCE = "*"

# Perfil de David (spec del perfil §1.1): `card` sale en tarjeta; `interest` se rastrea, pero solo
# sale si coincide con un interés; `off` no se rastrea y solo sale si coincide con un interés.
TopicMode = Literal["card", "interest", "off"]

# Categorías de la v1 (seguimientos y búsquedas) → tema donde caen sus alertas (spec §5.1).
CATEGORY_TOPIC: Mapping[str, str] = MappingProxyType(
    {
        "tecnologia": "tecnologia",
        "componentes": "tecnologia",
        "hogar": "hogar",
        "ropa": "moda",
        "otros": "otros",
    }
)


@dataclass(frozen=True, slots=True)
class TopicParams:
    code: str
    title: str
    t_bp: int | None  # umbral de bajada normal; None (errores, mercado) = el rastreo no alerta
    g_bp: int | None  # umbral de posible error de precio
    category: str  # categoría de la v1 que reciben los listings de este tema
    mode: TopicMode = "card"


@dataclass(frozen=True, slots=True)
class TopicRule:
    source: str  # código de fuente o "*"
    prefix: str  # ruta normalizada con `normalize_path`
    topic: str


def normalize_path(path: str) -> str:
    """Minúsculas, sin tildes, espacios simples y con `/` al inicio y al final."""
    decomposed = unicodedata.normalize("NFKD", path.strip().lower())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    parts = [" ".join(p.split()) for p in plain.split("/")]
    inner = "/".join(p for p in parts if p)
    return f"/{inner}/" if inner else "/"


def assign_topic(rules: Sequence[TopicRule], source: str, category_path: str) -> str:
    """Gana el prefijo más largo; a igual largo, la regla de la fuente antes que la comodín."""
    path = normalize_path(category_path)
    best: tuple[int, int, str] | None = None
    for r in rules:
        if r.source not in (source, ANY_SOURCE) or not path.startswith(r.prefix):
            continue
        key = (len(r.prefix), 1 if r.source == source else 0, r.topic)
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best is not None else DEFAULT_TOPIC


def crawl_wanted(
    rules: Sequence[TopicRule], params: Mapping[str, TopicParams], source: str, path: str
) -> bool:
    """¿Se recorre esta partición del catálogo? (spec del perfil §1.3).

    Sí si no es una categoría (`/`), si su tema no está `off` o si una regla más profunda la
    lleva a un tema rastreado (`panamericana:/hogar/ferreteria/` mantiene `/hogar/`).
    """
    route = normalize_path(path)
    if route == "/":
        return True
    if params[assign_topic(rules, source, route)].mode != "off":
        return True
    return any(
        r.source in (source, ANY_SOURCE)
        and len(r.prefix) > len(route)
        and r.prefix.startswith(route)
        and params[r.topic].mode != "off"
        for r in rules
    )
