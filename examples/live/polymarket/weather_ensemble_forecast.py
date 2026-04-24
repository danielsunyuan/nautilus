from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import date
from datetime import datetime
import importlib.util
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    from examples.live.polymarket.weather_ensemble_models import EnsembleForecastSnapshot
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_ensemble_models"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_models.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    EnsembleForecastSnapshot = module.EnsembleForecastSnapshot


_MAX_MEMBER_RE = re.compile(r"^temperature_2m_max_member(?P<member>\d+)$")
_MIN_MEMBER_RE = re.compile(r"^temperature_2m_min_member(?P<member>\d+)$")


@dataclass(frozen=True, slots=True)
class OpenMeteoEnsembleForecastConfig:
    base_url: str = "https://ensemble-api.open-meteo.com/v1/ensemble"
    model_name: str = "icon_seamless_eps"
    temperature_unit: str = "celsius"
    timezone: str = "GMT"
    timeout_seconds: float = 15.0
    cache_ttl_seconds: float = 300.0
    max_forecast_days: int = 16


def clip_probability(value: float, *, floor: float = 0.05, ceiling: float = 0.95) -> float:
    if floor > ceiling:
        raise ValueError("floor must be <= ceiling")
    if math.isnan(value):
        raise ValueError("value must be finite")
    bounded = max(0.0, min(1.0, float(value)))
    return max(floor, min(ceiling, bounded))


def _probability(
    members: tuple[float, ...],
    *,
    predicate: Any,
    clip: tuple[float, float] | None,
) -> float:
    if not members:
        raise ValueError("members must not be empty")
    count = sum(1 for value in members if predicate(value))
    probability = count / len(members)
    if clip is None:
        return probability
    return clip_probability(probability, floor=clip[0], ceiling=clip[1])


def probability_high_above(
    snapshot: EnsembleForecastSnapshot,
    threshold: float,
    *,
    clip: tuple[float, float] | None = (0.05, 0.95),
) -> float:
    return _probability(snapshot.member_highs, predicate=lambda value: value >= threshold, clip=clip)


def probability_high_below(
    snapshot: EnsembleForecastSnapshot,
    threshold: float,
    *,
    clip: tuple[float, float] | None = (0.05, 0.95),
) -> float:
    return _probability(snapshot.member_highs, predicate=lambda value: value <= threshold, clip=clip)


def probability_low_above(
    snapshot: EnsembleForecastSnapshot,
    threshold: float,
    *,
    clip: tuple[float, float] | None = (0.05, 0.95),
) -> float:
    return _probability(snapshot.member_lows, predicate=lambda value: value >= threshold, clip=clip)


def probability_low_below(
    snapshot: EnsembleForecastSnapshot,
    threshold: float,
    *,
    clip: tuple[float, float] | None = (0.05, 0.95),
) -> float:
    return _probability(snapshot.member_lows, predicate=lambda value: value <= threshold, clip=clip)


def _parse_iso_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _float_at(series: Any, index: int) -> float | None:
    if not isinstance(series, list) or index >= len(series):
        return None
    try:
        return float(series[index])
    except (TypeError, ValueError):
        return None


def _extract_unit(daily_units: dict[str, Any]) -> str:
    for key in ("temperature_2m_max", "temperature_2m_min"):
        raw = str(daily_units.get(key) or "").strip()
        if not raw:
            continue
        letters = "".join(ch for ch in raw if ch.isalpha())
        if letters:
            return letters.upper()
    return ""


def parse_open_meteo_daily_payload(
    payload: dict[str, Any],
    *,
    target_date: date,
    source: str,
    model_name: str,
) -> EnsembleForecastSnapshot | None:
    daily = payload.get("daily")
    daily_units = payload.get("daily_units")
    if not isinstance(daily, dict) or not isinstance(daily_units, dict):
        return None

    times = daily.get("time")
    if not isinstance(times, list):
        return None

    index: int | None = None
    for idx, raw_time in enumerate(times):
        parsed = _parse_iso_date(raw_time)
        if parsed == target_date:
            index = idx
            break
    if index is None:
        return None

    ensemble_high = _float_at(daily.get("temperature_2m_max"), index)
    ensemble_low = _float_at(daily.get("temperature_2m_min"), index)

    max_member_keys: dict[str, str] = {}
    min_member_keys: dict[str, str] = {}
    for key in daily:
        match = _MAX_MEMBER_RE.match(key)
        if match:
            max_member_keys[match.group("member")] = key
            continue
        match = _MIN_MEMBER_RE.match(key)
        if match:
            min_member_keys[match.group("member")] = key

    member_highs: list[float] = []
    member_lows: list[float] = []
    for member_id in sorted(set(max_member_keys) & set(min_member_keys)):
        high = _float_at(daily.get(max_member_keys[member_id]), index)
        low = _float_at(daily.get(min_member_keys[member_id]), index)
        if high is None or low is None:
            continue
        member_highs.append(high)
        member_lows.append(low)

    if not member_highs or not member_lows:
        return None

    generation_time = payload.get("generationtime_ms")
    try:
        generation_time_ms = float(generation_time) if generation_time is not None else None
    except (TypeError, ValueError):
        generation_time_ms = None

    try:
        latitude = float(payload.get("latitude"))
        longitude = float(payload.get("longitude"))
    except (TypeError, ValueError):
        return None

    timezone = str(payload.get("timezone") or "").strip()
    return EnsembleForecastSnapshot(
        source=source,
        model_name=model_name,
        target_date=target_date,
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        temperature_unit=_extract_unit(daily_units),
        ensemble_high=ensemble_high,
        ensemble_low=ensemble_low,
        member_highs=tuple(member_highs),
        member_lows=tuple(member_lows),
        generation_time_ms=generation_time_ms,
    )


class OpenMeteoEnsembleForecastClient:
    def __init__(self, *, config: OpenMeteoEnsembleForecastConfig):
        self.config = config
        self._cache: dict[tuple[float, float, date, str, str, str], tuple[float, EnsembleForecastSnapshot | None]] = {}

    def _cache_key(
        self,
        *,
        latitude: float,
        longitude: float,
        target_date: date,
    ) -> tuple[float, float, date, str, str, str]:
        return (
            float(latitude),
            float(longitude),
            target_date,
            self.config.model_name,
            self.config.temperature_unit,
            self.config.timezone,
        )

    def _forecast_days(self, target_date: date) -> int:
        utc_today = datetime.now(UTC).date()
        days = (target_date - utc_today).days + 1
        return max(1, min(self.config.max_forecast_days, days))

    async def fetch_snapshot(
        self,
        *,
        http_client: Any,
        latitude: float,
        longitude: float,
        target_date: date,
    ) -> EnsembleForecastSnapshot | None:
        key = self._cache_key(latitude=latitude, longitude=longitude, target_date=target_date)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self.config.cache_ttl_seconds:
            return cached[1]

        params = {
            "latitude": f"{float(latitude):.4f}",
            "longitude": f"{float(longitude):.4f}",
            "models": self.config.model_name,
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": self.config.temperature_unit,
            "timezone": self.config.timezone,
            "forecast_days": str(self._forecast_days(target_date)),
            "past_days": "0",
        }

        try:
            response = await http_client.get(
                self.config.base_url,
                params=params,
                timeout=int(self.config.timeout_seconds),
                headers={"User-Agent": "NautilusWeatherEnsemble/1.0"},
            )
            status = int(getattr(response, "status", getattr(response, "status_code", 0)))
            if status >= 400:
                snapshot = None
            else:
                payload = response.json() if hasattr(response, "json") else None
                snapshot = (
                    parse_open_meteo_daily_payload(
                        payload,
                        target_date=target_date,
                        source="open_meteo_ensemble",
                        model_name=self.config.model_name,
                    )
                    if isinstance(payload, dict)
                    else None
                )
        except Exception:
            snapshot = None

        self._cache[key] = (now, snapshot)
        return snapshot
