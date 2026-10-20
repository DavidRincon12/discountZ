"""Tarjeta PNG de una alerta del catálogo (spec del rastreo §5.2, maqueta aprobada).

1080×1080: píldora de la tienda y `#Tema`, foto en un recuadro blanco (o un recuadro con el
nombre de la tienda si no hay foto válida), insignia `-NN%`, precio en verde, «Antes» tachado,
mínimo o aviso de posible error, casillero en USD y la marca. Fuentes Inter (OFL) en
`assets/fonts/`. Las fotos llegan sin validar: `validate_photo` decide si se usan.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from discountz.domain.model import AlertKind
from discountz.domain.money import Currency, Money, format_money

FONTS_DIR = Path(__file__).resolve().parents[3] / "assets" / "fonts"
SIZE = 1080
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_PHOTO_PIXELS = 25_000_000  # tope contra bombas de descompresión
_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
PHOTO_BOX = (920, 560)  # espacio de la foto dentro del recuadro blanco

BG = (15, 23, 42)
WHITE = (255, 255, 255)
MUTED = (148, 163, 184)
SOFT = (226, 232, 240)
PLACEHOLDER_TEXT = (100, 116, 139)
BRAND = (71, 85, 105)
GREEN = (74, 222, 128)
YELLOW = (250, 204, 21)
RED = (239, 68, 68)

STORE_COLORS: Mapping[str, str] = MappingProxyType(
    {
        "exito": "#F2C200",
        "carulla": "#1E7B3A",
        "olimpica": "#D7182A",
        "panamericana": "#E11D48",
        "jumbo": "#00A650",
        "pepeganga": "#F97316",
        "bestbuy_us": "#0046BE",
        "walmart_us": "#0071DC",
        "amazon_us": "#FF9900",
        "newegg": "#F7941D",
        # Canales de Telegram: azules de Telegram, distintos de los colores de las tiendas.
        "tg_descuentostech": "#229ED9",
        "tg_clubgratis": "#2B6CB0",
        "tg_cajitatech": "#0E7490",
        "tg_descuentoshome": "#1D4ED8",
        "tg_ofertu": "#0369A1",
        # Feeds sin verificar: colores propios (no son los de sus marcas).
        "dealnews": "#7C3AED",
        "slickdeals_amazon": "#4D7C0F",
        "ebay_us": "#0064D2",
    }
)
DEFAULT_COLOR = "#334155"


@dataclass(frozen=True, slots=True)
class CardData:
    store_name: str
    store_color: str  # "#RRGGBB"
    topic_title: str  # sin emoji: Inter no los dibuja
    price: Money
    usual: Money | None  # None ⇒ tarjeta `community` (canales): sin «antes» ni %
    drop_bp: int | None
    min_days: int | None
    kind: AlertKind
    badge: str | None = None  # franja «<Canal> · sin verificar»
    coupon: str | None = None
    reference_label: str = "Antes"  # eBay §4.2: «Mediana de N publicaciones del mismo modelo»
    note: str | None = None  # reemplaza la línea «Mínimo en N días» (eBay: vendedor y envío)


@cache
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS_DIR / f"Inter-{weight}.otf"), size)


def _rgb(color: str) -> tuple[int, int, int]:
    raw = color.lstrip("#")
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _ink_on(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Texto oscuro sobre colores claros (Éxito amarillo), blanco sobre el resto."""
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return BG if luma > 160 else WHITE


def validate_photo(raw: bytes) -> bytes | None:
    """La foto solo si pesa ≤ 5 MB, es JPEG/PNG/WebP y pasa `Image.verify`."""
    if not raw or len(raw) > MAX_PHOTO_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format not in _FORMATS or img.width * img.height > MAX_PHOTO_PIXELS:
                return None
            img.verify()
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    return raw


def _fit_font(
    draw: ImageDraw.ImageDraw, text: str, weight: str, size: int, max_width: int
) -> ImageFont.FreeTypeFont:
    while size > 24:
        font = _font(weight, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 6
    return _font(weight, size)


def _photo_box(canvas: Image.Image, data: CardData, photo: bytes | None) -> None:
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((40, 130, 1040, 730), radius=28, fill=WHITE)
    if photo is not None:
        with Image.open(io.BytesIO(photo)) as img:
            pic = img.convert("RGBA")
        # `thumbnail` solo reduce: las miniaturas de 250-500 px quedaban diminutas. Se escala
        # en ambos sentidos hasta tocar el recuadro, sin deformar.
        scale = min(PHOTO_BOX[0] / pic.width, PHOTO_BOX[1] / pic.height)
        size = (max(1, round(pic.width * scale)), max(1, round(pic.height * scale)))
        pic = pic.resize(size, Image.Resampling.LANCZOS)
        x = 540 - pic.width // 2
        y = 430 - pic.height // 2
        canvas.paste(pic, (x, y), pic)
        return
    draw.rounded_rectangle((300, 200, 700, 660), radius=32, fill=SOFT)
    font = _fit_font(draw, data.store_name.upper(), "Bold", 44, 340)
    draw.text((500, 430), data.store_name.upper(), font=font, fill=PLACEHOLDER_TEXT, anchor="mm")


def _badge(draw: ImageDraw.ImageDraw, drop_bp: int) -> None:
    draw.ellipse((830, 150, 1020, 340), fill=YELLOW)
    pct = f"-{drop_bp // 100}%"
    draw.text((925, 235), pct, font=_fit_font(draw, pct, "Black", 64, 170), fill=BG, anchor="mm")
    draw.text((925, 287), "vs. su precio", font=_font("SemiBold", 20), fill=BG, anchor="mm")


def render_card(data: CardData, photo: bytes | None) -> bytes:
    canvas = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(canvas)
    color = _rgb(data.store_color)

    store = data.store_name.upper()
    pill_font = _fit_font(draw, store, "Bold", 32, 480)
    pill_w = int(draw.textlength(store, font=pill_font)) + 88
    draw.rounded_rectangle((40, 36, 40 + pill_w, 104), radius=34, fill=color)
    draw.text((40 + pill_w // 2, 70), store, font=pill_font, fill=_ink_on(color), anchor="mm")
    topic = f"#{data.topic_title}"
    draw.text(
        (1040, 70), topic, font=_fit_font(draw, topic, "SemiBold", 32, 440), fill=MUTED, anchor="rm"
    )

    _photo_box(canvas, data, photo)
    if data.drop_bp is not None:
        _badge(draw, data.drop_bp)

    price = format_money(data.price)
    draw.text(
        (56, 848), price, font=_fit_font(draw, price, "Black", 140, 960), fill=GREEN, anchor="ls"
    )

    if data.usual is None:
        _community_lines(draw, data, color)
    else:
        _catalog_lines(draw, data, data.usual)
    if data.price.currency is Currency.USD:
        draw.text(
            (60, 1035),
            "EE. UU. · Compra por casillero",
            font=_font("Regular", 28),
            fill=MUTED,
            anchor="ls",
        )
    draw.text((1040, 1050), "discountZ", font=_font("Bold", 28), fill=BRAND, anchor="rs")

    out = io.BytesIO()
    canvas.save(out, "PNG", optimize=True)
    return out.getvalue()


def _community_lines(
    draw: ImageDraw.ImageDraw, data: CardData, color: tuple[int, int, int]
) -> None:
    """Oferta de un canal: cupón y franja «sin verificar» en lugar del «antes» y el %."""
    if data.coupon:
        line = f"Cupón {data.coupon}"
        font = _fit_font(draw, line, "SemiBold", 40, 960)
        draw.text((60, 922), line, font=font, fill=SOFT, anchor="ls")
    if data.badge:
        font = _fit_font(draw, data.badge, "Bold", 32, 900)
        width = int(draw.textlength(data.badge, font=font)) + 64
        draw.rounded_rectangle((40, 950, 40 + width, 1004), radius=27, fill=color)
        draw.text((40 + width // 2, 977), data.badge, font=font, fill=_ink_on(color), anchor="mm")


def _catalog_lines(draw: ImageDraw.ImageDraw, data: CardData, usual: Money) -> None:
    before = format_money(usual)
    label = f"{data.reference_label} "
    label_font = _fit_font(draw, label + before, "SemiBold", 40, 960)
    draw.text((60, 922), label, font=label_font, fill=MUTED, anchor="ls")
    x = 60 + int(draw.textlength(label, font=label_font))
    draw.text((x, 922), before, font=label_font, fill=MUTED, anchor="ls")
    if data.reference_label == "Antes":  # la mediana del modelo no es un precio tachado
        width = int(draw.textlength(before, font=label_font))
        draw.line((x - 2, 909, x + width + 2, 909), fill=MUTED, width=4)

    if data.kind is AlertKind.PRICE_ERROR:
        draw.text(
            (60, 985), "POSIBLE ERROR DE PRECIO", font=_font("Bold", 34), fill=RED, anchor="ls"
        )
        return
    line = data.note
    if line is None and data.kind is not AlertKind.MODEL_LOW:
        line = f"Mínimo en {data.min_days} días"
    if line is not None:
        draw.text(
            (60, 985), line, font=_fit_font(draw, line, "Regular", 32, 960), fill=SOFT, anchor="ls"
        )
