"""
Pure signal evaluator for Polymarket weather temperature markets.

Three confirmed entry strategies (A1/A2/B2) based on daily high temperature
observations from Weather Underground. No network calls — fully unit-testable.
"""

from __future__ import annotations

import logging
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from examples.live.polymarket.weather_daily_temperature_resolver import (
        DailyTemperatureMarket,
    )


StrategyId = Literal["A1", "A2", "B2"]

# Safety margins: if daily_max is within this buffer of the threshold,
# we don't consider it confirmed.
SAFETY_MARGIN_C = 0.5  # °C
SAFETY_MARGIN_F = 1.0  # °F

# B2: temperature must be at least this far below threshold after 15:00 local
B2_GAP_C = 5.0
B2_GAP_F = 9.0  # 5°C ≈ 9°F

# Maximum acceptable CLOB price for each strategy (above this, edge too thin)
MAX_ENTRY_PRICE: dict[StrategyId, float] = {
    "A1": 0.97,
    "A2": 0.96,
    "B2": 0.93,
}

# Minimum confirmation polls before entering A1/A2
MIN_CONFIRM_POLLS = 2

# Data freshness: skip if as_of_utc is older than this (seconds)
MAX_DATA_AGE_SECS = 90 * 60  # 90 minutes

# Spike filter: skip if daily_max jumped more than this in one poll cycle
MAX_POLL_JUMP_C = 4.0
MAX_POLL_JUMP_F = 7.0

# Cities that use HKO oracle — intraday data is proxy only, skip A1/A2
HKO_CITIES: frozenset[str] = frozenset({"Hong Kong"})


@dataclass(frozen=True, slots=True)
class ConfirmedSignal:
    """A trade signal produced by comparing WU daily_max to a market threshold."""

    strategy: StrategyId
    market_slug: str
    city: str
    observation_date: str
    threshold_f: float  # threshold value (unit matches market unit)
    unit: str  # "F" or "C"
    token_id: str
    token_side: str  # "yes" or "no"
    max_entry_price: float
    preset_name: str
    arena: str
    stop_loss_price: float
    take_profit_price: float
    wu_daily_max: float
    wu_as_of_utc: str  # ISO8601


@dataclass
class ConfirmTracker:
    """
    Tracks consecutive poll confirmations per (slug, strategy) key.

    On each poll, call `record(slug, strategy, confirmed)`.
    Returns the current consecutive-confirmation count.
    """

    _counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def record(self, slug: str, strategy: str, confirmed: bool) -> int:
        """Record confirmation result; return current count."""
        key = (slug, strategy)
        if confirmed:
            self._counts[key] = self._counts.get(key, 0) + 1
        else:
            self._counts.pop(key, None)
        return self._counts.get(key, 0)

    def get(self, slug: str, strategy: str) -> int:
        """Get current confirmation count without recording."""
        return self._counts.get((slug, strategy), 0)

    def clear_slug(self, slug: str) -> None:
        """Remove all confirmation counts for a slug (after entry)."""
        for key in list(self._counts):
            if key[0] == slug:
                del self._counts[key]


def _safety_margin(unit: str) -> float:
    """Return safety margin based on unit."""
    return SAFETY_MARGIN_F if unit == "F" else SAFETY_MARGIN_C


def _b2_gap(unit: str) -> float:
    """Return B2 gap threshold based on unit."""
    return B2_GAP_F if unit == "F" else B2_GAP_C


def _is_data_fresh(as_of_utc: datetime, now: datetime) -> bool:
    """Check if observation is within MAX_DATA_AGE_SECS."""
    age_secs = (now - as_of_utc).total_seconds()
    return age_secs <= MAX_DATA_AGE_SECS


def _spike_detected(
    daily_max: float, prev_daily_max: float | None, unit: str
) -> bool:
    """Detect if daily_max jumped too much since previous poll."""
    if prev_daily_max is None:
        return False
    jump = daily_max - prev_daily_max
    limit = MAX_POLL_JUMP_F if unit == "F" else MAX_POLL_JUMP_C
    return jump > limit


def evaluate_a1(
    *,
    daily_max: float,
    threshold: float,
    unit: str,
    confirm_count: int,
) -> bool:
    """A1: or_higher market — is YES outcome confirmed?"""
    margin = _safety_margin(unit)
    return daily_max >= threshold + margin and confirm_count >= MIN_CONFIRM_POLLS


def evaluate_a2(
    *,
    daily_max: float,
    threshold: float,
    unit: str,
    confirm_count: int,
) -> bool:
    """A2: exact band market — has daily_max exceeded the upper bound (threshold+1)?"""
    band_upper = threshold + 1.0
    margin = _safety_margin(unit)
    return (
        daily_max > band_upper + margin and confirm_count >= MIN_CONFIRM_POLLS
    )


def evaluate_b2(
    *,
    daily_max: float,
    threshold: float,
    unit: str,
    local_hour: int,
) -> bool:
    """B2: or_higher market — is temperature too far below threshold after 15:00?"""
    gap = _b2_gap(unit)
    return daily_max < threshold - gap and local_hour >= 15


def build_signal(
    market: DailyTemperatureMarket,
    daily_max: float,
    unit: str,
    as_of_utc: datetime,
    confirm_counts: dict[str, int],
    prev_daily_max: float | None,
    now: datetime,
    city_tz: str,
) -> ConfirmedSignal | None:
    """
    Evaluate all enabled strategies for one (market, obs) pair.
    Returns the first matching signal, or None.

    ``confirm_counts`` must map strategy name → consecutive-poll count, e.g.
    ``{"A1": 2, "A2": 0}``. A1 and A2 are tracked independently.

    Strategy evaluation order: A1 → A2 → B2
    (confirmed strategies take priority over probabilistic)
    """
    # Data quality gates
    if not _is_data_fresh(as_of_utc, now):
        return None
    if _spike_detected(daily_max, prev_daily_max, unit):
        return None

    slug = market.slug
    obs_date = str(market.observation_date)
    threshold = market.threshold_f

    # A1: or_higher confirmed YES
    if market.band_type == "or_higher" and market.city not in HKO_CITIES:
        if evaluate_a1(
            daily_max=daily_max,
            threshold=threshold,
            unit=unit,
            confirm_count=confirm_counts.get("A1", 0),
        ):
            return ConfirmedSignal(
                strategy="A1",
                market_slug=slug,
                city=market.city,
                observation_date=obs_date,
                threshold_f=threshold,
                unit=unit,
                token_id=market.yes_token_id,
                token_side="yes",
                max_entry_price=MAX_ENTRY_PRICE["A1"],
                preset_name="temp_confirmed_a1",
                arena="temp_confirmed",
                stop_loss_price=0.85,
                take_profit_price=0.99,
                wu_daily_max=daily_max,
                wu_as_of_utc=as_of_utc.isoformat(),
            )

    # A2: exact band confirmed NO (exceeded upper bound)
    if market.band_type == "exact" and market.city not in HKO_CITIES:
        if evaluate_a2(
            daily_max=daily_max,
            threshold=threshold,
            unit=unit,
            confirm_count=confirm_counts.get("A2", 0),
        ):
            return ConfirmedSignal(
                strategy="A2",
                market_slug=slug,
                city=market.city,
                observation_date=obs_date,
                threshold_f=threshold,
                unit=unit,
                token_id=market.no_token_id,
                token_side="no",
                max_entry_price=MAX_ENTRY_PRICE["A2"],
                preset_name="temp_confirmed_a2",
                arena="temp_confirmed",
                stop_loss_price=0.85,
                take_profit_price=0.99,
                wu_daily_max=daily_max,
                wu_as_of_utc=as_of_utc.isoformat(),
            )

    # B2: or_higher probabilistic NO (too far below after 15:00 local)
    if market.band_type == "or_higher":
        try:
            tz = zoneinfo.ZoneInfo(city_tz)
            local_hour = now.astimezone(tz).hour
        except Exception as _tz_exc:
            _log.warning("ZoneInfo failed for %r, using UTC hour: %s", city_tz, _tz_exc)
            local_hour = now.hour  # fallback to UTC
        if evaluate_b2(
            daily_max=daily_max,
            threshold=threshold,
            unit=unit,
            local_hour=local_hour,
        ):
            return ConfirmedSignal(
                strategy="B2",
                market_slug=slug,
                city=market.city,
                observation_date=obs_date,
                threshold_f=threshold,
                unit=unit,
                token_id=market.no_token_id,
                token_side="no",
                max_entry_price=MAX_ENTRY_PRICE["B2"],
                preset_name="temp_confirmed_b2",
                arena="temp_confirmed",
                stop_loss_price=0.85,
                take_profit_price=0.99,
                wu_daily_max=daily_max,
                wu_as_of_utc=as_of_utc.isoformat(),
            )

    return None
