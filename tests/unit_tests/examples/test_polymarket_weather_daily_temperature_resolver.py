from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
import sys


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


# ---------------------------------------------------------------------------
# Static fixtures
# ---------------------------------------------------------------------------

ACTIVE_TEMP_MARKET_FIXTURE = {
    "slug": "will-the-high-temperature-in-new-york-city-on-april-15-2026-be-70f-or-above",
    "condition_id": "0xabc123def456",
    "question": "Will the high temperature in New York City on April 15, 2026 be 70\u00b0F or above?",
    "tokens": [
        {"token_id": "111222333", "outcome": "Yes"},
        {"token_id": "444555666", "outcome": "No"},
    ],
    "active": True,
    "accepting_orders": True,
    "closed": False,
    "tags": ["weather", "temperature"],
}

CLOSED_TEMP_MARKET_FIXTURE = {
    "slug": "will-the-high-temperature-in-chicago-on-april-10-2026-be-65f-or-above",
    "condition_id": "0xdef789abc012",
    "question": "Will the high temperature in Chicago on April 10, 2026 be 65\u00b0F or above?",
    "tokens": [
        {"token_id": "777888999", "outcome": "Yes"},
        {"token_id": "000111222", "outcome": "No"},
    ],
    "active": False,
    "accepting_orders": False,
    "closed": True,
    "tags": ["weather", "temperature"],
}

AMBIGUOUS_MARKET_FIXTURE = {
    "slug": "will-it-rain-in-miami-on-april-15-2026",
    "condition_id": "0x999888777",
    "question": "Will it rain in Miami on April 15, 2026?",
    "tokens": [
        {"token_id": "aaa111", "outcome": "Yes"},
        {"token_id": "bbb222", "outcome": "No"},
    ],
    "active": True,
    "accepting_orders": True,
    "closed": False,
    "tags": ["weather"],
}

NON_WEATHER_MARKET_FIXTURE = {
    "slug": "will-btc-be-above-100k",
    "condition_id": "0x555444333",
    "question": "Will Bitcoin be above $100,000 on April 15, 2026?",
    "tokens": [
        {"token_id": "ccc333", "outcome": "Yes"},
        {"token_id": "ddd444", "outcome": "No"},
    ],
    "active": True,
    "accepting_orders": True,
    "closed": False,
    "tags": ["crypto"],
}

LOW_TEMP_MARKET_FIXTURE = {
    "slug": "will-the-low-temperature-in-chicago-on-april-16-2026-be-32f-or-below",
    "condition_id": "0xlow123",
    "question": "Will the low temperature in Chicago on April 16, 2026 be 32\u00b0F or below?",
    "tokens": [
        {"token_id": "low111", "outcome": "Yes"},
        {"token_id": "low222", "outcome": "No"},
    ],
    "active": True,
    "accepting_orders": True,
    "closed": False,
    "tags": ["weather", "temperature"],
}


# ---------------------------------------------------------------------------
# Test: active market with clear city/date/threshold parsed correctly
# ---------------------------------------------------------------------------


def test_parse_active_high_temp_market():
    result = resolver.parse_daily_temperature_market(ACTIVE_TEMP_MARKET_FIXTURE)
    assert result is not None
    assert result.slug == "will-the-high-temperature-in-new-york-city-on-april-15-2026-be-70f-or-above"
    assert result.condition_id == "0xabc123def456"
    assert result.city == "New York City"
    assert result.observation_date == date(2026, 4, 15)
    assert result.metric == "high"
    assert result.threshold_f == 70.0
    assert result.yes_token_id == "111222333"
    assert result.no_token_id == "444555666"
    assert result.active is True
    assert result.accepting_orders is True


def test_parse_active_low_temp_market():
    result = resolver.parse_daily_temperature_market(LOW_TEMP_MARKET_FIXTURE)
    assert result is not None
    assert result.city == "Chicago"
    assert result.observation_date == date(2026, 4, 16)
    assert result.metric == "low"
    assert result.threshold_f == 32.0
    assert result.yes_token_id == "low111"
    assert result.no_token_id == "low222"
    assert result.active is True
    assert result.accepting_orders is True


# ---------------------------------------------------------------------------
# Test: closed market accepted for settlement updates (not rejected)
# ---------------------------------------------------------------------------


def test_parse_closed_market_accepted_for_settlement():
    result = resolver.parse_daily_temperature_market(CLOSED_TEMP_MARKET_FIXTURE)
    assert result is not None
    assert result.city == "Chicago"
    assert result.observation_date == date(2026, 4, 10)
    assert result.metric == "high"
    assert result.threshold_f == 65.0
    assert result.active is False
    assert result.accepting_orders is False


# ---------------------------------------------------------------------------
# Test: ambiguous market rejected (non-temperature weather market)
# ---------------------------------------------------------------------------


def test_ambiguous_weather_market_rejected():
    result = resolver.parse_daily_temperature_market(AMBIGUOUS_MARKET_FIXTURE)
    assert result is None


# ---------------------------------------------------------------------------
# Test: non-temperature weather market ignored
# ---------------------------------------------------------------------------


def test_non_weather_market_ignored():
    result = resolver.parse_daily_temperature_market(NON_WEATHER_MARKET_FIXTURE)
    assert result is None


# ---------------------------------------------------------------------------
# Test: missing tokens rejected
# ---------------------------------------------------------------------------


def test_market_with_missing_tokens_rejected():
    broken = {**ACTIVE_TEMP_MARKET_FIXTURE, "tokens": []}
    result = resolver.parse_daily_temperature_market(broken)
    assert result is None


def test_market_with_no_question_rejected():
    broken = {**ACTIVE_TEMP_MARKET_FIXTURE, "question": ""}
    result = resolver.parse_daily_temperature_market(broken)
    assert result is None


# ---------------------------------------------------------------------------
# Tests: Actual Polymarket question formats (v2 patterns)
# ---------------------------------------------------------------------------


POLYMARKET_EXACT_CELSIUS = {
    "slug": "will-the-highest-temperature-in-london-be-13c-on-april-14",
    "condition_id": "0xpm_london_14",
    "question": "Will the highest temperature in London be 13\u00b0C on April 14?",
    "tokens": [
        {"token_id": "pm_lon_yes", "outcome": "Yes"},
        {"token_id": "pm_lon_no", "outcome": "No"},
    ],
    "active": True,
    "accepting_orders": True,
    "closed": False,
}

POLYMARKET_OR_HIGHER = {
    "slug": "will-the-highest-temperature-in-london-be-19c-or-higher-on-april-15",
    "condition_id": "0xpm_london_15h",
    "question": "Will the highest temperature in London be 19\u00b0C or higher on April 15?",
    "tokens": [
        {"token_id": "pm_lon2_yes", "outcome": "Yes"},
        {"token_id": "pm_lon2_no", "outcome": "No"},
    ],
    "active": True,
    "accepting_orders": True,
    "closed": False,
}

POLYMARKET_OR_BELOW = {
    "slug": "will-the-highest-temperature-in-madrid-be-15c-or-below-on-april-14",
    "condition_id": "0xpm_madrid_14",
    "question": "Will the highest temperature in Madrid be 15\u00b0C or below on April 14?",
    "tokens": [
        {"token_id": "pm_mad_yes", "outcome": "Yes"},
        {"token_id": "pm_mad_no", "outcome": "No"},
    ],
    "active": True,
    "accepting_orders": True,
    "closed": False,
}

POLYMARKET_RANGE_FAHRENHEIT = {
    "slug": "will-the-highest-temperature-in-seattle-be-between-44-45f-on-april-15",
    "condition_id": "0xpm_seattle_15",
    "question": "Will the highest temperature in Seattle be between 44-45\u00b0F on April 15?",
    "tokens": [
        {"token_id": "pm_sea_yes", "outcome": "Yes"},
        {"token_id": "pm_sea_no", "outcome": "No"},
    ],
    "active": True,
    "accepting_orders": True,
    "closed": False,
}

POLYMARKET_WITH_YEAR = {
    "slug": "will-the-highest-temperature-in-tokyo-be-20c-on-april-16-2026",
    "condition_id": "0xpm_tokyo_16",
    "question": "Will the highest temperature in Tokyo be 20\u00b0C on April 16, 2026?",
    "tokens": [
        {"token_id": "pm_tok_yes", "outcome": "Yes"},
        {"token_id": "pm_tok_no", "outcome": "No"},
    ],
    "active": True,
    "accepting_orders": True,
    "closed": False,
}


def test_parse_polymarket_exact_celsius():
    result = resolver.parse_daily_temperature_market(POLYMARKET_EXACT_CELSIUS)
    assert result is not None
    assert result.city == "London"
    assert result.metric == "high"
    assert result.threshold_f == 13.0
    assert result.observation_date.month == 4
    assert result.observation_date.day == 14


def test_parse_polymarket_or_higher():
    result = resolver.parse_daily_temperature_market(POLYMARKET_OR_HIGHER)
    assert result is not None
    assert result.city == "London"
    assert result.threshold_f == 19.0


def test_parse_polymarket_or_below():
    result = resolver.parse_daily_temperature_market(POLYMARKET_OR_BELOW)
    assert result is not None
    assert result.city == "Madrid"
    assert result.threshold_f == 15.0


def test_parse_polymarket_range_fahrenheit():
    result = resolver.parse_daily_temperature_market(POLYMARKET_RANGE_FAHRENHEIT)
    assert result is not None
    assert result.city == "Seattle"
    assert result.threshold_f == 44.0  # lower bound of range


def test_parse_polymarket_with_year():
    result = resolver.parse_daily_temperature_market(POLYMARKET_WITH_YEAR)
    assert result is not None
    assert result.city == "Tokyo"
    assert result.observation_date == date(2026, 4, 16)
    assert result.threshold_f == 20.0
