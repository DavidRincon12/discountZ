"""Precisión del tema de canales y feeds contra ofertas reales etiquetadas a mano.

`ofertas-etiquetadas.jsonl`: 946 ofertas de los cinco canales de Telegram, DealNews y
Slickdeals Amazon leídas el 2026-10-19, con el tema (o los temas, si hay dos razonables) que
debería tener cada una. Antes de este conjunto la asignación acertaba el 51 %.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from discountz.config import load_topics
from discountz.domain.community import ChannelPost, assign_channel_topic
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl

LABELED = Path(__file__).parents[1] / "fixtures" / "topics" / "ofertas-etiquetadas.jsonl"
MIN_PRECISION = 0.95
_LINK = SafeUrl.parse("https://t.me/canal/1", frozenset({"t.me"}))


def _post(row: dict[str, object]) -> ChannelPost:
    return ChannelPost(
        channel=str(row["canal"]),
        post_id=1,
        published_at=datetime(2026, 10, 19, tzinfo=UTC),
        title=str(row["titulo"]),
        price=Money(1000, Currency.USD),
        coupon=None,
        store=None,
        hashtags=tuple(str(h) for h in row["hashtags"]),  # type: ignore[attr-defined]
        image_url=None,
        link=_LINK,
    )


def test_channel_topics_match_hand_labels() -> None:
    rules = load_topics().channel_rules
    rows = [json.loads(line) for line in LABELED.read_text(encoding="utf-8").splitlines()]
    misses = [
        (row["titulo"], got, row["temas"])
        for row in rows
        if (got := assign_channel_topic(_post(row), rules)) not in row["temas"]
    ]
    precision = 1 - len(misses) / len(rows)
    assert precision >= MIN_PRECISION, f"{precision:.1%}: {misses[:20]}"
