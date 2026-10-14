"""Feed comunitario de Slickdeals contra el RSS real (spec §4.3 «Slickdeals»)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.money import Currency, Money
from discountz.infra.http import FakeHttp
from discountz.sources.base import Capability, NoCredentials, SourceContext
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.registry import load_all
from discountz.sources.slickdeals import (
    RSS_URL,
    SlickdealsSource,
    parse_slickdeals_rss,
)

EXP: dict[str, Any] = json.loads(
    (FIXTURES_DIR / "slickdeals" / "frontpage.expected.json").read_text()
)
NOW = datetime.fromisoformat(EXP["now"])


def rss(*items: str) -> bytes:
    body = "".join(f"<item>{i}</item>" for i in items)
    return (
        '<?xml version="1.0"?><rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>'
        f"<title>Slickdeals</title>{body}</channel></rss>"
    ).encode()


def item(title: str, content: str, pub: str = "Tue, 13 Oct 2026 22:00:00 +0000") -> str:
    return (
        f"<title><![CDATA[{title}]]></title><link>https://slickdeals.net/f/1</link>"
        f"<pubDate>{pub}</pubDate><content:encoded><![CDATA[{content}]]></content:encoded>"
    )


def test_real_feed_extracts_amazon_leads() -> None:
    leads = parse_slickdeals_rss(load_fixture("slickdeals", "frontpage").body, NOW)
    assert [str(lead.target_url).rsplit("/", 1)[-1] for lead in leads] == EXP["amazon_asins"]
    first = leads[0]
    assert first.source == "slickdeals"
    assert first.title == EXP["first"]["title"]
    assert first.price == Money(EXP["first"]["price"], Currency.USD)
    assert str(first.target_url) == EXP["first"]["target_url"]
    assert first.published_at == datetime.fromisoformat(EXP["first"]["published_at"])


def test_leads_older_than_24h_are_dropped() -> None:
    oldest = datetime.fromisoformat(EXP["oldest_published_at"])
    body = load_fixture("slickdeals", "frontpage").body
    assert parse_slickdeals_rss(body, oldest + timedelta(hours=24, seconds=1)) != []
    assert parse_slickdeals_rss(body, NOW + timedelta(hours=24)) == []


def test_u2_redirect_is_decoded_without_network() -> None:
    link = (
        '<a href="https://slickdeals.net/click?lno=1&amp;u2=https%3A%2F%2Fwww.newegg.com'
        '%2Fp%2FN82E16820147861%3Fitem%3D1">Newegg</a>'
    )
    (lead,) = parse_slickdeals_rss(rss(item("SSD 990 PRO 1TB $1,299.99", link)), NOW)
    assert str(lead.target_url).startswith("https://www.newegg.com/p/N82E16820147861")
    assert lead.price == Money(129_999, Currency.USD)


def test_direct_store_href_is_used() -> None:
    link = '<a href="https://www.bestbuy.com/site/6512345.p?skuId=6512345">Best Buy</a>'
    (lead,) = parse_slickdeals_rss(rss(item("Monitor 27 in", link)), NOW)
    assert lead.target_url.host == "www.bestbuy.com"
    assert lead.price is None


def test_items_without_store_url_are_ignored() -> None:
    adidas = (
        '<a href="https://slickdeals.net/click?u3=abc" data-product-exitWebsite="adidas.com">x</a>'
    )
    evil = '<a href="https://www.amazon.com.evil.example/dp/B0AAAAAAAA">x</a>'
    userinfo = '<a href="https://www.amazon.com@evil.example/dp/B0AAAAAAAA">x</a>'
    body = rss(item("Zapatos $10", adidas), item("Cosa $5", evil), item("Otra $5", userinfo))
    assert parse_slickdeals_rss(body, NOW) == []


def test_bad_asin_attribute_is_ignored() -> None:
    link = '<a data-product-exitWebsite="amazon.com" data-aps-asin="../../x">Amazon</a>'
    assert parse_slickdeals_rss(rss(item("Cosa $5", link)), NOW) == []


def test_entities_and_dtd_are_rejected() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
        b"<rss><channel><item><title>&b;</title></item></channel></rss>"
    )
    with pytest.raises(ParseError):
        parse_slickdeals_rss(bomb, NOW)


def test_not_xml_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_slickdeals_rss(b"<html>hola", NOW)


def test_fetch_leads_reads_the_frontpage_feed() -> None:
    fake = FakeHttp({RSS_URL: load_fixture("slickdeals", "frontpage")})
    leads = SlickdealsSource().fetch_leads(SourceContext(fake, NoCredentials(), 1e9, NOW))
    assert len(leads) == len(EXP["amazon_asins"])
    assert len(fake.calls) == 1


def test_registered_as_feed() -> None:
    src = load_all()["slickdeals"]
    assert src.descriptor.kind == "feed"
    assert src.descriptor.capabilities == frozenset({Capability.LEADS})
