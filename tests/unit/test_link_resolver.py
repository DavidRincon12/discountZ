"""Enlace real de las ofertas de canales (`app.links.make_link_resolver`)."""

from __future__ import annotations

import logging

import pytest

from discountz.app.links import make_link_resolver
from discountz.domain.errors import Unavailable
from discountz.domain.urls import SafeUrl
from discountz.infra.http import FakeHttp, HttpResponse
from discountz.sources.registry import load_all
from discountz.sources.telegram_channels import channel_link_hosts

JOY = "https://joylink.io/clubgratis/sublime-oxford-sneakers"
load_all()  # los hosts de las tiendas registradas también valen
HOSTS = channel_link_hosts()


def link(raw: str) -> SafeUrl:
    return SafeUrl.parse(raw, HOSTS)


def redirect(to: str, status: int = 302) -> HttpResponse:
    return HttpResponse(status, JOY, {"location": to}, b"", 1)


def test_resolver_replaces_joylink_with_bare_amazon_product() -> None:
    net = FakeHttp({JOY: redirect("https://www.amazon.com/dp/B0FT9S8KLR?tag=clubgratis203-20")})
    assert str(make_link_resolver(net, HOSTS)(link(JOY))) == "https://www.amazon.com/dp/B0FT9S8KLR"
    assert [c[1] for c in net.calls] == [JOY]  # Amazon nunca se descarga


def test_resolver_follows_joylink_hops() -> None:
    hop = "https://joylink.io/r/abc"
    net = FakeHttp(
        {JOY: redirect(hop, 301), hop: redirect("https://www.amazon.com/dp/B0F3WX9DQZ?tag=maas")}
    )
    assert str(make_link_resolver(net, HOSTS)(link(JOY))) == "https://www.amazon.com/dp/B0F3WX9DQZ"


def test_resolver_strips_referral_from_other_allowed_stores() -> None:
    net = FakeHttp({JOY: redirect("https://www.ebay.com/itm/123456789012?utm_source=joy&ref=x")})
    assert str(make_link_resolver(net, HOSTS)(link(JOY))) == "https://www.ebay.com/itm/123456789012"


@pytest.mark.parametrize(
    ("route", "code"),
    [
        (redirect("https://evil.example.com/x"), "redirect"),
        (redirect("http://www.amazon.com/dp/B0FT9S8KLR"), "redirect"),
        (HttpResponse(302, JOY, {}, b"", 1), "redirect"),
        (HttpResponse(200, JOY, {}, b"<html>", 1), "status"),
        (HttpResponse(404, JOY, {}, b"", 1), "status"),
    ],
)
def test_resolver_keeps_channel_link_when_it_cannot_resolve(
    route: HttpResponse, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    net = FakeHttp({JOY: route})
    with caplog.at_level(logging.INFO, logger="discountz"):
        assert make_link_resolver(net, HOSTS)(link(JOY)) == link(JOY)
    assert f"link_unresolved code={code}" in caplog.text
    assert "joylink" not in caplog.text  # nunca la URL


def test_resolver_keeps_link_on_network_error() -> None:
    class Down:
        def request(self, *args: object, **kwargs: object) -> HttpResponse:
            raise Unavailable("fotos", "network")

    assert make_link_resolver(Down(), HOSTS)(link(JOY)) == link(JOY)


def test_resolver_stops_redirect_loops() -> None:
    hop = "https://joylink.io/r/abc"
    net = FakeHttp({JOY: redirect(hop), hop: redirect(JOY)})
    assert make_link_resolver(net, HOSTS)(link(JOY)) == link(JOY)
    assert len(net.calls) <= 3


def test_resolver_leaves_other_links_untouched() -> None:
    net = FakeHttp({})
    fb = link("https://www.facebook.com/permalink.php?id=1&story_fbid=2")
    assert make_link_resolver(net, HOSTS)(fb) == fb
    assert net.calls == []
