"""Sonda DESECHABLE del spike de fuentes (no es código de producción; se borra al terminar).

Pregunta: ¿qué tiendas entregan un precio legible desde un runner de GitHub Actions
sin proxies de pago? Criterio: >= 80 % de las peticiones con precio legible.
Compara dos clientes: httpx "plano" con cabeceras de Chrome y curl_cffi suplantando
la huella TLS de Chrome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

import httpx
from curl_cffi.requests import AsyncSession

ATTEMPTS = 4
TIMEOUT_S = 20
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_LANG = "es-CO,es;q=0.9,en-US;q=0.8,en;q=0.7"

Extractor = Callable[[str], str | None]

BLOCK_MARKERS = {
    "amazon-captcha": re.compile(
        r"Enter the characters you see below|api-services-support@amazon\.com|validateCaptcha", re.I
    ),
    "perimeterx": re.compile(r"px-captcha|Robot or human|perimeterx", re.I),
    "akamai-denied": re.compile(r"Access Denied.{0,300}Reference\s*#", re.I | re.S),
    "cloudflare": re.compile(r"cf-chl-|Just a moment\.\.\.|Attention Required", re.I),
    "datadome": re.compile(r"captcha-delivery\.com", re.I),
    "ml-verify": re.compile(r"account-verification|suspicious-traffic|negative_traffic", re.I),
    "captcha": re.compile(r"g-recaptcha|hcaptcha|captcha", re.I),
}


def rx(pattern: str) -> Extractor:
    compiled = re.compile(pattern, re.S)

    def extract(text: str) -> str | None:
        match = compiled.search(text)
        if not match:
            return None
        return next((g for g in match.groups() if g), match.group(0))

    return extract


def vtex(text: str) -> str | None:
    data = json.loads(text)
    for product in data:
        for item in product.get("items", []):
            for seller in item.get("sellers", []):
                price = seller.get("commertialOffer", {}).get("Price")
                if price:
                    return str(price)
    return None


def vtex_intelligent(text: str) -> str | None:
    data = json.loads(text)
    for product in data.get("products", []):
        for item in product.get("items", []):
            for seller in item.get("sellers", []):
                price = seller.get("commertialOffer", {}).get("Price")
                if price:
                    return str(price)
    return None


def ml_api_search(text: str) -> str | None:
    results = json.loads(text).get("results", [])
    return str(results[0]["price"]) if results else None


def ml_api_site(text: str) -> str | None:
    return json.loads(text).get("default_currency_id")


def trm(text: str) -> str | None:
    rows = json.loads(text)
    return rows[0].get("valor") if rows else None


def rss_with_prices(text: str) -> str | None:
    if text.count("<item") < 1 and text.count("<entry") < 1:
        return None
    match = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", text)
    return match.group(1) if match else None


def reddit_json(text: str) -> str | None:
    posts = json.loads(text).get("data", {}).get("children", [])
    for post in posts:
        match = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", post.get("data", {}).get("title", ""))
        if match:
            return match.group(1)
    return None


COP_HTML = r"\$\s?([1-9]\d{0,2}(?:\.\d{3})+)"
USD_HTML = r"\$\s?([1-9][\d,]*\.\d{2})"


@dataclass(frozen=True)
class Follow:
    """Deriva una página de producto a partir de una página de búsqueda."""

    pattern: str
    template: str
    name: str
    extractor: Extractor


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    extractor: Extractor
    json: bool = False
    follow: Follow | None = None


TARGETS: list[Target] = [
    # Colombia
    Target("ml-api-site", "https://api.mercadolibre.com/sites/MCO", ml_api_site, json=True),
    Target(
        "ml-api-search-anon",
        "https://api.mercadolibre.com/sites/MCO/search?q=ssd%201tb&limit=3",
        ml_api_search,
        json=True,
    ),
    Target(
        "ml-listado-html",
        "https://listado.mercadolibre.com.co/ssd-1tb",
        rx(r"andes-money-amount__fraction[^>]*>([\d.]+)"),
        follow=Follow(
            r'href="(https://(?:articulo|www)\.mercadolibre\.com\.co/[^"#?]+)',
            "{0}",
            "ml-producto-html",
            rx(r'itemprop="price" content="([\d.]+)"|"price":\s*([\d.]+)|andes-money-amount__fraction[^>]*>([\d.]+)'),
        ),
    ),
    Target(
        "exito-vtex-legacy",
        "https://www.exito.com/api/catalog_system/pub/products/search?ft=ssd&_from=0&_to=4",
        vtex,
        json=True,
    ),
    Target(
        "exito-vtex-intelligent",
        "https://www.exito.com/api/io/_v/api/intelligent-search/product_search/?query=ssd&count=5",
        vtex_intelligent,
        json=True,
    ),
    Target(
        "carulla-vtex-legacy",
        "https://www.carulla.com/api/catalog_system/pub/products/search?ft=cafe&_from=0&_to=4",
        vtex,
        json=True,
    ),
    Target(
        "olimpica-vtex-legacy",
        "https://www.olimpica.com/api/catalog_system/pub/products/search?ft=televisor&_from=0&_to=4",
        vtex,
        json=True,
    ),
    Target(
        "panamericana-vtex-legacy",
        "https://www.panamericana.com.co/api/catalog_system/pub/products/search?ft=portatil&_from=0&_to=4",
        vtex,
        json=True,
    ),
    Target("alkosto-html", "https://www.alkosto.com/search?text=ssd", rx(COP_HTML)),
    Target("ktronix-html", "https://www.ktronix.com/search?text=ssd", rx(COP_HTML)),
    Target(
        "falabella-html",
        "https://www.falabella.com.co/falabella-co/search?Ntt=ssd",
        rx(r'"price":\["([\d.,]+)"\]|' + COP_HTML),
    ),
    Target(
        "homecenter-html",
        "https://www.homecenter.com.co/homecenter-co/search?Ntt=taladro",
        rx(r'"price":\["([\d.,]+)"\]|' + COP_HTML),
    ),
    # Estados Unidos
    Target(
        "amazon-search-html",
        "https://www.amazon.com/s?k=ssd+1tb",
        rx(r'class="a-price-whole">([\d,]+)'),
        follow=Follow(
            r"/dp/([A-Z0-9]{10})",
            "https://www.amazon.com/dp/{0}",
            "amazon-dp-html",
            rx(r'class="a-price-whole">([\d,]+)|"priceAmount":([\d.]+)'),
        ),
    ),
    Target(
        "walmart-search-html",
        "https://www.walmart.com/search?q=ssd+1tb",
        rx(r'"currentPrice":\{[^}]*?"price":([\d.]+)|"priceString":"\$([\d.,]+)"|' + USD_HTML),
        follow=Follow(
            r'href="(/ip/[^"?#]+)',
            "https://www.walmart.com{0}",
            "walmart-ip-html",
            rx(r'"currentPrice":\{[^}]*?"price":([\d.]+)|itemprop="price"[^>]*content="([\d.]+)"|' + USD_HTML),
        ),
    ),
    Target(
        "newegg-search-html",
        "https://www.newegg.com/p/pl?d=ssd+1tb",
        rx(r"price-current[^>]*>.*?<strong>([\d,]+)</strong>"),
    ),
    Target(
        "bestbuy-search-html",
        "https://www.bestbuy.com/site/searchpage.jsp?st=ssd+1tb",
        rx(r'"currentPrice":([\d.]+)|' + USD_HTML),
    ),
    Target(
        "ebay-search-html",
        "https://www.ebay.com/sch/i.html?_nkw=ssd+1tb&LH_BIN=1",
        rx(r'(?:s-item__price|s-card__price)">\$([\d,.]+)'),
    ),
    Target(
        "bhphoto-search-html",
        "https://www.bhphotovideo.com/c/search?q=ssd%201tb",
        rx(r'uppedDecimalPrice\w*">\$?([\d,]+)|' + USD_HTML),
    ),
    Target(
        "microcenter-search-html",
        "https://www.microcenter.com/search/search_results.aspx?Ntt=ssd",
        rx(r'data-price="([\d.]+)"|itemprop="price" content="([\d.]+)"'),
    ),
    # Agregadores y datos auxiliares
    Target(
        "slickdeals-rss",
        "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
        rss_with_prices,
    ),
    Target("reddit-bapcs-json", "https://www.reddit.com/r/buildapcsales/new.json?limit=10", reddit_json, json=True),
    Target("reddit-bapcs-rss", "https://www.reddit.com/r/buildapcsales/new/.rss", rss_with_prices),
    Target(
        "trm-datos-gov",
        "https://www.datos.gov.co/resource/32sa-8pi3.json?$limit=1&$order=vigenciadesde%20DESC",
        trm,
        json=True,
    ),
]


@dataclass
class Attempt:
    target: str
    client: str
    status: int | None
    elapsed_ms: int
    size: int
    final_url: str
    price: str | None
    block: str | None
    error: str | None


def detect_block(text: str) -> str | None:
    head = text[:200_000]
    return next((name for name, marker in BLOCK_MARKERS.items() if marker.search(head)), None)


def safe_extract(extractor: Extractor, text: str) -> str | None:
    try:
        return extractor(text)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


class Clients:
    def __init__(self) -> None:
        self.plain = httpx.AsyncClient(
            headers={"User-Agent": CHROME_UA, "Accept-Language": ACCEPT_LANG},
            follow_redirects=True,
            timeout=TIMEOUT_S,
        )
        self.impersonate = AsyncSession(impersonate="chrome", timeout=TIMEOUT_S)

    async def get(self, client: str, url: str, accept: str) -> tuple[int, str, str]:
        if client == "plain":
            response = await self.plain.get(url, headers={"Accept": accept})
            return response.status_code, response.text, str(response.url)
        response = await self.impersonate.get(
            url, headers={"Accept": accept, "Accept-Language": ACCEPT_LANG}, allow_redirects=True
        )
        return response.status_code, response.text, str(response.url)

    async def close(self) -> None:
        await self.plain.aclose()
        await self.impersonate.close()


async def probe_once(clients: Clients, client: str, target: Target) -> tuple[Attempt, str]:
    accept = "application/json" if target.json else ACCEPT_HTML
    started = time.perf_counter()
    try:
        status, text, final_url = await clients.get(client, target.url, accept)
    except Exception as exc:  # la sonda registra cualquier fallo de red y sigue
        elapsed = int((time.perf_counter() - started) * 1000)
        return Attempt(target.name, client, None, elapsed, 0, "", None, None, repr(exc)[:160]), ""
    elapsed = int((time.perf_counter() - started) * 1000)
    price = safe_extract(target.extractor, text) if status < 400 else None
    block = None if price else detect_block(text)
    attempt = Attempt(target.name, client, status, elapsed, len(text), final_url[:120], price, block, None)
    return attempt, text


async def probe_target(clients: Clients, target: Target) -> list[Attempt]:
    attempts: list[Attempt] = []
    for client in ("impersonate", "plain"):
        follow_url: str | None = None
        for _ in range(ATTEMPTS):
            attempt, text = await probe_once(clients, client, target)
            attempts.append(attempt)
            if target.follow and follow_url is None and text:
                match = re.search(target.follow.pattern, text)
                if match:
                    follow_url = target.follow.template.format(match.group(1))
            await asyncio.sleep(random.uniform(1.5, 3.5))
        if target.follow and follow_url:
            child = Target(target.follow.name, follow_url, target.follow.extractor)
            for _ in range(ATTEMPTS):
                attempt, _text = await probe_once(clients, client, child)
                attempts.append(attempt)
                await asyncio.sleep(random.uniform(1.5, 3.5))
    return attempts


def summarize(attempts: list[Attempt]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[Attempt]] = {}
    for attempt in attempts:
        groups.setdefault((attempt.target, attempt.client), []).append(attempt)
    rows = []
    for (target, client), items in groups.items():
        ok = sum(1 for a in items if a.price)
        rows.append(
            {
                "target": target,
                "client": client,
                "ok": f"{ok}/{len(items)}",
                "statuses": sorted({str(a.status) for a in items}),
                "blocks": sorted({a.block for a in items if a.block}),
                "errors": sorted({a.error for a in items if a.error})[:1],
                "p50_ms": int(statistics.median(a.elapsed_ms for a in items)),
                "sample_price": next((a.price for a in items if a.price), None),
                "final_url": items[-1].final_url,
            }
        )
    return rows


async def runner_ip() -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            return (await client.get("https://api.ipify.org")).text.strip()
    except httpx.HTTPError as exc:
        return f"desconocida ({exc!r})"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default="local")
    parser.add_argument("--out", default="resultado.json")
    args = parser.parse_args()

    ip = await runner_ip()
    clients = Clients()
    try:
        nested = await asyncio.gather(*(probe_target(clients, target) for target in TARGETS))
    finally:
        await clients.close()
    attempts = [attempt for group in nested for attempt in group]
    rows = summarize(attempts)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"run": args.run, "ip": ip, "attempts": [asdict(a) for a in attempts]}, handle, indent=1)

    lines = [f"## Ejecución {args.run} · IP {ip}", "", "| fuente | cliente | ok | estados | bloqueo | p50 ms | precio |", "|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['target']} | {row['client']} | {row['ok']} | {','.join(row['statuses'])} "
            f"| {','.join(row['blocks']) or '-'} | {row['p50_ms']} | {row['sample_price'] or '-'} |"
        )
    report = "\n".join(lines)
    print(report)
    for row in rows:
        print("RESULT " + json.dumps({"run": args.run, "ip": ip, **row}, ensure_ascii=False))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")


if __name__ == "__main__":
    asyncio.run(main())
