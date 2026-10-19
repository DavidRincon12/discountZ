"""Diagnóstico temporal: descarga las fotos de los canales como lo hace el ciclo."""

import io
import os
from datetime import UTC, datetime

from PIL import Image

from discountz.app.deliver import card_png, make_photo_fetcher
from discountz.app.render import channel_card
from discountz.infra.http import HttpxClient
from discountz.sources.registry import load_all
from discountz.sources.telegram_channels import CHANNELS, parse_channel_page

now = datetime.now(UTC)
sources = load_all()
page_http = HttpxClient("diag")
pics = HttpxClient("fotos")
fetch = make_photo_fetcher(pics, sources)
os.makedirs("out", exist_ok=True)
for code, ch in CHANNELS.items():
    resp = page_http.request("GET", f"https://t.me/s/{ch}")
    page = parse_channel_page(code, resp.body, now)
    print(f"== {code} status={resp.status} posts={len(page.posts)}", flush=True)
    for p in page.posts[-3:]:
        if p.image_url is None:
            print(f"  {p.post_id} sin_url")
            continue
        url = str(p.image_url)
        raw = pics.request("GET", url)
        info = ""
        try:
            with Image.open(io.BytesIO(raw.body)) as im:
                info = f"{im.format} {im.size} {im.mode}"
        except Exception as exc:  # noqa: BLE001
            info = type(exc).__name__
        ok = fetch(code, url)
        print(
            f"  {p.post_id} host={p.image_url.host} status={raw.status} bytes={len(raw.body)} "
            f"ctype={raw.headers.get('content-type') if hasattr(raw, 'headers') else '?'} {info} "
            f"valid={ok is not None}",
            flush=True,
        )
        with open(f"out/raw_{code}_{p.post_id}.bin", "wb") as f:
            f.write(raw.body)
        with open(f"out/card_{code}_{p.post_id}.png", "wb") as f:
            f.write(card_png(channel_card(p, "general"), ok))
        import base64
        with Image.open(io.BytesIO(card_png(channel_card(p, "general"), ok))) as c:
            c = c.convert("RGB")
            c.thumbnail((540, 540))
            b = io.BytesIO()
            c.save(b, "JPEG", quality=70)
        print(f"B64 {code}_{p.post_id} {base64.b64encode(b.getvalue()).decode()}", flush=True)
