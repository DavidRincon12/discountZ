from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest

from discountz.domain.errors import BudgetExhausted, HardBlock, NotFound, SourceError, Unavailable
from discountz.domain.scheduling import SourceBudget
from discountz.infra.http import (
    MAX_BODY_BYTES,
    BlockKind,
    FakeHttp,
    HttpResponse,
    HttpxClient,
    PacedClient,
)

URL = "https://www.exito.com/api/x"
BUDGET = SourceBudget("grupo_exito", 2.0, 2.0, 5, 1500, timedelta(hours=1), timedelta(hours=3), 50)


def ok(
    body: bytes = b"{}", status: int = 200, headers: dict[str, str] | None = None
) -> HttpResponse:
    return HttpResponse(status, URL, headers or {}, body, 10)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    def monotonic(self) -> float:
        return self.now


def captcha(resp: HttpResponse) -> BlockKind | None:
    return BlockKind.CAPTCHA if b"captcha" in resp.body else None


def paced(fake: FakeHttp, clock: Clock, budget: SourceBudget = BUDGET) -> PacedClient:
    return PacedClient(
        fake, budget, clock.sleep, clock.monotonic, random.Random(0), captcha, source="exito"
    )


def test_respects_min_pause_between_requests() -> None:
    clock = Clock()
    client = paced(FakeHttp({URL: ok()}), clock)
    client.request("GET", URL)
    client.request("GET", URL)
    assert len(clock.sleeps) == 1
    assert 2.0 <= clock.sleeps[0] <= 4.0


def test_budget_exhausted_after_max_per_run() -> None:
    clock = Clock()
    client = paced(FakeHttp({URL: ok()}), clock)
    for _ in range(5):
        client.request("GET", URL)
    with pytest.raises(BudgetExhausted):
        client.request("GET", URL)
    assert client.used == 5


def test_503_retries_twice_then_unavailable() -> None:
    clock = Clock()
    fake = FakeHttp({URL: [ok(status=503)] * 3})
    with pytest.raises(Unavailable) as exc:
        paced(fake, clock).request("GET", URL)
    assert len(fake.calls) == 3
    assert exc.value.http_status == 503


def test_retry_succeeds_on_second_attempt() -> None:
    clock = Clock()
    fake = FakeHttp({URL: [ok(status=502), ok(b'{"a":1}')]})
    assert paced(fake, clock).request("GET", URL).body == b'{"a":1}'


def test_429_with_retry_after_sleeps_that_long() -> None:
    clock = Clock()
    fake = FakeHttp({URL: [ok(status=429, headers={"Retry-After": "5"}), ok()]})
    paced(fake, clock).request("GET", URL)
    assert 5.0 in clock.sleeps


def test_429_with_long_retry_after_is_not_retried() -> None:
    fake = FakeHttp({URL: [ok(status=429, headers={"Retry-After": "120"}), ok()]})
    with pytest.raises(Unavailable):
        paced(fake, Clock()).request("GET", URL)
    assert len(fake.calls) == 1


def test_captcha_is_hard_block_without_retry() -> None:
    fake = FakeHttp({URL: [ok(b"<html>captcha</html>"), ok()]})
    with pytest.raises(HardBlock) as exc:
        paced(fake, Clock()).request("GET", URL)
    assert len(fake.calls) == 1
    assert exc.value.kind == "blocked"
    assert "exito" in str(exc.value) and URL not in str(exc.value)


def test_post_is_never_retried() -> None:
    fake = FakeHttp({URL: [ok(status=503), ok()]})
    with pytest.raises(Unavailable):
        paced(fake, Clock()).request("POST", URL, json={"items": []})
    assert len(fake.calls) == 1


def test_404_is_not_found() -> None:
    with pytest.raises(NotFound):
        paced(FakeHttp({URL: ok(status=404)}), Clock()).request("GET", URL)


def test_oversized_body_is_unavailable() -> None:
    big = ok(b"x" * (MAX_BODY_BYTES + 1))
    with pytest.raises(Unavailable):
        paced(FakeHttp({URL: big}), Clock()).request("GET", URL)


def test_fake_http_unmapped_url_fails_the_test() -> None:
    with pytest.raises(AssertionError):
        FakeHttp({}).request("GET", URL)


def test_fake_http_keys_include_params() -> None:
    fake = FakeHttp({URL + "?ft=ssd&_from=0": ok(b"[1]")})
    assert fake.request("GET", URL, params={"ft": "ssd", "_from": "0"}).body == b"[1]"


def test_source_errors_share_base() -> None:
    assert issubclass(HardBlock, SourceError) and issubclass(NotFound, SourceError)


def test_no_retry_when_it_would_pass_the_soft_deadline() -> None:
    clock = Clock()
    fake = FakeHttp({URL: [ok(status=503), ok()]})
    client = PacedClient(
        fake,
        BUDGET,
        clock.sleep,
        clock.monotonic,
        random.Random(0),
        captcha,
        source="exito",
        deadline=clock.now + 10,
    )
    with pytest.raises(Unavailable):
        client.request("GET", URL)
    assert len(fake.calls) == 1


def test_httpx_client_sends_browser_headers_by_default() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, content=b"{}")

    client = HttpxClient("exito", transport=httpx.MockTransport(handler))
    client.request("GET", URL)
    assert "python-httpx" not in seen["user-agent"] and "Chrome" in seen["user-agent"]
    assert seen["accept-language"].startswith("es-CO")
    assert "sec-ch-ua" in seen


def test_httpx_client_enforces_total_time_per_request() -> None:
    clock = Clock()

    def slow_chunks() -> Iterator[bytes]:
        for _ in range(10):
            clock.now += 5  # cada fragmento tarda 5 s
            yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=slow_chunks())

    client = HttpxClient("exito", transport=httpx.MockTransport(handler), monotonic=clock.monotonic)
    with pytest.raises(Unavailable) as exc:
        client.request("GET", URL)
    assert exc.value.code == "deadline"


def test_paced_client_passes_files() -> None:
    fake = FakeHttp({"https://x.test/up": HttpResponse(200, "https://x.test/up", {}, b"ok", 1)})
    client = paced(fake, Clock())
    files = {"f": ("n.png", b"P", "image/png")}
    client.request("POST", "https://x.test/up", data={"a": "1"}, files=files)
    assert fake.uploads == [({"a": "1"}, {"f": ("n.png", b"P", "image/png")})]
