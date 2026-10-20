import pytest

from discountz.domain.errors import UnsafeUrl
from discountz.domain.urls import SafeUrl, amazon_product_url

EXITO = frozenset({"www.exito.com"})


@pytest.mark.parametrize(
    "raw",
    [
        "http://www.exito.com/x/p",
        "https://www.exito.com@evil.com/x",
        "https://user:pw@www.exito.com/x",
        "https://192.168.0.1/x",
        "https://[::1]/x",
        "https://www.exito.com:8443/x",
        "https://exito.com.evil.com/x",
        "ftp://www.exito.com/x",
        "https://www.exito.com/a/../../etc",
        "https://www.exito.com/x\n",
        "https://www.exito.com:abc/x",
        "",
    ],
)
def test_safe_url_rejects(raw: str) -> None:
    with pytest.raises(UnsafeUrl):
        SafeUrl.parse(raw, EXITO)


def test_safe_url_strips_tracking() -> None:
    u = SafeUrl.parse(
        "https://WWW.Amazon.com/dp/B0EXAMPLE1?tag=x&utm_source=y&th=1&keep=1",
        frozenset({"www.amazon.com"}),
    )
    assert str(u) == "https://www.amazon.com/dp/B0EXAMPLE1?keep=1"
    assert u.host == "www.amazon.com"
    assert u.path == "/dp/B0EXAMPLE1"
    assert u.query == (("keep", "1"),)


def test_safe_url_accepts_port_443_and_drops_fragment() -> None:
    u = SafeUrl.parse("https://www.exito.com:443/tv-55/p?skuId=12#reviews", EXITO)
    assert str(u) == "https://www.exito.com/tv-55/p?skuId=12"


def test_safe_url_idna_host_matches_exactly() -> None:
    idna_host = "www.\u00e9xito.com".encode("idna").decode()
    u = SafeUrl.parse("https://www.\u00e9xito.com/x", frozenset({idna_host}))
    assert u.host == idna_host
    with pytest.raises(UnsafeUrl):
        SafeUrl.parse("https://www.\u00e9xito.com/x", EXITO)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://www.amazon.com/dp/B0FT9S8KLR?tag=clubgratis203-20",
            "https://www.amazon.com/dp/B0FT9S8KLR",
        ),
        (
            "https://www.amazon.com/dp/B0F3WX9DQZ?maas=maas_adg_api_5809&ref_=aa_maas&tag=maas"
            "&aa_campaignid=lv_C&aa_adgroupid=lv_q&aa_creativeid=lv_B&m=A22LQ2Y41ZRFI0",
            "https://www.amazon.com/dp/B0F3WX9DQZ",
        ),
        (
            "https://www.amazon.co.jp/-/en/gp/product/B084HPMVNN?tag=promocajita-22&linkId=1",
            "https://www.amazon.co.jp/dp/B084HPMVNN",
        ),
        (
            "https://amazon.com/Nautica-Sneakers/dp/B0FT9S8KLR/ref=sr_1_1",
            "https://amazon.com/dp/B0FT9S8KLR",
        ),
    ],
)
def test_amazon_product_url_keeps_only_the_asin(raw: str, expected: str) -> None:
    hosts = frozenset({"www.amazon.com", "amazon.com", "www.amazon.co.jp"})
    url = amazon_product_url(SafeUrl.parse(raw, hosts))
    assert url is not None
    assert str(url) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.amazon.com/s?k=tv",  # búsqueda, no ficha
        "https://www.amazon.com/dp/short",
        "https://www.ebay.com/itm/123456789012",
        "https://amazon.com.evil.com/dp/B0FT9S8KLR",
    ],
)
def test_amazon_product_url_ignores_other_pages(raw: str) -> None:
    hosts = frozenset({"www.amazon.com", "www.ebay.com", "amazon.com.evil.com"})
    assert amazon_product_url(SafeUrl.parse(raw, hosts)) is None
