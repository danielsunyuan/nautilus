from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


resolver = _load_module(
    "examples.live.polymarket.weather_daily_temperature_resolver",
    ROOT / "examples" / "live" / "polymarket" / "weather_daily_temperature_resolver.py",
)
market_filter = _load_module(
    "examples.live.polymarket.london_weather_market_filter",
    ROOT / "examples" / "live" / "polymarket" / "london_weather_market_filter.py",
)


def _market(
    *,
    slug: str = "london-high-20-or-higher",
    city: str = "London",
    metric: str = "high",
    band_type: str = "or_higher",
    active: bool = True,
    accepting_orders: bool = True,
    condition_id: str = "0xcondition",
    yes_token_id: str = "yes-token",
    no_token_id: str = "no-token",
    observation_date: date = date(2026, 6, 1),
    threshold_f: float = 20.0,
) -> resolver.DailyTemperatureMarket:
    return resolver.DailyTemperatureMarket(
        slug=slug,
        condition_id=condition_id,
        city=city,
        observation_date=observation_date,
        metric=metric,
        threshold_f=threshold_f,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        active=active,
        accepting_orders=accepting_orders,
        band_type=band_type,
    )


def test_london_high_or_band_market_is_accepted() -> None:
    market = _market()
    result = market_filter.filter_london_weather_markets([market])

    assert result.accepted == [market]
    assert result.rejected == []
    assert market_filter.london_weather_market_rejection_reason(market) is None


@pytest.mark.parametrize(
    ("market", "reason"),
    [
        (_market(city="Madrid"), "unsupported_city"),
        (_market(metric="low"), "unsupported_metric"),
        (_market(band_type="exact"), "unsupported_exact_bucket"),
        (_market(active=False), "inactive"),
        (_market(accepting_orders=False), "not_accepting_orders"),
        (_market(condition_id=""), "missing_condition_id"),
        (_market(yes_token_id=""), "missing_yes_token_id"),
        (_market(no_token_id=""), "missing_no_token_id"),
    ],
)
def test_rejects_ineligible_markets_with_diagnostic_reason(
    market: resolver.DailyTemperatureMarket,
    reason: str,
) -> None:
    result = market_filter.filter_london_weather_markets([market])

    assert result.accepted == []
    assert len(result.rejected) == 1
    assert result.rejected[0].market == market
    assert result.rejected[0].reason == reason
    assert market_filter.london_weather_market_rejection_reason(market) == reason


def test_accepts_or_lower_london_high_market() -> None:
    market = _market(band_type="or_lower")

    result = market_filter.filter_london_weather_markets([market])

    assert result.accepted == [market]


def test_filter_order_is_deterministic() -> None:
    later = _market(slug="later", observation_date=date(2026, 6, 2), threshold_f=18.0)
    lower_band = _market(slug="lower-band", band_type="or_lower", threshold_f=17.0)
    higher_19 = _market(slug="higher-19", threshold_f=19.0)
    higher_18 = _market(slug="higher-18", threshold_f=18.0)
    rejected_late = _market(
        slug="rejected-late",
        city="Paris",
        observation_date=date(2026, 6, 3),
    )
    rejected_early = _market(
        slug="rejected-early",
        city="Paris",
        observation_date=date(2026, 5, 31),
    )

    result = market_filter.filter_london_weather_markets(
        [later, rejected_late, lower_band, higher_19, rejected_early, higher_18],
    )

    assert [market.slug for market in result.accepted] == [
        "higher-18",
        "higher-19",
        "lower-band",
        "later",
    ]
    assert [diagnostic.market.slug for diagnostic in result.rejected] == [
        "rejected-early",
        "rejected-late",
    ]


@pytest.mark.asyncio
async def test_async_wrapper_reuses_resolver_and_tradeable_filter_without_network() -> None:
    accepted = _market(slug="accepted")
    exact = _market(slug="exact", band_type="exact")
    tomorrow = _market(slug="tomorrow", observation_date=date(2026, 6, 2))
    far = _market(slug="far", observation_date=date(2026, 6, 3))

    async def resolve_markets(**kwargs):
        assert kwargs["http_client"] == "client"
        assert kwargs["gamma_base_url"] == "https://gamma.test"
        assert kwargs["timeout_seconds"] == 3.0
        return [far, exact, tomorrow, accepted]

    result = await market_filter.resolve_tradeable_london_weather_markets(
        http_client="client",
        gamma_base_url="https://gamma.test",
        today=date(2026, 6, 1),
        timeout_seconds=3.0,
        resolve_markets=resolve_markets,
    )

    assert [market.slug for market in result.accepted] == ["accepted", "tomorrow"]
    assert [(item.market.slug, item.reason) for item in result.rejected] == [
        ("exact", "unsupported_exact_bucket"),
    ]
