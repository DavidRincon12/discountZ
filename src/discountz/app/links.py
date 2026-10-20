"""Enlace real de las ofertas de canales: el destino de un acortador, sin el referido del canal.

Clubgratis y OFERTU publican `joylink.io/…`, que redirige por HTTP (30x) a la tienda con el
código de afiliado del canal. Se lee solo la cabecera `Location` del acortador: la tienda nunca
se descarga. Si algo falla, la oferta sale con el enlace del canal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import urljoin

from discountz.app.deliver import REDIRECTS
from discountz.domain.errors import DiscountzError, UnsafeUrl
from discountz.domain.urls import SafeUrl, amazon_product_url
from discountz.infra.http import HttpClient
from discountz.infra.log import event

log = logging.getLogger("discountz")

REDIRECTORS = frozenset({"joylink.io"})
MAX_HOPS = 3

LinkResolver = Callable[[SafeUrl], SafeUrl]


def make_link_resolver(http: HttpClient, allowed: frozenset[str]) -> LinkResolver:
    """Cambia un enlace de `REDIRECTORS` por su destino si es un host de `allowed`.

    Una ficha de Amazon queda como `/dp/<ASIN>` sin parámetros; otra tienda, sin los parámetros
    de rastreo de `SafeUrl` (`tag`, `ref`, `utm_*`…). `http` no sigue redirecciones.
    """

    def unresolved(link: SafeUrl, code: str, status: int | None = None) -> SafeUrl:
        event(log, "link_unresolved", code=code, http_status=status)
        return link

    def resolve(link: SafeUrl) -> SafeUrl:
        if link.host not in REDIRECTORS:
            return link
        url = str(link)
        for _ in range(MAX_HOPS):
            try:
                resp = http.request("GET", url)
            except DiscountzError:
                return unresolved(link, "network")
            if resp.status not in REDIRECTS:
                return unresolved(link, "status", resp.status)
            location = {k.lower(): v for k, v in resp.headers.items()}.get("location", "")
            try:
                target = SafeUrl.parse(urljoin(url, location), allowed | REDIRECTORS)
            except UnsafeUrl:
                return unresolved(link, "redirect", resp.status)
            if target.host not in REDIRECTORS:
                return amazon_product_url(target) or target
            url = str(target)
        return unresolved(link, "redirect_loop")

    return resolve
