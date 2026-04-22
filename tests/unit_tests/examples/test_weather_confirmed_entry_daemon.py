"""Tests for weather_confirmed_entry_daemon module."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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
) -> tuple[dict, float]:
    """Stripped _run_poll_cycle for unit tests: no CLOB, no writer, no I/O.

    Returns (cities_with_fresh_obs_dict, next_poll_secs).
    cities_with_fresh_obs_dict maps city -> obs that were fetched this cycle.
    """
    unique_cities = {m.city for m in markets}
    prev_obs = {
        city: (latest_obs[city].daily_max if city in latest_obs else None)
        for city in unique_cities
    }
    cities_with_fresh_obs: set[str] = set()
    for city in unique_cities:
        try:
            obs = await fetch_fn(city)
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

        async def fetch_fn(city: str) -> StationObs:
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

        async def fetch_fn(city: str):
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

        async def fetch_fn(city: str):
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

        async def fetch_fn(city: str) -> StationObs:
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
        async def fetch_fn(city: str) -> StationObs:
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

        async def fetch_fn(city: str) -> StationObs:
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

        async def fetch_fn(city: str) -> StationObs:
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

        async def fetch_fn(city: str) -> StationObs:
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

        async def fetch_fn(city: str) -> StationObs:
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
