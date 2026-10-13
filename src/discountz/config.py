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

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from discountz.domain.commands import CATEGORIES
from discountz.domain.errors import ConfigError
from discountz.domain.money import Currency
from discountz.domain.params import CategoryParams, GlobalParams
from discountz.domain.scheduling import Event, SourceBudget

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
PARAMS_PATH = CONFIG_DIR / "discountz.toml"
CANARIES_PATH = CONFIG_DIR / "canaries.toml"
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
    healthchecks_url: SecretStr | None = None
    soft_deadline_s: Annotated[int, Field(gt=0, le=900)] = 480
    gh_run_id: int | None = None
    gh_run_attempt: Annotated[int, Field(ge=1)] | None = None
    trigger: str = "local"


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
    max_batch: Annotated[int, Field(ge=1, le=50)]


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


class _Params(_Strict):
    quiet_hours: _Quiet
    pricing: _Pricing
    categories: dict[str, _Category]
    sources: dict[str, _Source]
    events: list[_Event] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AppParams:
    categories: Mapping[str, CategoryParams]
    glob: GlobalParams
    budgets: Mapping[str, SourceBudget]
    events: Sequence[Event]
    quiet_start: time
    quiet_end: time


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
