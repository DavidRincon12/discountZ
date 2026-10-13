import pytest

from discountz.domain.errors import UnsafeUrl
from discountz.domain.urls import SafeUrl

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
