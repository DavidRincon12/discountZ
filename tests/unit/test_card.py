"""Tarjeta PNG de las alertas del catálogo (spec del rastreo §5.2)."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

from discountz.domain.model import AlertKind
from discountz.domain.money import Currency, Money
from discountz.infra.card import (
    MAX_PHOTO_BYTES,
    STORE_COLORS,
    CardData,
    render_card,
    validate_photo,
)

GOLDEN = Path(__file__).parent.parent / "golden"
C01 = CardData(
    store_name="Panamericana",
    store_color=STORE_COLORS["panamericana"],
    topic_title="Tecnología",
    price=Money(19_900, Currency.COP),
    usual=Money(99_900, Currency.COP),
    drop_bp=8008,
    min_days=90,
    kind=AlertKind.CATALOG_DROP,
)


def photo(size: tuple[int, int] = (400, 600), fmt: str = "JPEG") -> bytes:
    img = Image.new("RGB", size, (30, 120, 220))
    ImageDraw.Draw(img).ellipse((40, 40, size[0] - 40, size[1] - 40), fill=(250, 200, 30))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def png(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def average_hash(img: Image.Image) -> int:
    small = img.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    pixels: list[int] = list(small.getdata())
    mean = sum(pixels) / len(pixels)
    return sum(1 << i for i, p in enumerate(pixels) if p >= mean)


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def region_has_color(
    img: Image.Image, box: tuple[int, int, int, int], rgb: tuple[int, int, int]
) -> bool:
    crop = img.convert("RGB").crop(box)
    pixels: list[tuple[int, int, int]] = list(crop.getdata())
    return any(all(abs(c - t) <= 30 for c, t in zip(px, rgb, strict=True)) for px in pixels)


def test_card_is_1080_png() -> None:
    data = render_card(C01, validate_photo(photo()))
    assert data.startswith(b"\x89PNG")
    assert png(data).size == (1080, 1080)


def test_card_without_photo_draws_store_box() -> None:
    with_photo = png(render_card(C01, validate_photo(photo())))
    without = png(render_card(C01, None))
    box = (300, 200, 700, 660)
    assert region_has_color(with_photo, box, (30, 120, 220))
    assert not region_has_color(without, box, (30, 120, 220))
    assert region_has_color(without, box, (226, 232, 240))  # recuadro gris con el nombre


def test_card_without_photo_on_corrupt_bytes() -> None:
    assert validate_photo(b"\xff\xd8\xff\xe0 no es una imagen") is None
    assert validate_photo(b"GIF89a" + photo()[6:]) is None


def test_card_without_photo_on_huge_file() -> None:
    assert validate_photo(photo() + b"\0" * MAX_PHOTO_BYTES) is None


def test_validate_photo_accepts_png_and_webp() -> None:
    assert validate_photo(photo(fmt="PNG")) is not None
    assert validate_photo(photo(fmt="WEBP")) is not None


def test_card_usd_shows_casillero_line() -> None:
    usd = replace(C01, price=Money(1_999, Currency.USD), usual=Money(9_999, Currency.USD))
    cop_img = png(render_card(C01, None))
    usd_img = png(render_card(usd, None))
    line = (60, 1000, 700, 1050)  # bajo «Mínimo en N días»
    assert distance(average_hash(cop_img.crop(line)), average_hash(usd_img.crop(line))) > 0


def test_card_price_error_banner() -> None:
    err = replace(C01, kind=AlertKind.PRICE_ERROR)
    img = png(render_card(err, None))
    assert region_has_color(img, (60, 940, 1020, 1000), (239, 68, 68))
    assert not region_has_color(png(render_card(C01, None)), (60, 940, 1020, 1000), (239, 68, 68))


def test_card_matches_golden() -> None:
    golden = Image.open(GOLDEN / "card-c01.png")
    img = png(render_card(C01, None))
    assert distance(average_hash(img), average_hash(golden)) <= 5
