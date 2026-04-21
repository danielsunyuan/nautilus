"""Unit tests for weather_confirmed_signal evaluator module."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass as _dc
from datetime import UTC, date, datetime, timedelta
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


signal_module = _load_module(
    "examples.live.polymarket.weather_confirmed_signal",
    ROOT / "examples" / "live" / "polymarket" / "weather_confirmed_signal.py",
)

evaluate_a1 = signal_module.evaluate_a1
evaluate_a2 = signal_module.evaluate_a2
evaluate_b2 = signal_module.evaluate_b2
_is_data_fresh = signal_module._is_data_fresh
_spike_detected = signal_module._spike_detected
ConfirmTracker = signal_module.ConfirmTracker
ConfirmedSignal = signal_module.ConfirmedSignal
build_signal = signal_module.build_signal
SAFETY_MARGIN_C = signal_module.SAFETY_MARGIN_C
SAFETY_MARGIN_F = signal_module.SAFETY_MARGIN_F
B2_GAP_C = signal_module.B2_GAP_C
B2_GAP_F = signal_module.B2_GAP_F
MIN_CONFIRM_POLLS = signal_module.MIN_CONFIRM_POLLS


# --- A1 tests ---


def test_a1_fires_when_breached_with_margin_and_two_polls():
    assert evaluate_a1(daily_max=15.6, threshold=15.0, unit="C", confirm_count=2)


def test_a1_blocked_when_within_safety_margin():
    # daily_max = 15.4, threshold = 15.0, margin = 0.5 → 15.4 < 15.5
    assert not evaluate_a1(daily_max=15.4, threshold=15.0, unit="C", confirm_count=2)


def test_a1_blocked_when_only_one_poll():
    assert not evaluate_a1(daily_max=16.0, threshold=15.0, unit="C", confirm_count=1)


def test_a1_fahrenheit_uses_correct_margin():
    # margin for F is 1.0; threshold 70F, max=71.0 → exactly 1.0 above → fires
    assert evaluate_a1(daily_max=71.0, threshold=70.0, unit="F", confirm_count=2)
    # max=70.9 → 0.9 above → blocked
    assert not evaluate_a1(daily_max=70.9, threshold=70.0, unit="F", confirm_count=2)


# --- A2 tests ---


def test_a2_fires_when_above_band_upper_plus_margin():
    # band lower=54F, upper=55F, margin=1.0F → need daily_max > 56.0
    assert evaluate_a2(daily_max=56.1, threshold=54.0, unit="F", confirm_count=2)


def test_a2_blocked_at_exactly_band_upper():
    # daily_max=55.0 is AT upper bound, not above
    assert not evaluate_a2(daily_max=55.0, threshold=54.0, unit="F", confirm_count=2)


def test_a2_blocked_within_safety_margin():
    # band upper=55F, daily_max=55.9 → 55.9 - 55 = 0.9 < 1.0 safety → blocked
    assert not evaluate_a2(daily_max=55.9, threshold=54.0, unit="F", confirm_count=2)


def test_a2_celsius_band():
    # band lower=10C, upper=11C, margin=0.5 → need >11.5
    assert evaluate_a2(daily_max=11.6, threshold=10.0, unit="C", confirm_count=2)
    assert not evaluate_a2(daily_max=11.4, threshold=10.0, unit="C", confirm_count=2)


# --- B2 tests ---


def test_b2_fires_when_far_below_threshold_after_15():
    # threshold=25C, daily_max=18C (gap=7>5), local_hour=16
    assert evaluate_b2(daily_max=18.0, threshold=25.0, unit="C", local_hour=16)


def test_b2_blocked_before_15_00():
    assert not evaluate_b2(daily_max=18.0, threshold=25.0, unit="C", local_hour=14)


def test_b2_blocked_when_gap_insufficient():
    # gap=4.9 < 5.0
    assert not evaluate_b2(daily_max=20.1, threshold=25.0, unit="C", local_hour=16)


def test_b2_fahrenheit_gap():
    # gap threshold is 9F; threshold=90F, daily_max=80F (gap=10 > 9) → fires at 16:00
    assert evaluate_b2(daily_max=80.0, threshold=90.0, unit="F", local_hour=15)
    # gap=8.9 < 9 → blocked
    assert not evaluate_b2(daily_max=81.1, threshold=90.0, unit="F", local_hour=15)


# --- Data quality guards ---


def test_is_data_fresh_within_90_min():
    now = datetime.now(UTC)
    as_of = now - timedelta(minutes=89)
    assert _is_data_fresh(as_of, now)


def test_is_data_stale_beyond_90_min():
    now = datetime.now(UTC)
    as_of = now - timedelta(minutes=91)
    assert not _is_data_fresh(as_of, now)


def test_spike_detected_above_threshold_celsius():
    assert _spike_detected(30.0, 25.0, "C")  # 5°C jump > 4°C limit


def test_spike_not_detected_below_threshold():
    assert not _spike_detected(28.0, 25.0, "C")  # 3°C jump < 4°C limit


def test_spike_not_detected_on_first_poll():
    assert not _spike_detected(28.0, None, "C")


# --- ConfirmTracker tests ---


def test_confirm_tracker_increments_on_true():
    t = ConfirmTracker()
    assert t.record("slug", "A1", True) == 1
    assert t.record("slug", "A1", True) == 2


def test_confirm_tracker_resets_on_false():
    t = ConfirmTracker()
    t.record("slug", "A1", True)
    t.record("slug", "A1", True)
    assert t.record("slug", "A1", False) == 0


def test_confirm_tracker_clear_slug():
    t = ConfirmTracker()
    t.record("slug", "A1", True)
    t.record("slug", "A2", True)
    t.clear_slug("slug")
    assert t.get("slug", "A1") == 0
    assert t.get("slug", "A2") == 0


# --- build_signal integration tests ---
# Uses a minimal mock DailyTemperatureMarket-like object


@_dc
class _FakeMarket:
    slug: str
    city: str
    observation_date: object
    threshold_f: float
    band_type: str
    yes_token_id: str = "yes-tok"
    no_token_id: str = "no-tok"


_NOW = datetime(2026, 4, 22, 16, 0, 0, tzinfo=UTC)
_FRESH = _NOW - timedelta(minutes=30)


def test_build_signal_a1_returns_yes_token():
    market = _FakeMarket(
        "slug-paris-15c", "Paris", date(2026, 4, 22), 15.0, "or_higher"
    )
    sig = build_signal(
        market=market,
        daily_max=16.0,
        unit="C",
        as_of_utc=_FRESH,
        confirm_counts={"A1": 2, "A2": 0},
        prev_daily_max=15.8,
        now=_NOW,
        city_tz="Europe/Paris",
    )
    assert sig is not None
    assert sig.strategy == "A1"
    assert sig.token_side == "yes"
    assert sig.token_id == "yes-tok"


def test_build_signal_a2_returns_no_token():
    market = _FakeMarket(
        "slug-nyc-54-55f", "NYC", date(2026, 4, 22), 54.0, "exact"
    )
    sig = build_signal(
        market=market,
        daily_max=56.2,
        unit="F",
        as_of_utc=_FRESH,
        confirm_counts={"A1": 0, "A2": 2},
        prev_daily_max=56.0,
        now=_NOW,
        city_tz="America/New_York",
    )
    assert sig is not None
    assert sig.strategy == "A2"
    assert sig.token_side == "no"


def test_build_signal_b2_triggers_after_15_local():
    market = _FakeMarket(
        "slug-london-20c", "London", date(2026, 4, 22), 20.0, "or_higher"
    )
    # London BST = UTC+1; _NOW is 16:00 UTC = 17:00 local
    sig = build_signal(
        market=market,
        daily_max=13.0,
        unit="C",
        as_of_utc=_FRESH,
        confirm_counts={"A1": 1, "A2": 0},
        prev_daily_max=12.8,
        now=_NOW,
        city_tz="Europe/London",
    )
    assert sig is not None
    assert sig.strategy == "B2"


def test_build_signal_skips_stale_data():
    market = _FakeMarket(
        "slug-tokyo-30c", "Tokyo", date(2026, 4, 22), 25.0, "or_higher"
    )
    stale = _NOW - timedelta(minutes=95)
    sig = build_signal(
        market=market,
        daily_max=26.0,
        unit="C",
        as_of_utc=stale,
        confirm_counts={"A1": 2, "A2": 0},
        prev_daily_max=25.8,
        now=_NOW,
        city_tz="Asia/Tokyo",
    )
    assert sig is None


def test_build_signal_skips_spike():
    market = _FakeMarket(
        "slug-miami-90f", "Miami", date(2026, 4, 22), 85.0, "or_higher"
    )
    sig = build_signal(
        market=market,
        daily_max=92.0,
        unit="F",
        as_of_utc=_FRESH,
        confirm_counts={"A1": 2, "A2": 0},
        prev_daily_max=83.0,  # 9F jump > 7F limit
        now=_NOW,
        city_tz="America/New_York",
    )
    assert sig is None


def test_build_signal_a1_blocked_by_a2_count_only():
    """A1 must use its own confirm count, not A2's — the key cross-contamination regression."""
    market = _FakeMarket(
        "slug-test-cc", "TestCity", date(2026, 4, 22), 20.0, "or_higher"
    )
    # A2 has 2 confirmations but A1 has 0 — A1 must NOT fire
    sig = build_signal(
        market=market,
        daily_max=21.0,
        unit="C",
        as_of_utc=_FRESH,
        confirm_counts={"A1": 0, "A2": 2},
        prev_daily_max=20.9,
        now=_NOW,
        city_tz="UTC",
    )
    assert sig is None  # A1 needs A1 count ≥ 2; A2 count must not substitute


def test_build_signal_skips_hong_kong_for_a1():
    market = _FakeMarket(
        "slug-hk-30c", "Hong Kong", date(2026, 4, 22), 28.0, "or_higher"
    )
    sig = build_signal(
        market=market,
        daily_max=30.0,
        unit="C",
        as_of_utc=_FRESH,
        confirm_counts={"A1": 3, "A2": 0},
        prev_daily_max=29.8,
        now=_NOW,
        city_tz="Asia/Hong_Kong",
    )
    assert sig is None  # HKO oracle intraday proxy not reliable; local_hour=00 blocks B2 too


def test_build_signal_a1_output_structure():
    market = _FakeMarket(
        "slug-test", "TestCity", date(2026, 4, 22), 20.0, "or_higher"
    )
    sig = build_signal(
        market=market,
        daily_max=21.0,
        unit="C",
        as_of_utc=_FRESH,
        confirm_counts={"A1": 2, "A2": 0},
        prev_daily_max=20.9,
        now=_NOW,
        city_tz="UTC",
    )
    assert sig is not None
    assert sig.market_slug == "slug-test"
    assert sig.city == "TestCity"
    assert sig.preset_name == "temp_confirmed_a1"
    assert sig.arena == "temp_confirmed"
    assert sig.stop_loss_price == 0.85
    assert sig.take_profit_price == 0.99
    assert sig.max_entry_price == 0.97
