"""Intereses de David: qué título coincide con cuál (spec del perfil §3.2).

Puro: la semilla (`config/interests.toml`) y los personales (`/interes`) llegan ya leídos.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from discountz.domain.community import stem
from discountz.domain.model import normalize_title
from discountz.domain.topics import TOPIC_CODES

# `errores` y `comunidad_us` no clasifican productos: un interés no se agrupa ahí.
INTEREST_TOPICS = tuple(c for c in TOPIC_CODES if c not in ("errores", "comunidad_us"))
MAX_TERMS = 10
MAX_TERM_CHARS = 40
MIN_WORD_LETTERS = 3  # un término de una sola palabra más corta coincidiría con cualquier cosa
MAX_PERSONAL = 100  # intereses de /interes, además de la semilla

Words = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Interest:
    name: str
    topic: str
    terms: tuple[Words, ...]  # normalizados y en singular
    exclude: tuple[Words, ...]
    personal_id: int | None = None  # fila de `interest`; None = semilla


def _words(text: str) -> Words:
    return tuple(stem(w) for w in normalize_title(text).split())


def term_problem(term: str) -> str | None:
    """Por qué el término no vale como término de un interés, o None si vale."""
    if len(term.strip()) > MAX_TERM_CHARS:
        return f"tiene más de {MAX_TERM_CHARS} caracteres"
    words = normalize_title(term).split()
    if not words:
        return "no tiene letras ni números"
    if len(words) == 1 and len(words[0]) < MIN_WORD_LETTERS:
        return f"una sola palabra necesita al menos {MIN_WORD_LETTERS} letras"
    return None


def compile_interest(
    name: str,
    topic: str,
    terms: Sequence[str],
    exclude: Sequence[str] = (),
    personal_id: int | None = None,
) -> Interest:
    """Normaliza los términos; uno que queda vacío se descarta (si no, coincidiría con todo)."""
    return Interest(
        name=name,
        topic=topic,
        terms=tuple(w for w in map(_words, terms) if w),
        exclude=tuple(w for w in map(_words, exclude) if w),
        personal_id=personal_id,
    )


def interest_key(name: str) -> str:
    """El nombre sin mayúsculas, tildes ni signos, para no repetir intereses."""
    return normalize_title(name)


def _contains(words: Words, term: Words) -> bool:
    n = len(term)
    return any(words[i : i + n] == term for i in range(len(words) - n + 1))


def match_interest(title: str, interests: Sequence[Interest]) -> Interest | None:
    """El interés del término con más palabras presente en el título; a igual largo, el primero."""
    words = _words(title)
    best: Interest | None = None
    best_len = 0
    for interest in interests:
        if any(_contains(words, t) for t in interest.exclude):
            continue
        found = max((len(t) for t in interest.terms if _contains(words, t)), default=0)
        if found > best_len:
            best, best_len = interest, found
    return best
