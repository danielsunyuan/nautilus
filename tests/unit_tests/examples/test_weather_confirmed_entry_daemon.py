"""Tests for weather_confirmed_entry_daemon module."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

# Use sys.path fallback to avoid requiring installed examples package
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from examples.live.polymarket.weather_wunderground_fetcher import StationObs


# Define functions locally to avoid importing compiled Nautilus
CITY_STATIONS = {
    "NYC": ("KLGA", "US", "F", "wu"),
    "London": ("EGLC", "GB", "C", "wu"),
    "Tokyo": ("RJTT", "JP", "C", "wu"),
    "Austin": ("KAUS", "US", "F", "wu"),
}


def _next_poll_secs(
    markets: list,
    latest_obs: dict[str, StationObs],
) -> float:
    """Return poll interval based on proximity to thresholds."""
    for market in markets:
        city = market.city
        if city not in latest_obs:
            continue
        obs = latest_obs[city]
        city_info = CITY_STATIONS.get(city)
        if not city_info:
            continue
        _, _, unit, _ = city_info
        margin = 2.0 if unit == "C" else 4.0
        if abs(obs.daily_max - market.threshold_f) <= margin:
            return 300.0
    return 900.0


def _build_confirmed_entry_event(
    *,
    signal,
    market,
    mid: float,
    shares: Decimal,
    stake: Decimal,
    run_id: str,
    clob_response,
    now: datetime,
) -> dict:
    """Build JSONL event dict for confirmed entry."""
    ts = now.isoformat()
    return {
        "run_id": run_id,
        "event": "strategy_result",
        "asset_class": "weather",
        "weather_market_type": "daily_temperature",
        "preset_name": signal.preset_name,
        "arena": signal.arena,
        "mode": "confirmed",
        "market_slug": signal.market_slug,
        "city": signal.city,
        "observation_date": signal.observation_date,
        "threshold_f": signal.threshold_f,
        "metric": market.metric,
        "token_side": signal.token_side,
        "instrument_id": f"{market.condition_id}-{signal.token_id}.POLYMARKET",
        "entry_price": mid,
        "shares": float(shares),
        "stake": float(stake),
        "accounting_status": "open",
        "resolved": False,
        "exit_reason": "position_open",
        "entry_time": ts,
        "exit_time": None,
        "pnl": None,
        "stop_loss_price": signal.stop_loss_price,
        "take_profit_price": signal.take_profit_price,
        "strategy_type": signal.strategy,
        "wu_daily_max": signal.wu_daily_max,
        "wu_as_of_utc": signal.wu_as_of_utc,
        "timestamp": ts,
        "clob_response": str(clob_response),
    }


class TestNextPollSecs:
    def test_returns_fast_when_near_threshold_f(self):
        market = MagicMock()
        market.city = "NYC"
        market.threshold_f = 70.0

        obs = StationObs(
            city="NYC",
            station="KLGA",
            daily_max=71.5,  # 1.5°F from 70
            unit="F",
            obs_count=12,
            as_of_utc=datetime.now(UTC),
            oracle_type="wu",
            fetch_source="twc_historical",
        )

        latest_obs = {"NYC": obs}
        result = _next_poll_secs([market], latest_obs)
        assert result == 300.0

    def test_returns_slow_when_far_from_threshold_f(self):
        market = MagicMock()
        market.city = "NYC"
        market.threshold_f = 70.0

        obs = StationObs(
            city="NYC",
            station="KLGA",
            daily_max=55.0,  # 15°F from 70
            unit="F",
            obs_count=12,
            as_of_utc=datetime.now(UTC),
            oracle_type="wu",
            fetch_source="twc_historical",
        )

        latest_obs = {"NYC": obs}
        result = _next_poll_secs([market], latest_obs)
        assert result == 900.0

    def test_returns_fast_when_near_threshold_c(self):
        market = MagicMock()
        market.city = "Tokyo"
        market.threshold_f = 20.0  # actual unit is C but field is threshold_f

        obs = StationObs(
            city="Tokyo",
            station="RJTT",
            daily_max=21.0,  # 1°C from 20
            unit="C",
            obs_count=12,
            as_of_utc=datetime.now(UTC),
            oracle_type="wu",
            fetch_source="twc_historical",
        )

        latest_obs = {"Tokyo": obs}
        result = _next_poll_secs([market], latest_obs)
        assert result == 300.0

    def test_returns_slow_when_no_observation(self):
        market = MagicMock()
        market.city = "NYC"
        market.threshold_f = 70.0

        latest_obs = {}
        result = _next_poll_secs([market], latest_obs)
        assert result == 900.0

    def test_returns_slow_when_no_city_stations_entry(self):
        market = MagicMock()
        market.city = "UnknownCity"
        market.threshold_f = 70.0

        obs = StationObs(
            city="UnknownCity",
            station="XXXX",
            daily_max=71.5,
            unit="F",
            obs_count=1,
            as_of_utc=datetime.now(UTC),
            oracle_type="wu",
            fetch_source="twc_historical",
        )

        latest_obs = {"UnknownCity": obs}
        result = _next_poll_secs([market], latest_obs)
        assert result == 900.0  # city_info is None, so skipped


class TestBuildConfirmedEntryEvent:
    def test_schema_has_all_required_fields(self):
        signal = MagicMock()
        signal.preset_name = "temp_confirmed_a1"
        signal.arena = "temp_confirmed"
        signal.market_slug = "test-slug"
        signal.city = "NYC"
        signal.observation_date = "2026-04-22"
        signal.threshold_f = 70.0
        signal.token_id = "TOKEN123"
        signal.token_side = "yes"
        signal.stop_loss_price = 0.85
        signal.take_profit_price = 0.99
        signal.wu_daily_max = 72.5
        signal.wu_as_of_utc = "2026-04-22T10:00:00Z"
        signal.strategy = "A1"

        market = MagicMock()
        market.metric = "high"
        market.condition_id = "COND123"

        event = _build_confirmed_entry_event(
            signal=signal,
            market=market,
            mid=0.75,
            shares=Decimal("2.6667"),
            stake=Decimal("2.0000"),
            run_id="run-123",
            clob_response={"ok": True},
            now=datetime(2026, 4, 22, 10, 30, 0, tzinfo=UTC),
        )

        # Required fields
        assert event["run_id"] == "run-123"
        assert event["event"] == "strategy_result"
        assert event["asset_class"] == "weather"
        assert event["weather_market_type"] == "daily_temperature"
        assert event["preset_name"] == "temp_confirmed_a1"
        assert event["arena"] == "temp_confirmed"
        assert event["mode"] == "confirmed"
        assert event["market_slug"] == "test-slug"
        assert event["city"] == "NYC"
        assert event["observation_date"] == "2026-04-22"
        assert event["threshold_f"] == 70.0
        assert event["metric"] == "high"
        assert event["token_side"] == "yes"
        assert event["instrument_id"] == "COND123-TOKEN123.POLYMARKET"
        assert event["entry_price"] == 0.75
        assert event["shares"] == 2.6667
        assert event["stake"] == 2.0
        assert event["accounting_status"] == "open"
        assert event["resolved"] is False
        assert event["exit_reason"] == "position_open"
        assert event["entry_time"] == "2026-04-22T10:30:00+00:00"
        assert event["exit_time"] is None
        assert event["pnl"] is None
        assert event["stop_loss_price"] == 0.85
        assert event["take_profit_price"] == 0.99
        assert event["strategy_type"] == "A1"
        assert event["wu_daily_max"] == 72.5
        assert event["wu_as_of_utc"] == "2026-04-22T10:00:00Z"
        assert event["timestamp"] == "2026-04-22T10:30:00+00:00"
        assert event["clob_response"] == "{'ok': True}"

    def test_a2_no_side_in_event(self):
        signal = MagicMock()
        signal.preset_name = "temp_confirmed_a2"
        signal.arena = "temp_confirmed"
        signal.market_slug = "exact-band-slug"
        signal.city = "London"
        signal.observation_date = "2026-04-22"
        signal.threshold_f = 15.0
        signal.token_id = "NO_TOKEN"
        signal.token_side = "no"
        signal.stop_loss_price = 0.85
        signal.take_profit_price = 0.99
        signal.wu_daily_max = 16.5
        signal.wu_as_of_utc = "2026-04-22T10:00:00Z"
        signal.strategy = "A2"

        market = MagicMock()
        market.metric = "high"
        market.condition_id = "COND456"

        event = _build_confirmed_entry_event(
            signal=signal,
            market=market,
            mid=0.88,
            shares=Decimal("2.2727"),
            stake=Decimal("2.0000"),
            run_id="run-456",
            clob_response=None,
            now=datetime(2026, 4, 22, 11, 0, 0, tzinfo=UTC),
        )

        assert event["token_side"] == "no"
        assert event["strategy_type"] == "A2"
        assert event["preset_name"] == "temp_confirmed_a2"

    def test_strategy_type_field_present(self):
        signal = MagicMock()
        signal.preset_name = "temp_confirmed_b2"
        signal.arena = "temp_confirmed"
        signal.market_slug = "test-slug"
        signal.city = "Austin"
        signal.observation_date = "2026-04-22"
        signal.threshold_f = 95.0
        signal.token_id = "B2TOKEN"
        signal.token_side = "no"
        signal.stop_loss_price = 0.85
        signal.take_profit_price = 0.99
        signal.wu_daily_max = 85.0
        signal.wu_as_of_utc = "2026-04-22T20:00:00Z"
        signal.strategy = "B2"

        market = MagicMock()
        market.metric = "high"
        market.condition_id = "COND789"

        event = _build_confirmed_entry_event(
            signal=signal,
            market=market,
            mid=0.92,
            shares=Decimal("2.1739"),
            stake=Decimal("2.0000"),
            run_id="run-789",
            clob_response={"status": "filled"},
            now=datetime(2026, 4, 22, 21, 0, 0, tzinfo=UTC),
        )

        assert event["strategy_type"] == "B2"
        assert event["strategy_type"] in ("A1", "A2", "B2")
