"""Tope de tarjetas por fuente al día, una por promoción y resumen de la noche (ADR 0012)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from discountz.app.held import Slot, admit, digest_due, send_digest
from discountz.config import load_params
from discountz.domain.model import AlertKind
from discountz.infra import db as repo

pytestmark = pytest.mark.integration

T = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)  # 10:00 en Bogotá
DROP = AlertKind.CATALOG_DROP
PARAMS = load_params()


def slot(
    kind: AlertKind = DROP,
    drop: int | None = 2000,
    topic: str = "hogar",
    source: str = "exito",
    interest: str | None = None,
) -> Slot:
    return Slot(source, topic, kind, drop, interest)


def carded(db: psycopg.Connection) -> list[tuple[str, bool]]:
    rows = db.execute("SELECT source, carded FROM discountz.offer_log ORDER BY id").fetchall()
    return [(r[0], r[1]) for r in rows]


def digests(db: psycopg.Connection) -> list[tuple[str, str, bool]]:
    rows = db.execute(
        """SELECT dedup_key, body_html, silent FROM discountz.outbox
            WHERE purpose = 'digest' ORDER BY id"""
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def test_first_cards_of_the_day_go_out_and_the_rest_waits(db: psycopg.Connection) -> None:
    sent = [admit(db, slot(drop=1000 + 100 * i), T, 8, f"• {i}") for i in range(9)]
    assert sent == [True] * 8 + [False]
    assert carded(db)[-1] == ("exito", False)
    row = db.execute("SELECT line FROM discountz.offer_log WHERE NOT carded").fetchone()
    assert row == ("• 8",)


def test_the_cap_is_per_source(db: psycopg.Connection) -> None:
    for i in range(8):
        assert admit(db, slot(drop=1000 + 100 * i), T, 8, "• x")
    assert admit(db, slot(source="panamericana"), T, 8, "• x")


def test_price_errors_always_go_out_and_do_not_count(db: psycopg.Connection) -> None:
    for i in range(7):
        assert admit(db, slot(drop=1000 + 100 * i), T, 8, "• x")
    assert admit(db, slot(AlertKind.PRICE_ERROR, 8000), T, 8, "• x")
    assert admit(db, slot(drop=3000), T, 8, "• x")  # la octava
    assert not admit(db, slot(drop=3100), T, 8, "• x")
    assert admit(db, slot(AlertKind.PRICE_ERROR, 9000), T, 8, "• x")


def test_the_day_starts_at_midnight_in_bogota(db: psycopg.Connection) -> None:
    late = datetime(2026, 10, 23, 4, 50, tzinfo=UTC)  # 23:50 del 22 en Bogotá
    for i in range(8):
        assert admit(db, slot(drop=1000 + 100 * i), late, 8, "• x")
    assert not admit(db, slot(drop=3000), late, 8, "• x")
    assert admit(db, slot(drop=3100), late + timedelta(minutes=20), 8, "• x")


def test_same_promotion_waits_without_using_the_cap(db: psycopg.Connection) -> None:
    """Misma tienda, tema y clase, con el ahorro a ±0,5 %: tallas y colores de una promoción."""
    assert admit(db, slot(drop=2000, topic="moda"), T, 2, "• a")
    assert not admit(db, slot(drop=2049, topic="moda"), T, 2, "• b")
    assert admit(db, slot(drop=2051, topic="moda"), T, 2, "• c")  # otra promoción: la 2.ª
    assert admit(db, slot(drop=2000, topic="moda"), T + timedelta(hours=25), 2, "• d")


def test_promotion_needs_the_same_topic_and_kind(db: psycopg.Connection) -> None:
    assert admit(db, slot(drop=2000, topic="moda"), T, 8, "• a")
    assert admit(db, slot(drop=2000, topic="hogar"), T, 8, "• b")
    assert admit(db, slot(AlertKind.PRICE_ERROR, 2000, topic="moda"), T, 8, "• c")
    assert not admit(db, slot(AlertKind.PRICE_ERROR, 2000, topic="moda"), T, 8, "• d")


def test_model_lows_are_never_one_promotion(db: psycopg.Connection) -> None:
    """eBay compara vendedores distintos: dos ahorros iguales son coincidencia."""
    model = AlertKind.MODEL_LOW
    assert admit(db, slot(model, 2100, "tecnologia", "ebay_us"), T, 8, "• a")
    assert admit(db, slot(model, 2100, "tecnologia", "ebay_us"), T, 8, "• b")


def test_digest_goes_after_21h_with_what_waited_until_then(db: psycopg.Connection) -> None:
    admit(db, slot(drop=1500), T, 0, "• mañana")
    evening = datetime(2026, 10, 23, 1, 30, tzinfo=UTC)  # 20:30 en Bogotá
    admit(db, slot(drop=2500), evening, 0, "• tarde")
    assert not digest_due(db, evening)
    nine = datetime(2026, 10, 23, 2, 5, tzinfo=UTC)  # 21:05
    assert digest_due(db, nine)
    assert send_digest(db, nine, PARAMS) == 1
    ((key, body, silent),) = digests(db)
    assert key.startswith("held:2026-10-22:") and not silent
    assert body.index("• tarde") < body.index("• mañana")  # de mayor a menor ahorro
    assert not digest_due(db, nine + timedelta(minutes=2))
    later = datetime(2026, 10, 23, 2, 40, tzinfo=UTC)  # 21:40: ya para mañana
    admit(db, slot(drop=1800), later, 0, "• noche")
    assert not digest_due(db, later + timedelta(hours=2))
    tomorrow = datetime(2026, 10, 24, 2, 0, tzinfo=UTC)  # 21:00 del 23
    assert digest_due(db, tomorrow)
    assert send_digest(db, tomorrow, PARAMS) == 1
    assert "• noche" in digests(db)[-1][1] and "• tarde" not in digests(db)[-1][1]


def test_late_commits_from_before_21h_go_out_in_another_digest(db: psycopg.Connection) -> None:
    """Una corrida del rastreo fija su hora al empezar: lo de las 20:30 puede guardarse a las
    21:10, después del resumen. Sale en otro resumen, no se pierde."""
    admit(db, slot(drop=1500), T, 0, "• mañana")
    nine = datetime(2026, 10, 23, 2, 5, tzinfo=UTC)  # 21:05 en Bogotá
    assert send_digest(db, nine, PARAMS) == 1
    admit(db, slot(drop=2500), datetime(2026, 10, 23, 1, 30, tzinfo=UTC), 0, "• rezagada")
    assert digest_due(db, nine + timedelta(minutes=5))
    assert send_digest(db, nine + timedelta(minutes=5), PARAMS) == 1
    bodies = [body for _, body, _ in digests(db)]
    assert len(bodies) == 2 and "• rezagada" in bodies[1]


def test_nothing_held_means_no_digest(db: psycopg.Connection) -> None:
    admit(db, slot(), T, 8, "• x")
    nine = datetime(2026, 10, 23, 2, 5, tzinfo=UTC)
    assert not digest_due(db, nine)
    assert send_digest(db, nine, PARAMS) == 0
    assert digests(db) == []


def test_long_digest_rings_once(db: psycopg.Connection) -> None:
    for source in ("exito", "panamericana", "alkosto"):
        for i in range(40):
            admit(db, slot(drop=1000 + i, source=source), T, 0, f"• {'x' * 110} {i}")
    nine = datetime(2026, 10, 23, 2, 5, tzinfo=UTC)
    assert send_digest(db, nine, PARAMS) > 1
    assert [silent for _, _, silent in digests(db)][:2] == [False, True]


def test_interest_cards_do_not_count_for_the_source_cap(db: psycopg.Connection) -> None:
    day = T.date()
    for interest, line in (("SSD", None), ("SSD", None), (None, None), ("SSD", "• x")):
        entry = repo.OfferEntry("exito", day, "tecnologia", "catalog_drop", 2000, line, interest)
        repo.log_offer(db, entry, T)
    other_day = repo.OfferEntry("exito", day - timedelta(days=1), "tecnologia", "x", 1, None, "SSD")
    repo.log_offer(db, other_day, T - timedelta(days=1))
    assert repo.count_cards(db, "exito", day) == 1
    assert repo.count_interest_cards(db, "SSD", day) == 2  # sin la retenida ni la de ayer
    assert repo.count_interest_cards(db, "Monitor", day) == 0


def logged_interests(db: psycopg.Connection) -> list[tuple[str | None, bool]]:
    rows = db.execute("SELECT interest, carded FROM discountz.offer_log ORDER BY id").fetchall()
    return [(r[0], r[1]) for r in rows]


def test_interest_slot_skips_a_full_source_cap(db: psycopg.Connection) -> None:
    for i in range(8):
        assert admit(db, slot(drop=1000 + 100 * i), T, 8, "• x")
    assert not admit(db, slot(drop=3000), T, 8, "• x")
    assert admit(db, slot(drop=4000, interest="SSD"), T, 8, "• x")
    assert logged_interests(db)[-1] == ("SSD", True)
    assert not admit(db, slot(drop=5000), T, 8, "• x")  # no le devolvió cupo a la fuente


def test_interest_cap_holds_the_fourth_card(
    db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    sent = [admit(db, slot(drop=1000 + 200 * i, interest="SSD"), T, 8, "• x") for i in range(4)]
    assert sent == [True, True, True, False]
    assert logged_interests(db)[-1] == ("SSD", False)
    assert admit(db, slot(drop=3000, interest="Monitor"), T, 8, "• x")  # el tope es por interés
    assert admit(db, slot(drop=5000, interest="SSD"), T + timedelta(days=1), 8, "• x")
    assert "offer_held source=exito code=interest_cap" in caplog.text
    assert "SSD" not in caplog.text


def test_interest_cap_follows_the_given_limit(db: psycopg.Connection) -> None:
    assert admit(db, slot(drop=1000, interest="SSD"), T, 8, "• x", per_interest=1)
    assert not admit(db, slot(drop=2000, interest="SSD"), T, 8, "• x", per_interest=1)


def test_interest_slot_keeps_the_promotion_rule(db: psycopg.Connection) -> None:
    assert admit(db, slot(drop=2000, interest="SSD"), T, 8, "• x")
    assert not admit(db, slot(drop=2030, interest="SSD"), T + timedelta(hours=1), 8, "• x")
    assert logged_interests(db) == [("SSD", True), ("SSD", False)]


def test_interest_price_errors_always_go_out_and_do_not_count(db: psycopg.Connection) -> None:
    for i in range(3):
        assert admit(db, slot(drop=1000 + 200 * i, interest="SSD"), T, 8, "• x")
    error = slot(AlertKind.PRICE_ERROR, 8000, interest="SSD")
    assert admit(db, error, T, 8, "• x")
    assert repo.count_interest_cards(db, "SSD", T.date()) == 3
