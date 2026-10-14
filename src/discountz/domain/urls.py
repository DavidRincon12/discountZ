"""`SafeUrl`: URLs de tiendas validadas contra una lista blanca exacta de hosts (spec §4.3)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

from discountz.domain.errors import UnsafeUrl

_TRACKING_KEYS = frozenset({"ref", "tag", "psc", "th", "smid", "_encoding"})


def _is_tracking(key: str) -> bool:
    return key in _TRACKING_KEYS or key.startswith("utm_")


@dataclass(frozen=True, slots=True)
class SafeUrl:
    host: str
    path: str
    query: tuple[tuple[str, str], ...]

    @classmethod
    def parse(cls, raw: str, allowed_hosts: frozenset[str]) -> SafeUrl:
        """Valida `raw` y devuelve la URL sin parámetros de rastreo; lanza `UnsafeUrl`."""
        if not raw or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in raw):
            raise UnsafeUrl("bad_chars")
        try:
            parts = urlsplit(raw)
            port = parts.port
        except ValueError as exc:
            raise UnsafeUrl("bad_url") from exc
        if parts.scheme != "https":
            raise UnsafeUrl("not_https")
        if "@" in parts.netloc or parts.username is not None:
            raise UnsafeUrl("userinfo")
        if port not in (None, 443):
            raise UnsafeUrl("port")
        host = _normalize_host(parts.hostname or "")
        if host not in allowed_hosts:
            raise UnsafeUrl("host_not_allowed")
        path = parts.path or "/"
        if any(seg in ("..", ".") for seg in path.split("/")) or "\\" in path:
            raise UnsafeUrl("path_traversal")
        query = tuple(
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
        )
        return cls(host=host, path=path, query=query)

    def __str__(self) -> str:
        suffix = f"?{urlencode(self.query)}" if self.query else ""
        return f"https://{self.host}{self.path}{suffix}"


def _normalize_host(hostname: str) -> str:
    if not hostname:
        raise UnsafeUrl("no_host")
    try:
        ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        pass
    else:
        raise UnsafeUrl("ip_literal")
    try:
        return hostname.lower().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeUrl("bad_host") from exc
