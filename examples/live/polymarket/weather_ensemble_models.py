from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal


WeatherMetric = Literal["high", "low"]
WeatherBandType = Literal["or_higher", "or_lower", "exact"]
WeatherSide = Literal["yes", "no"]
FilterStatus = Literal["actionable", "filtered"]


@dataclass(frozen=True, slots=True)
class EnsembleForecastSnapshot:
    source: str
    model_name: str
    target_date: date
    latitude: float
    longitude: float
    timezone: str
    temperature_unit: str
    ensemble_high: float | None
    ensemble_low: float | None
    member_highs: tuple[float, ...]
    member_lows: tuple[float, ...]
    generation_time_ms: float | None = None

    @property
    def member_count(self) -> int:
        return min(len(self.member_highs), len(self.member_lows))


@dataclass(frozen=True, slots=True)
class WeatherMarketSnapshot:
    market_slug: str
    city: str
    observation_date: date
    metric: WeatherMetric | str
    band_type: WeatherBandType | str
    threshold: float
    yes_price: float


@dataclass(frozen=True, slots=True)
class FilterOutcome:
    status: FilterStatus
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WeatherEnsembleSignalDecision:
    market_slug: str
    city: str
    observation_date: date
    metric: str
    band_type: str
    threshold: float
    selected_side: WeatherSide | None
    model_yes_probability: float | None
    market_yes_price: float
    edge: float | None
    entry_price: float | None
    confidence: float | None
    filter_status: FilterStatus
    filter_reasons: tuple[str, ...]
    forecast_source: str | None
    forecast_model: str | None
