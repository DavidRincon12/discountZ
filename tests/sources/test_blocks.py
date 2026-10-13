import pytest

from discountz.infra.http import HttpResponse
from discountz.sources.blocks import BlockKind, detect_block_common


def resp(body: bytes = b"<html>ok</html>", status: int = 200, url: str = "https://x.com/p",
         headers: dict[str, str] | None = None) -> HttpResponse:  # fmt: skip
    return HttpResponse(status, url, headers or {}, body, 5)


@pytest.mark.parametrize(
    ("r", "kind"),
    [
        (resp(status=403), BlockKind.HTTP_403),
        (resp(b"<title>Just a moment...</title>"), BlockKind.CHALLENGE),
        (resp(b"<script src='/cdn-cgi/challenge-platform/cf-chl-x'>"), BlockKind.CHALLENGE),
        (resp(headers={"cf-mitigated": "challenge"}), BlockKind.CHALLENGE),
        (resp(url="https://www.walmart.com/blocked?url=x"), BlockKind.PERIMETERX),
        (resp(b"<div id='px-captcha'></div>"), BlockKind.PERIMETERX),
        (resp(b"<h1>Robot or human?</h1>"), BlockKind.PERIMETERX),
        (resp(b"<form action='/errors/validateCaptcha'>"), BlockKind.CAPTCHA),
        (resp(b"Enter the characters you see below"), BlockKind.CAPTCHA),
        (resp(b"https://geo.captcha-delivery.com/captcha"), BlockKind.DATADOME),
        (resp(b"<h1>Access Denied</h1> Reference #18.abc"), BlockKind.AKAMAI),
        (
            resp(url="https://www.mercadolibre.com.co/gz/account-verification?x"),
            BlockKind.ACCOUNT_VERIFICATION,
        ),
        (resp(status=406), BlockKind.VTEX_406),
    ],
)
def test_markers(r: HttpResponse, kind: BlockKind) -> None:
    assert detect_block_common(r) == kind


def test_normal_page_is_not_blocked() -> None:
    assert detect_block_common(resp(b"<html><title>SSD Kingston</title>$ 289.900</html>")) is None
    assert detect_block_common(resp(b"[]", status=206)) is None
