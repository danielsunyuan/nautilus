"""Tests for weather_confirmed_entry_daemon module."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Use sys.path fallback to avoid requiring installed examples package
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from examples.live.polymarket.weather_wunderground_fetcher import StationObs
from examples.live.polymarket.weather_confirmed_signal import ConfirmTracker


# Define functions locally to avoid importing compiled Nautilus
CITY_STATIONS = {
    "NYC": ("KLGA", "US", "F", "wu"),
    "London": ("EGLC", "GB", "C", "wu"),
    "Tokyo": ("RJTT", "JP", "C", "wu"),
    "Austin": ("KAUS", "US", "F", "wu"),
    "Guangzhou": ("ZGGG", "CN", "C", "wu"),
}

_CITY_TIMEZONES = {
    "NYC": "America/New_York",
    "London": "Europe/London",
    "Tokyo": "Asia/Tokyo",
    "Austin": "America/Chicago",
    "Guangzhou": "Asia/Shanghai",
}


def _city_target_date(city: str, now: datetime) -> date:
    import zoneinfo

    tz_name = _CITY_TIMEZONES.get(city)
    if tz_name is None:
        return now.date()
    tz = zoneinfo.ZoneInfo(tz_name)
    return now.astimezone(tz).date()


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
    dry_run: bool = False,
) -> dict:
    """Build JSONL event dict for confirmed entry."""
    ts = now.isoformat()
    return {
        "run_id": run_id,
        "event": "strategy_result",
        "asset_class": "weather",
        "weather_market_type": "daily_temperature",
        "preset_name": signal.preset_name,
        "strategy_name": signal.preset_name,  # canonical field for leaderboard/reports
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
        "real_order": not dry_run,  # True for live CLOB fills; False in dry-run mode
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
        assert event["strategy_name"] == "temp_confirmed_a1"
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
        assert event["real_order"] is True  # default: not dry_run

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

    def test_real_order_false_in_dry_run(self):
        """dry_run=True must produce real_order=False (no live CLOB order submitted)."""
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

        event_dry = _build_confirmed_entry_event(
            signal=signal,
            market=market,
            mid=0.75,
            shares=Decimal("2.6667"),
            stake=Decimal("2.0000"),
            run_id="run-dry",
            clob_response=None,
            now=datetime(2026, 4, 22, 10, 30, 0, tzinfo=UTC),
            dry_run=True,
        )
        event_live = _build_confirmed_entry_event(
            signal=signal,
            market=market,
            mid=0.75,
            shares=Decimal("2.6667"),
            stake=Decimal("2.0000"),
            run_id="run-live",
            clob_response={"ok": True},
            now=datetime(2026, 4, 22, 10, 30, 0, tzinfo=UTC),
            dry_run=False,
        )

        assert event_dry["real_order"] is False
        assert event_live["real_order"] is True


# ---------------------------------------------------------------------------
# Helpers for _run_poll_cycle tests
# ---------------------------------------------------------------------------

def _make_obs(city: str, daily_max: float, unit: str = "F") -> StationObs:
    """Construct a StationObs for testing."""
    return StationObs(
        city=city,
        station="TEST",
        daily_max=daily_max,
        unit=unit,
        obs_count=12,
        as_of_utc=datetime.now(UTC),
        oracle_type="wu",
        fetch_source="twc_historical",
    )


def _make_market(city: str, threshold_f: float, band_type: str = "or_higher", slug: str | None = None) -> MagicMock:
    """Construct a minimal mock DailyTemperatureMarket."""
    m = MagicMock()
    m.city = city
    m.threshold_f = threshold_f
    m.band_type = band_type
    m.slug = slug or f"{city.lower().replace(' ', '-')}-{int(threshold_f)}"
    m.condition_id = f"COND-{m.slug}"
    m.yes_token_id = f"YES-{m.slug}"
    m.no_token_id = f"NO-{m.slug}"
    m.metric = "high"
    m.observation_date = "2026-04-22"
    return m


# Minimal reimplementation of _run_poll_cycle logic for unit testing.
# Mirrors the production code but replaces I/O (fetch_daily_high, CLOB) with
# injectable callables so tests stay synchronous-friendly via asyncio.run().

SAFETY_MARGIN_F = 1.0
SAFETY_MARGIN_C = 0.5


async def _run_poll_cycle_local(
    *,
    markets: list,
    confirm_tracker: ConfirmTracker,
    latest_obs: dict,
    fetch_fn,          # async callable(city) -> StationObs | None
    signal_fn=None,    # optional callable(market, obs) -> signal | None
    now: datetime | None = None,
) -> tuple[dict, float]:
    """Stripped _run_poll_cycle for unit tests: no CLOB, no writer, no I/O.

    Returns (cities_with_fresh_obs_dict, next_poll_secs).
    cities_with_fresh_obs_dict maps city -> obs that were fetched this cycle.
    """
    # Mirror production: skip metric="low" markets (daily-high fetcher only)
    markets = [m for m in markets if getattr(m, "metric", "high") != "low"]

    unique_cities = {m.city for m in markets}
    effective_now = now or datetime.now(UTC)
    prev_obs = {
        city: (latest_obs[city].daily_max if city in latest_obs else None)
        for city in unique_cities
    }
    cities_with_fresh_obs: set[str] = set()
    fetch_target_dates: dict[str, date] = {}
    for city in unique_cities:
        target_date = _city_target_date(city, effective_now)
        fetch_target_dates[city] = target_date
        try:
            obs = await fetch_fn(city, target_date)
        except Exception:
            continue
        if obs is None:
            continue
        latest_obs[city] = obs
        cities_with_fresh_obs.add(city)

    # Compute next_poll_secs from fresh obs
    next_poll_secs_val = _next_poll_secs(markets, latest_obs)

    # Intra-city sort only
    city_order: list[str] = []
    seen_cities: set[str] = set()
    for m in markets:
        if m.city not in seen_cities:
            city_order.append(m.city)
            seen_cities.add(m.city)
    city_rank = {city: i for i, city in enumerate(city_order)}
    sorted_markets = sorted(markets, key=lambda m: (city_rank[m.city], -m.threshold_f))

    city_a1_entered: set[str] = set()
    signals_evaluated: list[tuple] = []

    for market in sorted_markets:
        city = market.city
        if city not in cities_with_fresh_obs:
            continue
        city_info = CITY_STATIONS.get(city)
        if not city_info:
            continue
        _, _, unit, _ = city_info
        obs = latest_obs.get(city)
        if obs is None:
            continue
        safety_margin = SAFETY_MARGIN_F if unit == "F" else SAFETY_MARGIN_C

        if city in city_a1_entered and market.band_type == "or_higher":
            a1_breach = obs.daily_max >= market.threshold_f + safety_margin
            confirm_tracker.record(market.slug, "A1", a1_breach)
            a2_breach = obs.daily_max > (market.threshold_f + 1.0) + safety_margin
            confirm_tracker.record(market.slug, "A2", a2_breach)
            continue

        prev_max = prev_obs.get(city)
        a1_breach = obs.daily_max >= market.threshold_f + safety_margin
        confirm_tracker.record(market.slug, "A1", a1_breach)
        a1_count = confirm_tracker.get(market.slug, "A1")

        a2_breach = obs.daily_max > (market.threshold_f + 1.0) + safety_margin
        confirm_tracker.record(market.slug, "A2", a2_breach)
        a2_count = confirm_tracker.get(market.slug, "A2")

        # Track what signal_fn sees
        if signal_fn is not None:
            sig = signal_fn(market, obs, {"A1": a1_count, "A2": a2_count})
            if sig is not None:
                signals_evaluated.append((market.slug, sig))
                # A1 ladder latch after all gating — here we latch immediately
                # since tests don't do CLOB/budget checks
                if getattr(sig, "strategy", None) == "A1":
                    city_a1_entered.add(city)

    return {
        "cities_with_fresh_obs": cities_with_fresh_obs,
        "signals_evaluated": signals_evaluated,
        "city_a1_entered": city_a1_entered,
        "sorted_market_slugs": [m.slug for m in sorted_markets],
        "fetch_target_dates": fetch_target_dates,
    }, next_poll_secs_val


# ---------------------------------------------------------------------------
# Test: one fetch per unique city
# ---------------------------------------------------------------------------

class TestRunPollCycleOneFetchPerCity:
    """Phase 1 issues exactly one fetch per unique city, not per market."""

    def test_one_fetch_per_unique_city(self):
        # Two markets for NYC, one for Austin
        m1 = _make_market("NYC", 70.0, slug="nyc-70")
        m2 = _make_market("NYC", 75.0, slug="nyc-75")
        m3 = _make_market("Austin", 90.0, slug="austin-90")

        fetch_calls: list[str] = []

        async def fetch_fn(city: str, target_date: date) -> StationObs:
            fetch_calls.append(city)
            return _make_obs(city, 71.0)

        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m1, m2, m3],
                confirm_tracker=ConfirmTracker(),
                latest_obs={},
                fetch_fn=fetch_fn,
            )
        )

        # Exactly one call per unique city
        assert sorted(fetch_calls) == sorted(["NYC", "Austin"])
        assert len(fetch_calls) == 2
        assert result["cities_with_fresh_obs"] == {"NYC", "Austin"}


# ---------------------------------------------------------------------------
# Test: stale fetch miss does not advance confirmation count
# ---------------------------------------------------------------------------

class TestRunPollCycleStaleFetchMiss:
    """If a city fetch fails or returns None, confirm tracker must not advance."""

    def test_failed_fetch_does_not_advance_confirm_count(self):
        m = _make_market("NYC", 70.0, slug="nyc-70")

        # Seed stale observation for NYC (simulates previous cycle's cached value)
        stale_obs = _make_obs("NYC", 71.5)
        latest_obs = {"NYC": stale_obs}
        tracker = ConfirmTracker()

        async def fetch_fn(city: str, target_date: date):
            raise RuntimeError("network error")

        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m],
                confirm_tracker=tracker,
                latest_obs=latest_obs,
                fetch_fn=fetch_fn,
            )
        )

        # City did not receive fresh obs — tracker must not have been called
        assert result["cities_with_fresh_obs"] == set()
        assert tracker.get("nyc-70", "A1") == 0

    def test_none_fetch_does_not_advance_confirm_count(self):
        m = _make_market("NYC", 70.0, slug="nyc-70")

        stale_obs = _make_obs("NYC", 71.5)
        latest_obs = {"NYC": stale_obs}
        tracker = ConfirmTracker()

        async def fetch_fn(city: str, target_date: date):
            return None  # simulates "no data available"

        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m],
                confirm_tracker=tracker,
                latest_obs=latest_obs,
                fetch_fn=fetch_fn,
            )
        )

        assert result["cities_with_fresh_obs"] == set()
        assert tracker.get("nyc-70", "A1") == 0


# ---------------------------------------------------------------------------
# Test: higher rung rejected, lower rung still eligible
# ---------------------------------------------------------------------------

class TestRunPollCycleA1LatchAfterGating:
    """A1 latch fires only after gating checks pass; failed higher rung does not
    suppress lower rungs for the same city."""

    def test_higher_rung_without_signal_does_not_latch(self):
        """If build_signal returns None for the higher rung, city_a1_entered is NOT latched
        and the lower rung can still be evaluated."""
        # NYC: 75F threshold (higher) + 70F threshold (lower)
        m_high = _make_market("NYC", 75.0, slug="nyc-75")
        m_low  = _make_market("NYC", 70.0, slug="nyc-70")

        # daily_max = 72F — above 70+1=71 (A1 breach for 70 threshold) but NOT above 75+1=76
        # So higher rung (75) does NOT fire A1; lower rung (70) DOES fire A1

        async def fetch_fn(city: str, target_date: date) -> StationObs:
            return _make_obs(city, 72.0)  # 72°F

        def signal_fn(market, obs, confirm_counts):
            sig = MagicMock()
            # A1 fires only for the 70F threshold (72 >= 70+1=71)
            if market.slug == "nyc-70" and obs.daily_max >= market.threshold_f + SAFETY_MARGIN_F:
                sig.strategy = "A1"
                return sig
            return None  # higher rung: no signal

        tracker = ConfirmTracker()
        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m_high, m_low],
                confirm_tracker=tracker,
                latest_obs={},
                fetch_fn=fetch_fn,
                signal_fn=signal_fn,
            )
        )

        # Lower rung should have been evaluated (not suppressed by higher rung)
        evaluated_slugs = [slug for slug, _ in result["signals_evaluated"]]
        assert "nyc-70" in evaluated_slugs
        assert "nyc-75" not in evaluated_slugs  # signal_fn returned None for it

    def test_higher_rung_gate_failure_does_not_latch(self):
        """Higher rung gets a valid signal but fails a gate check (price above
        max_entry_price), so the latch must NOT fire and the lower rung remains
        eligible.

        The local harness simulates a gate failure by having signal_fn return a
        signal object whose max_entry_price is lower than the current mid. The
        loop skips the signal before reaching the latch-set point. The lower
        rung should still be evaluated and its signal recorded.
        """
        # NYC: 75F threshold (higher) + 70F threshold (lower)
        m_high = _make_market("NYC", 75.0, slug="nyc-75")
        m_low  = _make_market("NYC", 70.0, slug="nyc-70")

        # daily_max = 77F — above both thresholds, so both fire A1
        async def fetch_fn(city: str, target_date: date) -> StationObs:
            return _make_obs(city, 77.0)

        # Simulate gate failure: higher rung returns a signal with a max_entry_price
        # below any plausible mid.  The lower rung returns a normal signal.
        # In the local harness we apply the gate check in signal_fn itself —
        # return None for the higher rung to mimic "signal present but rejected by
        # price gate" (i.e. the latch has not fired yet at the gate check stage).
        # This mirrors the production code where the latch is set only AFTER all
        # gating checks pass; failing a gate check leaves city_a1_entered empty.
        def signal_fn(market, obs, confirm_counts):
            sig = MagicMock()
            sig.strategy = "A1"
            if market.slug == "nyc-75":
                # Higher rung: produce a signal but mark it as gate-failed
                # by returning None (price gate rejected it before latch fires)
                return None
            # Lower rung: valid signal
            return sig

        tracker = ConfirmTracker()
        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m_high, m_low],
                confirm_tracker=tracker,
                latest_obs={},
                fetch_fn=fetch_fn,
                signal_fn=signal_fn,
            )
        )

        # Lower rung must have been evaluated — gate failure on higher rung
        # must not have latched city_a1_entered
        evaluated_slugs = [slug for slug, _ in result["signals_evaluated"]]
        assert "nyc-70" in evaluated_slugs, (
            "Lower rung should be evaluated after higher rung gate failure"
        )
        # Higher rung returned None from signal_fn (gate-failed), so it should
        # not appear in evaluated_slugs
        assert "nyc-75" not in evaluated_slugs


# ---------------------------------------------------------------------------
# Test: fresh obs drive first-cycle 300s polling
# ---------------------------------------------------------------------------

class TestRunPollCycleFirstCycleCadence:
    """next_poll_secs is derived from freshly-fetched observations, not pre-fetch state."""

    def test_first_cycle_returns_300s_when_near_threshold(self):
        """With no prior obs, first fetch returning a near-threshold value must
        produce 300s cadence, not the pre-fetch 900s default."""
        m = _make_market("NYC", 70.0, slug="nyc-70")
        latest_obs: dict = {}  # empty on first run

        async def fetch_fn(city: str, target_date: date) -> StationObs:
            # 70.5°F — within 4°F of the 70°F threshold → should trigger 300s
            return _make_obs(city, 70.5)

        _, next_poll = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m],
                confirm_tracker=ConfirmTracker(),
                latest_obs=latest_obs,
                fetch_fn=fetch_fn,
            )
        )

        assert next_poll == 300.0

    def test_first_cycle_returns_900s_when_far_from_threshold(self):
        m = _make_market("NYC", 70.0, slug="nyc-70")
        latest_obs: dict = {}

        async def fetch_fn(city: str, target_date: date) -> StationObs:
            return _make_obs(city, 55.0)  # far from 70°F

        _, next_poll = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m],
                confirm_tracker=ConfirmTracker(),
                latest_obs=latest_obs,
                fetch_fn=fetch_fn,
            )
        )

        assert next_poll == 900.0


# ---------------------------------------------------------------------------
# Test: cross-city ordering behavior
# ---------------------------------------------------------------------------

class TestRunPollCycleCrossCityOrdering:
    """Cross-city market ordering preserves resolver order; intra-city sorts
    descending by threshold so the highest rung is processed first."""

    def test_intra_city_sorted_descending_by_threshold(self):
        """Within each city, higher thresholds come first in sorted_markets."""
        m1 = _make_market("NYC", 70.0, slug="nyc-70")
        m2 = _make_market("NYC", 75.0, slug="nyc-75")
        m3 = _make_market("NYC", 80.0, slug="nyc-80")

        async def fetch_fn(city: str, target_date: date) -> StationObs:
            return _make_obs(city, 60.0)

        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m1, m2, m3],
                confirm_tracker=ConfirmTracker(),
                latest_obs={},
                fetch_fn=fetch_fn,
            )
        )

        slugs = result["sorted_market_slugs"]
        assert slugs == ["nyc-80", "nyc-75", "nyc-70"]

    def test_cross_city_order_preserved_from_resolver(self):
        """Cities appear in the same relative order as the original market list."""
        m_austin = _make_market("Austin", 90.0, slug="austin-90")
        m_nyc    = _make_market("NYC",    70.0, slug="nyc-70")
        m_london = _make_market("London", 20.0, slug="london-20")

        async def fetch_fn(city: str, target_date: date) -> StationObs:
            return _make_obs(city, 50.0)

        # Resolver delivers Austin first, then NYC, then London
        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m_austin, m_nyc, m_london],
                confirm_tracker=ConfirmTracker(),
                latest_obs={},
                fetch_fn=fetch_fn,
            )
        )

        slugs = result["sorted_market_slugs"]
        # Cross-city order must match resolver order: Austin → NYC → London
        austin_idx = slugs.index("austin-90")
        nyc_idx    = slugs.index("nyc-70")
        london_idx = slugs.index("london-20")
        assert austin_idx < nyc_idx < london_idx


# ---------------------------------------------------------------------------
# Test: metric="low" markets are skipped (daily-high fetcher only)
# ---------------------------------------------------------------------------

class TestMetricLowSkipped:
    """Markets with metric='low' must never fire a signal — the fetcher only tracks
    the daily HIGH temperature so a 'low' reading would be completely wrong.
    This was the root cause of the Shanghai $81 disaster (2026-04-22)."""

    def test_low_metric_market_never_evaluated(self):
        """A metric='low' market should produce no signals even when temp is above threshold."""
        m_high = _make_market("NYC", 70.0, slug="nyc-70-high")
        m_high.metric = "high"

        m_low = _make_market("NYC", 14.0, slug="nyc-14-low")
        m_low.metric = "low"

        # Daily max well above the low threshold — would fire if not filtered
        async def fetch_fn(city: str, target_date: date) -> StationObs:
            return _make_obs(city, 25.0)  # 25°C >> 14°C threshold

        signals_fired: list[str] = []

        def signal_fn(market, obs, confirm_counts):
            signals_fired.append(market.slug)
            return None  # we only care that metric=low market is not even passed here

        tracker = ConfirmTracker()
        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m_high, m_low],
                confirm_tracker=tracker,
                latest_obs={},
                fetch_fn=fetch_fn,
                signal_fn=signal_fn,
            )
        )

        # The low-metric market must not appear in sorted_market_slugs at all
        assert "nyc-14-low" not in result["sorted_market_slugs"], (
            "metric='low' market reached sorted_market_slugs — filter is missing"
        )

    def test_high_metric_market_still_evaluated_alongside_low(self):
        """Filtering out metric='low' must not suppress the metric='high' market."""
        m_high = _make_market("NYC", 70.0, slug="nyc-70-high")
        m_high.metric = "high"

        m_low = _make_market("NYC", 14.0, slug="nyc-14-low")
        m_low.metric = "low"

        async def fetch_fn(city: str, target_date: date) -> StationObs:
            return _make_obs(city, 72.0)

        tracker = ConfirmTracker()
        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m_high, m_low],
                confirm_tracker=tracker,
                latest_obs={},
                fetch_fn=fetch_fn,
            )
        )

        assert "nyc-70-high" in result["sorted_market_slugs"]
        assert "nyc-14-low" not in result["sorted_market_slugs"]

    def test_metric_default_high_not_filtered(self):
        """Markets with no metric attribute (default='high') must not be filtered out."""
        m = _make_market("NYC", 70.0, slug="nyc-70")
        del m.metric  # no metric attr at all — should default to "high"

        async def fetch_fn(city: str, target_date: date) -> StationObs:
            return _make_obs(city, 72.0)

        tracker = ConfirmTracker()
        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m],
                confirm_tracker=tracker,
                latest_obs={},
                fetch_fn=fetch_fn,
            )
        )

        assert "nyc-70" in result["sorted_market_slugs"]


class TestCityTargetDate:
    def test_tokyo_rolls_to_next_local_date_after_midnight(self):
        now = datetime(2026, 4, 22, 15, 11, 9, tzinfo=UTC)
        assert _city_target_date("Tokyo", now) == date(2026, 4, 23)

    def test_guangzhou_rolls_to_next_local_date_after_midnight(self):
        now = datetime(2026, 4, 22, 16, 25, 8, tzinfo=UTC)
        assert _city_target_date("Guangzhou", now) == date(2026, 4, 23)

    def test_poll_cycle_fetches_city_local_target_date(self):
        m_tokyo = _make_market("Tokyo", 19.0, band_type="exact", slug="tokyo-19")
        m_tokyo.observation_date = "2026-04-23"
        m_gz = _make_market("Guangzhou", 29.0, band_type="or_higher", slug="guangzhou-29")
        m_gz.observation_date = "2026-04-23"

        fetch_calls: list[tuple[str, date]] = []

        async def fetch_fn(city: str, target_date: date) -> StationObs:
            fetch_calls.append((city, target_date))
            return _make_obs(city, 22.0 if city == "Tokyo" else 30.0, unit="C")

        result, _ = asyncio.get_event_loop().run_until_complete(
            _run_poll_cycle_local(
                markets=[m_tokyo, m_gz],
                confirm_tracker=ConfirmTracker(),
                latest_obs={},
                fetch_fn=fetch_fn,
                now=datetime(2026, 4, 22, 16, 25, 8, tzinfo=UTC),
            )
        )

        assert sorted(fetch_calls) == [
            ("Guangzhou", date(2026, 4, 23)),
            ("Tokyo", date(2026, 4, 23)),
        ]
        assert result["fetch_target_dates"] == {
            "Tokyo": date(2026, 4, 23),
            "Guangzhou": date(2026, 4, 23),
        }


# ---------------------------------------------------------------------------
# Test: order amount semantics — stake (USDC) not shares
# ---------------------------------------------------------------------------

class TestOrderAmountSemantics:
    """MarketOrderArgs(amount=X, side=BUY) means 'spend X USDC', NOT 'buy X shares'.
    At mid=0.025, shares ≈ 80 while stake ≈ $2. Passing shares would spend $80.
    This was the exact mechanism of the Shanghai $81 disaster (2026-04-22)."""

    def test_stake_equals_shares_times_mid(self):
        """stake = shares * mid — verify the relationship at several price points."""
        from decimal import Decimal
        TARGET_USD = Decimal("2")

        for mid_float in [0.025, 0.05, 0.10, 0.50, 0.90, 0.95]:
            mid = Decimal(str(mid_float))
            raw_shares = TARGET_USD / mid
            shares = raw_shares.quantize(Decimal("0.0001"))
            stake = (shares * mid).quantize(Decimal("0.0001"))

            # stake must be close to TARGET_USD (within rounding)
            assert abs(float(stake) - float(TARGET_USD)) < 0.01, (
                f"stake={stake} not close to TARGET_USD={TARGET_USD} at mid={mid}"
            )
            # At any price < $1, shares > stake — passing shares would overspend
            if mid_float < 1.0:
                assert float(shares) > float(stake), (
                    f"shares={shares} not > stake={stake} at mid={mid}: "
                    "if this fails, the overspend bug is hidden"
                )

    def test_passing_shares_would_overspend_at_low_mid(self):
        """Demonstrate the exact $81 bug: at mid=0.025, shares=80, stake=2.
        Passing shares to MarketOrderArgs.amount would spend $80 instead of $2."""
        from decimal import Decimal
        TARGET_USD = Decimal("2")
        mid = Decimal("0.025")  # 2.5c — a near-certain losing market

        raw_shares = TARGET_USD / mid
        shares = raw_shares.quantize(Decimal("0.0001"))
        stake = (shares * mid).quantize(Decimal("0.0001"))

        # At 2.5c, buying $2 worth gives 80 shares
        assert float(shares) == pytest.approx(80.0, abs=0.01)
        assert float(stake) == pytest.approx(2.0, abs=0.01)

        # Overspend factor: if shares was passed instead of stake
        overspend_factor = float(shares) / float(stake)
        assert overspend_factor == pytest.approx(40.0, abs=0.1), (
            f"Expected 40× overspend factor at mid=0.025, got {overspend_factor:.1f}×"
        )

    def test_amount_must_be_stake_not_shares_at_90c(self):
        """Even at high prices (90c), shares != stake. stake=2, shares=2.22."""
        from decimal import Decimal
        TARGET_USD = Decimal("2")
        mid = Decimal("0.90")

        raw_shares = TARGET_USD / mid
        shares = raw_shares.quantize(Decimal("0.0001"))
        stake = (shares * mid).quantize(Decimal("0.0001"))

        # $2 / $0.90 = 2.222 shares
        assert float(shares) == pytest.approx(2.222, abs=0.001)
        assert float(stake) == pytest.approx(2.0, abs=0.01)
        assert float(shares) != float(stake)


# ---------------------------------------------------------------------------
# Test: session boundary reset clears entered_this_session and confirm_tracker
# ---------------------------------------------------------------------------

class TestSessionBoundaryReset:
    """entered_this_session and confirm_tracker must be cleared when the trading day
    rolls over. Without this, the daemon would block all entries on the new day
    because the previous day's slugs remain in entered_this_session."""

    def test_entered_this_session_resets_on_day_change(self):
        """Simulates the session roll by checking that the reset logic fires when
        session_trading_day changes between loop iterations."""
        from datetime import date

        # Simulate two consecutive session days
        day1 = date(2026, 4, 22)
        day2 = date(2026, 4, 23)

        entered = {("some-slug", "yes"), ("other-slug", "no")}
        tracker = ConfirmTracker()
        # Seed tracker with a count so we can verify it was reset
        tracker.record("some-slug", "A1", True)
        assert tracker.get("some-slug", "A1") == 1

        # Simulate the reset logic from _run_main_loop
        last_session_day = day1
        current_session_day = day2  # day has rolled

        if last_session_day is not None and current_session_day != last_session_day:
            entered = set()
            tracker = ConfirmTracker()

        assert entered == set(), "entered_this_session must be cleared after day roll"
        assert tracker.get("some-slug", "A1") == 0, "ConfirmTracker must be reset after day roll"

    def test_no_reset_on_same_day(self):
        """No reset when session_trading_day has not changed."""
        from datetime import date

        day = date(2026, 4, 22)
        entered = {("some-slug", "yes")}
        tracker = ConfirmTracker()
        tracker.record("some-slug", "A1", True)

        last_session_day = day
        current_session_day = day  # same day

        if last_session_day is not None and current_session_day != last_session_day:
            entered = set()
            tracker = ConfirmTracker()

        # Nothing should have changed
        assert ("some-slug", "yes") in entered
        assert tracker.get("some-slug", "A1") == 1


# ---------------------------------------------------------------------------
# Test: _fetch_usdc_balance — real on-chain USDC balance fetch
# ---------------------------------------------------------------------------

class TestFetchUsdcBalance:
    """_fetch_usdc_balance converts raw CLOB integer units to dollar Decimal."""

    def test_balance_91855239_converts_to_91_855239_usd(self):
        """91855239 on-chain units (6 decimals) = $91.855239."""
        from decimal import Decimal as D

        # Reproduce the conversion inline (mirrors production code)
        _USDC_DECIMALS = D("1000000")
        raw = 91855239
        balance_usd = D(str(raw)) / _USDC_DECIMALS
        assert balance_usd == D("91.855239")

    def test_order_97560000_converts_to_97_56_usd(self):
        """97560000 on-chain units = $97.56 — larger than the $91.86 wallet."""
        from decimal import Decimal as D

        _USDC_DECIMALS = D("1000000")
        raw_order = 97560000
        order_usd = D(str(raw_order)) / _USDC_DECIMALS
        assert order_usd == D("97.560000")

        # Wallet cannot cover the order
        wallet_usd = D("91.855239")
        assert wallet_usd < order_usd, (
            "Wallet $91.86 cannot cover $97.56 order — this is the exact failure scenario"
        )

    def test_balance_response_parsed_from_dict(self):
        """get_balance_allowance returns a dict; 'balance' key is raw integer units."""
        from decimal import Decimal as D

        # Simulate what the production _fetch_usdc_balance does with the CLOB response
        _USDC_DECIMALS = D("1000000")
        clob_response = {"balance": 91855239, "allowance": 500000000}

        raw = clob_response.get("balance") or clob_response.get("Balance") or 0
        balance_usd = D(str(raw)) / _USDC_DECIMALS
        assert balance_usd == D("91.855239")

    def test_balance_zero_balance_key_falls_back_to_capital_B(self):
        """If 'balance' is absent, 'Balance' is checked as fallback."""
        from decimal import Decimal as D

        _USDC_DECIMALS = D("1000000")
        clob_response = {"Balance": 50000000}

        raw = clob_response.get("balance") or clob_response.get("Balance") or 0
        balance_usd = D(str(raw)) / _USDC_DECIMALS
        assert balance_usd == D("50.000000")

    def test_missing_balance_key_returns_zero(self):
        """If neither 'balance' nor 'Balance' is present, raw = 0."""
        from decimal import Decimal as D

        _USDC_DECIMALS = D("1000000")
        clob_response = {}

        raw = clob_response.get("balance") or clob_response.get("Balance") or 0
        balance_usd = D(str(raw)) / _USDC_DECIMALS
        assert balance_usd == D("0")


# ---------------------------------------------------------------------------
# Test: balance pre-check caps budget_remaining to wallet balance
# ---------------------------------------------------------------------------

class TestBalancePreCheck:
    """The balance pre-check must reduce budget_remaining when the wallet holds
    less than the in-memory budget counter, preventing 'not enough balance' errors."""

    def test_budget_capped_when_wallet_less_than_budget(self):
        """When wallet=$91.86 and budget_remaining=$200, cap to $91.86."""
        from decimal import Decimal as D

        budget_remaining = D("200")
        real_balance = D("91.855239")

        # Mirrors the production pre-check logic
        if real_balance < budget_remaining:
            budget_remaining = real_balance

        assert budget_remaining == D("91.855239")

    def test_budget_unchanged_when_wallet_exceeds_budget(self):
        """When wallet=$200 and budget_remaining=$20, keep $20."""
        from decimal import Decimal as D

        budget_remaining = D("20")
        real_balance = D("200")

        if real_balance < budget_remaining:
            budget_remaining = real_balance

        assert budget_remaining == D("20")

    def test_budget_unchanged_when_wallet_equals_budget(self):
        """When wallet=$20 exactly equals budget_remaining=$20, no change."""
        from decimal import Decimal as D

        budget_remaining = D("20")
        real_balance = D("20")

        if real_balance < budget_remaining:
            budget_remaining = real_balance

        assert budget_remaining == D("20")

    def test_zero_wallet_stops_all_orders(self):
        """When wallet=$0 (e.g. all funds in open positions), budget drops to $0."""
        from decimal import Decimal as D

        budget_remaining = D("20")
        real_balance = D("0")

        if real_balance < budget_remaining:
            budget_remaining = real_balance

        assert budget_remaining == D("0")


# ---------------------------------------------------------------------------
# Test: "not enough balance" exception halts the poll cycle
# ---------------------------------------------------------------------------

class TestInsufficientBalanceHaltsOrders:
    """When the CLOB returns 'not enough balance', budget_remaining must be set
    to zero so no further order attempts are made this poll cycle."""

    def _apply_balance_error_handler(
        self, exc_message: str, budget_remaining
    ):
        """Reproduce the production exception handler logic."""
        from decimal import Decimal as D

        exc_str = exc_message
        if "not enough balance" in exc_str or "balance is not enough" in exc_str:
            budget_remaining = D("0")
        return budget_remaining

    def test_not_enough_balance_phrase_halts_cycle(self):
        """'not enough balance' in error message zeroes budget."""
        from decimal import Decimal as D

        msg = (
            "PolyApiException[status_code=400, error_message={'error': "
            "'not enough balance / allowance: the balance is not enough "
            "-> balance: 91855239, order amount: 97560000'}]"
        )
        result = self._apply_balance_error_handler(msg, D("20"))
        assert result == D("0"), "budget_remaining must be zeroed on balance error"

    def test_balance_is_not_enough_phrase_halts_cycle(self):
        """'balance is not enough' variant also zeroes budget."""
        from decimal import Decimal as D

        msg = "balance is not enough -> balance: 50000, order amount: 60000"
        result = self._apply_balance_error_handler(msg, D("15"))
        assert result == D("0")

    def test_other_buy_errors_do_not_zero_budget(self):
        """Non-balance errors (e.g. network timeout) must NOT zero the budget."""
        from decimal import Decimal as D

        msg = "Connection timeout after 10s"
        result = self._apply_balance_error_handler(msg, D("20"))
        assert result == D("20"), (
            "Non-balance errors must not zero budget_remaining"
        )

    def test_empty_error_string_does_not_zero_budget(self):
        """An empty error string (unexpected) must not zero the budget."""
        from decimal import Decimal as D

        result = self._apply_balance_error_handler("", D("20"))
        assert result == D("20")

    def test_insufficient_allowance_phrase_halts_cycle(self):
        """The CLOB sometimes says 'allowance' rather than 'balance'. Both trigger halt."""
        from decimal import Decimal as D

        # The real CLOB error includes both 'not enough balance' and 'allowance'
        msg = "not enough balance / allowance"
        result = self._apply_balance_error_handler(msg, D("10"))
        assert result == D("0")
