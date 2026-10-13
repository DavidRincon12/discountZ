"""Fixtures grabadas: cuerpo gzip + `.meta.json`, sin cookies ni cabeceras (spec §7.3)."""

from __future__ import annotations

import gzip
import ipaddress
import json
import re
from pathlib import Path

from discountz.infra.http import HttpResponse

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
_EXTENSIONS = ("json", "html", "xml")
_IPV4 = re.compile(rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(rb"(?<![0-9A-Fa-f:])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:]*(?![0-9A-Fa-f:])")
_DELIVER = re.compile(rb"Deliver to[^<]{0,120}", re.IGNORECASE)
_PII_WORDS = re.compile(rb"Bucaramanga|680001", re.IGNORECASE)


def _only_ip(match: re.Match[bytes], version: int, replacement: bytes) -> bytes:
    """Reemplaza el candidato solo si es una IP válida (no horas `12:30:45` ni versiones)."""
    try:
        ip = ipaddress.ip_address(match.group(0).decode("ascii"))
    except ValueError:
        return match.group(0)
    return replacement if ip.version == version else match.group(0)


def scrub(body: bytes) -> bytes:
    """Reemplaza IPs, «Deliver to …» y ciudades conocidas antes de guardar una fixture."""
    body = _IPV4.sub(lambda m: _only_ip(m, 4, b"0.0.0.0"), body)
    body = _IPV6.sub(lambda m: _only_ip(m, 6, b"::"), body)
    body = _DELIVER.sub(b"Deliver to [redactado]", body)
    return _PII_WORDS.sub(b"[redactado]", body)


def _extension(resp: HttpResponse) -> str:
    head = resp.body.lstrip()[:1]
    if head in (b"{", b"["):
        return "json"
    if resp.body.lstrip()[:5] == b"<?xml" or b"<rss" in resp.body[:500]:
        return "xml"
    return "html"


def save_fixture(
    root: Path, source: str, name: str, resp: HttpResponse, *, recorded_at: str
) -> Path:
    folder = root / source
    folder.mkdir(parents=True, exist_ok=True)
    ext = _extension(resp)
    path = folder / f"{name}.{ext}.gz"
    path.write_bytes(gzip.compress(scrub(resp.body), mtime=0))
    meta = {"status": resp.status, "url": resp.url.split("?", 1)[0], "recorded_at": recorded_at}
    (folder / f"{name}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return path


def load_fixture(source: str, name: str, *, root: Path = FIXTURES_DIR) -> HttpResponse:
    folder = root / source
    meta = json.loads((folder / f"{name}.meta.json").read_text())
    for ext in _EXTENSIONS:
        path = folder / f"{name}.{ext}.gz"
        if path.exists():
            return HttpResponse(
                int(meta["status"]), str(meta["url"]), {}, gzip.decompress(path.read_bytes()), 0
            )
    raise FileNotFoundError(f"{source}/{name}")
