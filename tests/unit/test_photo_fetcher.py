"""Descarga de la foto de la tarjeta (`app.deliver.make_photo_fetcher`)."""

from __future__ import annotations

import io
import logging

import pytest
from PIL import Image

from discountz.app.deliver import make_photo_fetcher
from discountz.infra.http import FakeHttp, HttpResponse
from discountz.sources.registry import load_all

IMG = "https://exitocol.vteximg.com.br/arquivos/ids/1-1000-1000/a.jpg"
CDN = "https://exitocol.vtexassets.com/arquivos/ids/1-1000-1000/a.jpg"


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def redirect(to: str, status: int = 301) -> HttpResponse:
    return HttpResponse(status, IMG, {"location": to}, b"", 1)


def test_photo_fetcher_follows_redirect_to_allowed_host() -> None:
    net = FakeHttp({IMG: redirect(CDN), CDN: HttpResponse(200, CDN, {}, jpeg(), 1)})
    assert make_photo_fetcher(net, load_all())("exito", IMG) is not None


def test_photo_fetcher_ignores_redirect_to_foreign_host() -> None:
    net = FakeHttp({IMG: redirect("https://evil.example.com/a.jpg", 302)})
    assert make_photo_fetcher(net, load_all())("exito", IMG) is None
    assert len(net.calls) == 1


def test_photo_fetcher_stops_redirect_loops() -> None:
    net = FakeHttp({IMG: redirect(CDN), CDN: redirect(IMG)})
    assert make_photo_fetcher(net, load_all())("exito", IMG) is None
    assert len(net.calls) <= 3


def test_photo_fetcher_logs_why_photo_is_missing(caplog: pytest.LogCaptureFixture) -> None:
    net = FakeHttp({IMG: HttpResponse(404, IMG, {}, b"", 1)})
    with caplog.at_level(logging.INFO, logger="discountz"):
        assert make_photo_fetcher(net, load_all())("exito", IMG) is None
    assert "photo_missing source=exito code=status http_status=404" in caplog.text
    assert "vteximg" not in caplog.text  # nunca la URL
