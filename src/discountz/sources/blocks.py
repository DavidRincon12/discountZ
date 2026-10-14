"""Detección común de bloqueos con los marcadores del spike (spec §0.4 y §4.4)."""

from __future__ import annotations

from discountz.infra.http import BlockKind, HttpResponse

__all__ = ["BlockKind", "detect_block_common"]

_SCAN_BYTES = 256 * 1024  # los marcadores aparecen al principio de la página
_BODY_MARKERS: tuple[tuple[bytes, BlockKind], ...] = (
    (b"Just a moment...", BlockKind.CHALLENGE),
    (b"cf-chl-", BlockKind.CHALLENGE),
    (b"px-captcha", BlockKind.PERIMETERX),
    (b"Robot or human", BlockKind.PERIMETERX),
    (b"/errors/validateCaptcha", BlockKind.CAPTCHA),
    (b"validateCaptcha", BlockKind.CAPTCHA),
    (b"Enter the characters you see below", BlockKind.CAPTCHA),
    (b"<title>Robot Check</title>", BlockKind.CAPTCHA),
    (b"captcha-delivery.com", BlockKind.DATADOME),
    (b"account-verification", BlockKind.ACCOUNT_VERIFICATION),
)


def detect_block_common(resp: HttpResponse) -> BlockKind | None:
    if resp.status == 403:
        return BlockKind.HTTP_403
    if resp.status == 406:
        return BlockKind.VTEX_406
    headers = {k.lower(): v for k, v in resp.headers.items()}
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return BlockKind.CHALLENGE
    if "/blocked" in resp.url:
        return BlockKind.PERIMETERX
    if "account-verification" in resp.url:
        return BlockKind.ACCOUNT_VERIFICATION
    head = resp.body[:_SCAN_BYTES]
    for marker, kind in _BODY_MARKERS:
        if marker in head:
            return kind
    if b"Access Denied" in head and b"Reference #" in head:
        return BlockKind.AKAMAI
    return None
