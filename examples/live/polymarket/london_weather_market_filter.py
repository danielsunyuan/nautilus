"""London high-temperature market filtering for Polymarket paper discovery."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from typing import Any

from examples.live.polymarket.weather_daily_temperature_resolver import (
    DailyTemperatureMarket,
)
from examples.live.polymarket.weather_daily_temperature_resolver import (
    filter_tradeable_daily_temperature_markets,
)
from examples.live.polymarket.weather_daily_temperature_resolver import (
    resolve_daily_temperature_markets,
)


SUPPORTED_BAND_TYPES = frozenset({"or_higher", "or_lower"})


@dataclass(frozen=True, slots=True)
class LondonWeatherMarketRejection:
    market: DailyTemperatureMarket
    reason: str


@dataclass(frozen=True, slots=True)
class LondonWeatherMarketFilterResult:
    accepted: list[DailyTemperatureMarket]
    rejected: list[LondonWeatherMarketRejection]


def london_weather_market_rejection_reason(
    market: DailyTemperatureMarket,
) -> str | None:
    """Return the reason a market is not eligible, or None when eligible."""
    if market.city != "London":
        return "unsupported_city"
    if market.metric != "high":
        return "unsupported_metric"
    if market.band_type == "exact":
        return "unsupported_exact_bucket"
    if market.band_type not in SUPPORTED_BAND_TYPES:
        return "unsupported_band_type"
    if not market.active:
        return "inactive"
    if not market.accepting_orders:
        return "not_accepting_orders"
    if not market.condition_id:
        return "missing_condition_id"
    if not market.yes_token_id:
        return "missing_yes_token_id"
    if not market.no_token_id:
        return "missing_no_token_id"
    return None


def filter_london_weather_markets(
    markets: list[DailyTemperatureMarket],
) -> LondonWeatherMarketFilterResult:
    """Split markets into eligible London high-temperature markets and diagnostics."""
    accepted: list[DailyTemperatureMarket] = []
    rejected: list[LondonWeatherMarketRejection] = []

    for market in markets:
        reason = london_weather_market_rejection_reason(market)
        if reason is None:
            accepted.append(market)
        else:
            rejected.append(LondonWeatherMarketRejection(market=market, reason=reason))

    return LondonWeatherMarketFilterResult(
        accepted=sorted(accepted, key=_market_sort_key),
        rejected=sorted(rejected, key=lambda item: _market_sort_key(item.market)),
    )


async def resolve_tradeable_london_weather_markets(
    *,
    http_client: Any,
    gamma_base_url: str,
    today: date,
    timeout_seconds: float = 15.0,
    resolve_markets: Callable[..., Awaitable[list[DailyTemperatureMarket]]] | None = None,
) -> LondonWeatherMarketFilterResult:
    """Resolve daily temperature markets, apply tradeable filtering, then narrow to London."""
    resolver = resolve_markets or resolve_daily_temperature_markets
    markets = await resolver(
        http_client=http_client,
        gamma_base_url=gamma_base_url,
        timeout_seconds=timeout_seconds,
    )
    tradeable_markets = filter_tradeable_daily_temperature_markets(markets, today)
    tradeable_result = filter_london_weather_markets(tradeable_markets)

    tradeable_ids = {id(market) for market in tradeable_markets}
    near_term_exact_rejections = [
        LondonWeatherMarketRejection(market=market, reason=reason)
        for market in markets
        if id(market) not in tradeable_ids
        if _is_near_term(market, today)
        if (reason := london_weather_market_rejection_reason(market)) is not None
    ]
    return LondonWeatherMarketFilterResult(
        accepted=tradeable_result.accepted,
        rejected=sorted(
            [*tradeable_result.rejected, *near_term_exact_rejections],
            key=lambda item: _market_sort_key(item.market),
        ),
    )


def _market_sort_key(market: DailyTemperatureMarket) -> tuple[date, int, float, str, str]:
    return (
        market.observation_date,
        _band_type_priority(market.band_type),
        market.threshold_f,
        market.city,
        market.slug,
    )


def _is_near_term(market: DailyTemperatureMarket, today: date) -> bool:
    return market.observation_date in (today, today + timedelta(days=1))


def _band_type_priority(band_type: str) -> int:
    if band_type == "or_higher":
        return 0
    if band_type == "or_lower":
        return 1
    return 2
