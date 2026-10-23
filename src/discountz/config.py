"""Configuración validada en el borde: variables de entorno, parámetros y canarios.

Los errores de validación se reducen a `ubicación: tipo` (nunca el valor recibido), porque
salen en logs públicos y una variable mal puesta podría contener un secreto.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

from discountz.domain.commands import CATEGORIES
from discountz.domain.community import ChannelRule, stem
from discountz.domain.errors import ConfigError
from discountz.domain.interests import (
    INTEREST_TOPICS,
    MAX_TERM_CHARS,
    MAX_TERMS,
    Interest,
    compile_interest,
    interest_key,
    term_problem,
)
from discountz.domain.model import normalize_title
from discountz.domain.money import Currency
from discountz.domain.params import CategoryParams, GlobalParams
from discountz.domain.scheduling import CrawlBudget, Event, SourceBudget
from discountz.domain.topics import (
    TOPIC_CODES,
    TopicMode,
    TopicParams,
    TopicRule,
    normalize_path,
)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
PARAMS_PATH = CONFIG_DIR / "discountz.toml"
CANARIES_PATH = CONFIG_DIR / "canaries.toml"
TOPICS_PATH = CONFIG_DIR / "topics.toml"
INTERESTS_PATH = CONFIG_DIR / "interests.toml"
BOGOTA = timezone(timedelta(hours=-5), "COT")

NonNeg = Annotated[int, Field(ge=0)]
Pos = Annotated[int, Field(gt=0)]
Bp = Annotated[int, Field(ge=0, le=10_000)]


class Settings(BaseSettings):
    """Variables `DISCOUNTZ_*` del entorno (secretos solo como `SecretStr`)."""

    model_config = SettingsConfigDict(env_prefix="DISCOUNTZ_", hide_input_in_errors=True)

    database_url: SecretStr
    telegram_bot_token: SecretStr
    telegram_chat_id: int
    telegram_owner_id: int
    # Canal general (solo lectura) que recibe copia de todas las ofertas; sin él no se copia.
    telegram_channel_id: int | None = None
    healthchecks_url: SecretStr | None = None
    soft_deadline_s: Annotated[int, Field(gt=0, le=900)] = 480
    gh_run_id: int | None = None
    gh_run_attempt: Annotated[int, Field(ge=1)] | None = None
    trigger: str = "local"
    # Rastreo de EE. UU. (spec del rastreo §2.1 y §4.5): sin clave, la fuente queda apagada.
    bestbuy_api_key: SecretStr | None = None
    walmart_consumer_id: str | None = None
    walmart_key_version: str | None = None
    walmart_private_key: SecretStr | None = None
    walmart_publisher_id: str | None = None
    # eBay (ADR 0008): App ID y Cert ID del keyset de producción; sin ellos, eBay no rastrea.
    ebay_app_id: str | None = None
    ebay_cert_id: SecretStr | None = None
    # Woot (ADR 0015): clave de su API; sin ella, el canal de Woot queda no disponible.
    woot_api_key: SecretStr | None = None

    @field_validator(
        "bestbuy_api_key",
        "ebay_app_id",
        "telegram_channel_id",
        "ebay_cert_id",
        "walmart_consumer_id",
        "walmart_key_version",
        "walmart_private_key",
        "walmart_publisher_id",
        "woot_api_key",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _walmart_all_or_none(self) -> Settings:
        given = (
            self.walmart_consumer_id,
            self.walmart_key_version,
            self.walmart_private_key,
            self.walmart_publisher_id,
        )
        if any(v is not None for v in given) and not all(v is not None for v in given):
            raise PydanticCustomError("walmart_incomplete", "faltan credenciales de Walmart")
        return self

    @model_validator(mode="after")
    def _ebay_both_or_none(self) -> Settings:
        if (self.ebay_app_id is None) != (self.ebay_cert_id is None):
            raise PydanticCustomError("ebay_incomplete", "faltan credenciales de ebay")
        return self


def _describe(exc: ValidationError, prefix: str) -> str:
    parts = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{prefix}{loc}: {err['type']}")
    return "; ".join(parts)


def load_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # los campos vienen del entorno
    except ValidationError as exc:
        raise ConfigError(_describe(exc, "env.")) from None


# ───────────── Parámetros (config/discountz.toml)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Quiet(_Strict):
    start: time
    end: time


class _Pricing(_Strict):
    h_min_h: Pos
    flash_max_h: Pos
    validity_min_d: Pos
    adapt_min_coverage_d: Pos
    rho_bp: Bp
    anchor_window_d: Pos
    power10_tol_bp: Bp
    confirm_tol_bp: Bp
    persistence_gap_min: Pos
    peers_min: Pos
    peers_price_bp: Bp
    peers_list_bp: Bp
    storm_min: Pos
    jump_window_d: Pos
    gap_before_d: Pos
    min_abs_cop: NonNeg
    min_abs_usd: NonNeg
    min_price_cop: NonNeg
    min_price_usd: NonNeg
    catalog_min_saving_cop: NonNeg
    catalog_min_saving_usd: NonNeg
    rarity_tol_bp: Bp
    improve_bp: Bp
    rearm_h: Pos
    glitch_reclass_d: Pos


class _Category(_Strict):
    window_d: Pos
    mature_coverage_d: Pos
    t_good_bp: Bp
    t_floor_bp: Bp
    d_low_bp: Bp
    d_floor_bp: Bp
    epsilon_bp: Bp
    g_bp: Bp
    f_max_bp: Bp
    cooldown_h: Pos
    allow_marketplace: bool
    k_d: Pos
    d_trend_bp: Bp


class _Source(_Strict):
    group: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")]
    min_pause_s: NonNeg
    jitter_s: NonNeg
    max_per_run: Pos
    max_per_24h: Pos
    watch_interval_min: Pos
    search_interval_min: Pos
    max_batch: Annotated[int, Field(ge=1, le=100)]
    max_auto_watches: NonNeg = 0


class _Event(_Strict):
    name: Annotated[str, Field(min_length=1, max_length=60)]
    sources: Annotated[list[str], Field(min_length=1)]
    starts: datetime
    ends: datetime

    @field_validator("starts", "ends")
    @classmethod
    def _naive(cls, v: datetime) -> datetime:
        if v.tzinfo is not None:
            raise ValueError("sin zona horaria: se interpreta en hora de Bogotá")
        return v


class _Crawl(_Strict):
    min_pause_s: Annotated[float, Field(ge=0)]
    jitter_s: Annotated[float, Field(ge=0)]
    max_per_run: Pos
    max_per_24h: Pos
    hot_share_bp: Annotated[int, Field(ge=0, le=10_000)] = 2500


class _Channels(_Strict):
    interval_min: Pos = 10


class _Cards(_Strict):
    daily_per_source: NonNeg = 8


class _Ebay(_Strict):
    ship_to_zip: Annotated[str, Field(pattern=r"^[0-9]{5}$")] = "33166"
    items_per_partition: Annotated[int, Field(ge=1, le=200)] = 75
    max_items: Pos = 150_000
    min_feedback_pct: Annotated[int, Field(ge=0, le=100)] = 98
    min_feedback_score: NonNeg = 100
    excluded_roots: list[Pos] = Field(default_factory=list)


class _Params(_Strict):
    quiet_hours: _Quiet
    pricing: _Pricing
    categories: dict[str, _Category]
    sources: dict[str, _Source]
    events: list[_Event] = Field(default_factory=list)
    crawl: dict[str, _Crawl] = Field(default_factory=dict)
    channels: _Channels = Field(default_factory=_Channels)
    cards: _Cards = Field(default_factory=_Cards)
    ebay: _Ebay = Field(default_factory=_Ebay)


@dataclass(frozen=True, slots=True)
class EbayParams:
    """Parámetros del rastreo de eBay (spec de eBay §2 y §3)."""

    ship_to_zip: str = "33166"
    items_per_partition: int = 75
    max_items: int = 150_000
    min_feedback_pct: int = 98
    min_feedback_score: int = 100
    excluded_roots: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class AppParams:
    categories: Mapping[str, CategoryParams]
    glob: GlobalParams
    budgets: Mapping[str, SourceBudget]
    events: Sequence[Event]
    quiet_start: time
    quiet_end: time
    crawl: Mapping[str, CrawlBudget] = MappingProxyType({})
    channel_interval: timedelta = timedelta(minutes=10)  # cada canal como máximo cada N
    ebay: EbayParams = EbayParams()
    daily_cards: int = 8  # tarjetas por fuente al día; el resto va al resumen (ADR 0012)


def _days(n: int) -> timedelta:
    return timedelta(days=n)


def _hours(n: int) -> timedelta:
    return timedelta(hours=n)


def _read_toml(path: Path) -> object:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path.name}: {type(exc).__name__}") from None


def load_params(path: Path = PARAMS_PATH) -> AppParams:
    try:
        raw = _Params.model_validate(_read_toml(path))
    except ValidationError as exc:
        raise ConfigError(_describe(exc, f"{path.name}:")) from None
    if set(raw.categories) != CATEGORIES:
        raise ConfigError(f"{path.name}: las categorías deben ser {', '.join(sorted(CATEGORIES))}")
    for name, cat in raw.categories.items():
        if cat.t_floor_bp > cat.t_good_bp or cat.d_floor_bp > cat.d_low_bp:
            raise ConfigError(f"{path.name}:categories.{name}: piso mayor que el umbral")
    if set(raw.crawl) - set(raw.sources):
        raise ConfigError(f"{path.name}:crawl: fuente desconocida")
    for e in raw.events:
        unknown = set(e.sources) - set(raw.sources)
        if unknown:
            raise ConfigError(f"{path.name}:events.{e.name}: fuente desconocida")
        if e.ends <= e.starts:
            raise ConfigError(f"{path.name}:events.{e.name}: el fin debe ser posterior al inicio")
    p = raw.pricing
    glob = GlobalParams(
        h_min=_hours(p.h_min_h),
        flash_max=_hours(p.flash_max_h),
        validity_min=_days(p.validity_min_d),
        adapt_min_coverage=_days(p.adapt_min_coverage_d),
        rho_bp=p.rho_bp,
        anchor_window=_days(p.anchor_window_d),
        power10_tol_bp=p.power10_tol_bp,
        confirm_tol_bp=p.confirm_tol_bp,
        persistence_gap=timedelta(minutes=p.persistence_gap_min),
        peers_min=p.peers_min,
        peers_price_bp=p.peers_price_bp,
        peers_list_bp=p.peers_list_bp,
        storm_min=p.storm_min,
        jump_window=_days(p.jump_window_d),
        gap_before=_days(p.gap_before_d),
        min_abs=MappingProxyType({Currency.COP: p.min_abs_cop, Currency.USD: p.min_abs_usd}),
        min_price=MappingProxyType({Currency.COP: p.min_price_cop, Currency.USD: p.min_price_usd}),
        catalog_min_saving=MappingProxyType(
            {Currency.COP: p.catalog_min_saving_cop, Currency.USD: p.catalog_min_saving_usd}
        ),
        rarity_tol_bp=p.rarity_tol_bp,
        improve_bp=p.improve_bp,
        rearm=_hours(p.rearm_h),
        glitch_reclass=_days(p.glitch_reclass_d),
    )
    categories = {
        name: CategoryParams(
            window=_days(c.window_d),
            mature_coverage=_days(c.mature_coverage_d),
            t_good_bp=c.t_good_bp,
            t_floor_bp=c.t_floor_bp,
            d_low_bp=c.d_low_bp,
            d_floor_bp=c.d_floor_bp,
            epsilon_bp=c.epsilon_bp,
            g_bp=c.g_bp,
            f_max_bp=c.f_max_bp,
            cooldown=_hours(c.cooldown_h),
            allow_marketplace=c.allow_marketplace,
            k=_days(c.k_d),
            d_trend_bp=c.d_trend_bp,
        )
        for name, c in raw.categories.items()
    }
    budgets = {
        code: SourceBudget(
            group=s.group,
            min_pause_s=float(s.min_pause_s),
            jitter_s=float(s.jitter_s),
            max_per_run=s.max_per_run,
            max_per_24h=s.max_per_24h,
            watch_interval=timedelta(minutes=s.watch_interval_min),
            search_interval=timedelta(minutes=s.search_interval_min),
            max_batch=s.max_batch,
            max_auto_watches=s.max_auto_watches,
        )
        for code, s in raw.sources.items()
    }
    events = [
        Event(
            name=e.name,
            sources=frozenset(e.sources),
            starts_at=e.starts.replace(tzinfo=BOGOTA).astimezone(UTC),
            ends_at=e.ends.replace(tzinfo=BOGOTA).astimezone(UTC),
        )
        for e in raw.events
    ]
    return AppParams(
        categories=MappingProxyType(categories),
        glob=glob,
        budgets=MappingProxyType(budgets),
        events=tuple(events),
        quiet_start=raw.quiet_hours.start,
        quiet_end=raw.quiet_hours.end,
        crawl=MappingProxyType(
            {
                code: CrawlBudget(
                    c.min_pause_s, c.jitter_s, c.max_per_run, c.max_per_24h, c.hot_share_bp
                )
                for code, c in raw.crawl.items()
            }
        ),
        channel_interval=timedelta(minutes=raw.channels.interval_min),
        daily_cards=raw.cards.daily_per_source,
        ebay=EbayParams(
            ship_to_zip=raw.ebay.ship_to_zip,
            items_per_partition=raw.ebay.items_per_partition,
            max_items=raw.ebay.max_items,
            min_feedback_pct=raw.ebay.min_feedback_pct,
            min_feedback_score=raw.ebay.min_feedback_score,
            excluded_roots=frozenset(raw.ebay.excluded_roots),
        ),
    )


# ───────────── Canarios (config/canaries.toml)


class _Canary(_Strict):
    external_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{1,40}$")]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    min_minor: Pos
    max_minor: Pos


@dataclass(frozen=True, slots=True)
class Canary:
    source: str
    external_id: str
    title: str
    min_minor: int
    max_minor: int

    def in_range(self, price_minor: int) -> bool:
        return self.min_minor <= price_minor <= self.max_minor


def load_canaries(path: Path = CANARIES_PATH) -> Mapping[str, list[Canary]]:
    raw = _read_toml(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: formato inválido")
    out: dict[str, list[Canary]] = {}
    for source, items in raw.items():
        if not isinstance(items, list) or not 1 <= len(items) <= 2:
            raise ConfigError(f"{path.name}:{source}: entre 1 y 2 canarios")
        for i, item in enumerate(items):
            try:
                c = _Canary.model_validate(item)
            except ValidationError as exc:
                raise ConfigError(_describe(exc, f"{path.name}:{source}.{i}.")) from None
            if c.min_minor >= c.max_minor:
                raise ConfigError(f"{path.name}:{source}.{i}: min_minor debe ser < max_minor")
            out.setdefault(source, []).append(
                Canary(source, c.external_id, c.title, c.min_minor, c.max_minor)
            )
    return MappingProxyType(out)


# ───────────── Temas (config/topics.toml)


class _Topic(_Strict):
    title: Annotated[str, Field(min_length=1, max_length=60)]
    t_bp: Bp | None = None
    g_bp: Bp | None = None
    category: str
    mode: TopicMode = "card"


class _Rule(_Strict):
    source: Annotated[str, Field(pattern=r"^(\*|[a-z][a-z0-9_]{1,31})$")] = "*"
    prefixes: Annotated[list[Annotated[str, Field(min_length=2)]], Field(min_length=1)]
    topic: str


class _ChannelRule(_Strict):
    topic: str
    words: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]


class _Topics(_Strict):
    topics: dict[str, _Topic]
    intentional_otros: list[str] = Field(default_factory=list)
    rules: list[_Rule]
    channel_rules: list[_ChannelRule] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Topics:
    params: Mapping[str, TopicParams]  # en el orden de TOPIC_CODES
    rules: tuple[TopicRule, ...]
    intentional_otros: frozenset[str]  # «fuente:/ruta/» normalizada
    channel_rules: tuple[ChannelRule, ...] = ()  # tema de las ofertas de canales


def load_topics(path: Path = TOPICS_PATH) -> Topics:
    try:
        raw = _Topics.model_validate(_read_toml(path))
    except ValidationError as exc:
        raise ConfigError(_describe(exc, f"{path.name}:")) from None
    if set(raw.topics) != set(TOPIC_CODES):
        raise ConfigError(f"{path.name}: los temas deben ser {', '.join(TOPIC_CODES)}")
    for code, t in raw.topics.items():
        if t.category not in CATEGORIES:
            raise ConfigError(f"{path.name}:topics.{code}: categoría desconocida")
        if (t.t_bp is None) != (t.g_bp is None):
            raise ConfigError(f"{path.name}:topics.{code}: t_bp y g_bp van juntos")
    rules: list[TopicRule] = []
    for i, r in enumerate(raw.rules):
        if r.topic not in raw.topics:
            raise ConfigError(f"{path.name}:rules.{i}: tema desconocido")
        rules.extend(TopicRule(r.source, normalize_path(p), r.topic) for p in r.prefixes)
    otros = set()
    for item in raw.intentional_otros:
        source, sep, route = item.partition(":")
        if not sep:
            raise ConfigError(f"{path.name}:intentional_otros: formato «fuente:/ruta/»")
        otros.add(f"{source}:{normalize_path(route)}")
    params = {
        code: TopicParams(
            code,
            raw.topics[code].title,
            raw.topics[code].t_bp,
            raw.topics[code].g_bp,
            raw.topics[code].category,
            raw.topics[code].mode,
        )
        for code in TOPIC_CODES
    }
    channel_rules: list[ChannelRule] = []
    seen: dict[str, str] = {}  # término en singular → tema: un término nombra un solo tema
    for i, cr in enumerate(raw.channel_rules):
        if cr.topic not in raw.topics:
            raise ConfigError(f"{path.name}:channel_rules.{i}: tema desconocido")
        words = frozenset(normalize_title(term) for term in cr.words) - {""}
        for term in sorted(words):
            other = seen.setdefault(" ".join(stem(t) for t in term.split()), cr.topic)
            if other != cr.topic:
                raise ConfigError(f"{path.name}:channel_rules.{i}: «{term}» ya está en {other}")
        channel_rules.append(ChannelRule(cr.topic, words))
    return Topics(MappingProxyType(params), tuple(rules), frozenset(otros), tuple(channel_rules))


class _Interest(_Strict):
    name: Annotated[str, Field(min_length=1, max_length=40)]
    topic: str
    terms: Annotated[list[str], Field(min_length=1, max_length=MAX_TERMS)]
    exclude: list[Annotated[str, Field(min_length=1, max_length=MAX_TERM_CHARS)]] = Field(
        default_factory=list
    )


class _Interests(_Strict):
    daily_cards_per_interest: Annotated[int, Field(ge=1, le=20)]
    interests: list[_Interest]


@dataclass(frozen=True, slots=True)
class InterestsConfig:
    daily_cards_per_interest: int  # tarjetas por interés al día de Bogotá (spec del perfil §3.3)
    seeds: tuple[Interest, ...]  # en el orden del archivo: desempata a igual largo de término


def load_interests(path: Path = INTERESTS_PATH) -> InterestsConfig:
    try:
        raw = _Interests.model_validate(_read_toml(path))
    except ValidationError as exc:
        raise ConfigError(_describe(exc, f"{path.name}:")) from None
    seen: set[str] = set()
    for i, it in enumerate(raw.interests):
        where = f"{path.name}:interests.{i}"
        if it.topic not in INTEREST_TOPICS:
            raise ConfigError(f"{where}: tema desconocido (van {', '.join(INTEREST_TOPICS)})")
        if (key := interest_key(it.name)) in seen:
            raise ConfigError(f"{where}: nombre repetido «{it.name}»")
        seen.add(key)
        for term in it.terms:
            if problem := term_problem(term):
                raise ConfigError(f"{where}: «{term}» {problem}")
    seeds = tuple(compile_interest(it.name, it.topic, it.terms, it.exclude) for it in raw.interests)
    return InterestsConfig(raw.daily_cards_per_interest, seeds)
