"""Registro de fuentes (Strategy con registro) y ruteo de URLs de David."""

from __future__ import annotations

import importlib
from collections.abc import Mapping

from discountz.domain.errors import UnsafeUrl
from discountz.domain.model import ListingRef
from discountz.domain.urls import SafeUrl
from discountz.sources.base import NeedsResolution, Source

# Cada adaptador se registra en su propio módulo al importarse (ver `load_all`).
SOURCES: dict[str, Source] = {}


# Módulos de adaptadores que se registran al importarse (crece con cada tienda).
ADAPTER_MODULES: tuple[str, ...] = (
    "discountz.sources.vtex",
    "discountz.sources.amazon",
    "discountz.sources.newegg",
    "discountz.sources.slickdeals",
    "discountz.sources.walmart",
    "discountz.sources.bestbuy",
)


def load_all() -> Mapping[str, Source]:
    """Importa todos los adaptadores para que se registren y devuelve el registro."""
    for module in ADAPTER_MODULES:
        importlib.import_module(module)
    return SOURCES


def register(source: Source) -> Source:
    SOURCES[source.descriptor.code] = source
    return source


def route_url(
    raw: str, sources: Mapping[str, Source] | None = None
) -> tuple[Source, ListingRef | NeedsResolution]:
    """Valida la URL contra la lista blanca exacta y la entrega al adaptador del host."""
    table = SOURCES if sources is None else sources
    hosts = frozenset(h for s in table.values() for h in s.descriptor.hosts)
    url = SafeUrl.parse(raw, hosts)
    for source in table.values():
        if url.host in source.descriptor.hosts:
            ref = source.parse_url(url)
            if ref is None:
                raise UnsafeUrl("unsupported_path")
            return source, ref
    raise UnsafeUrl("host_not_allowed")
