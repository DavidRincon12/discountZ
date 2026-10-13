"""Fixtures grabadas: cuerpo gzip + `.meta.json`, sin cookies ni cabeceras (spec §7.3)."""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

from discountz.infra.http import HttpResponse

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
_EXTENSIONS = ("json", "html", "xml")
_IPV4 = re.compile(rb"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(
    rb"\b(?:[0-9a-fA-F]{1,4}:){2,7}(?::|[0-9a-fA-F]{1,4})\b|\b(?:[0-9a-fA-F]{1,4}:){2,}:[0-9a-fA-F:]*"
)
_DELIVER = re.compile(rb"Deliver to[^<]{0,120}", re.IGNORECASE)
_PII_WORDS = re.compile(rb"Bucaramanga|680001", re.IGNORECASE)


def scrub(body: bytes) -> bytes:
    """Reemplaza IPs, «Deliver to …» y ciudades conocidas antes de guardar una fixture."""
    body = _IPV4.sub(b"0.0.0.0", body)
    body = _IPV6.sub(b"::", body)
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
            return HttpResponse(int(meta["status"]), str(meta["url"]), {},
                                gzip.decompress(path.read_bytes()), 0)  # fmt: skip
    raise FileNotFoundError(f"{source}/{name}")
