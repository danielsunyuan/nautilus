# Weather Confirmed-Entry Daemon Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a standalone daemon that polls Wunderground for live daily temperatures, detects when a market's outcome is confirmed or near-certain, and executes the trade directly via CLOB before the market price fully catches up.

**Architecture:** Entirely bypasses `TradingNode` (same rationale as `_run_direct_clob_entry_session` — `TradingNode.build()` hangs on futex). Polls `weather_wunderground_fetcher.py` directly on an adaptive schedule (300s when near threshold, 900s otherwise). Evaluates three confirmed strategies (A1/A2/B2). Executes via `py_clob_client` FOK BUY. Writes `strategy_result` JSONL events in the same schema as the existing live daemon so settlement and take-profit watchers pick them up automatically.

**Tech Stack:** Python 3.12, `py_clob_client`, `httpx`, `weather_wunderground_fetcher.py`, `weather_daily_temperature_resolver.py`, `polymarket_weather_daily_temperature_live_daemon.py` (reuses `_build_clob_client_for_entry`, `_already_entered_today`, `_session_trading_day`), docker-compose `nautilus-recorder:latest` image.

---

## Data Chain (no TradingNode)

```
WU API (TWC / ASOS / HKO)
  ↓  fetch_daily_high(city)  [weather_wunderground_fetcher.py]
StationObs { daily_max, unit, as_of_utc, obs_count }
  ↓  evaluate_confirmed_signal(obs, market)
ConfirmedSignal { strategy, token_id, token_side, price_cap, ... }
  ↓  CLOB midpoint check (reject if already at 0.99)
  ↓  py_clob_client FOK BUY
strategy_result JSONL event
  ↓  (picked up automatically by)
weather_daily_temperature_settlement.py  +  weather_daily_temperature_take_profit.py
```

## Three Strategies

| ID | Condition | Buy | Max entry price |
|----|-----------|-----|-----------------|
| A1 | `or_higher` AND `daily_max ≥ threshold + SAFETY` AND 2 consecutive polls | YES | 0.97 |
| A2 | `exact` band AND `daily_max > (threshold + 1.0) + SAFETY` AND 2 consecutive polls | NO | 0.96 |
| B2 | `or_higher` AND `daily_max < threshold − 5.0` AND local_hour ≥ 15 | NO | 0.93 |

Safety margin: 1.0°F for Fahrenheit markets, 0.5°C for Celsius markets.

## Pre-Deploy Checklist (implemented as code guards)

1. **`as_of_utc` freshness gate** — skip city if latest WU observation is >90 min old
2. **Spike filter** — skip if `daily_max` jumped >4°C (or >7°F) since previous poll cycle
3. **Two-poll confirmation** — A1/A2 require 2 consecutive polls above threshold before entry
4. **Safety margin buffer** — never enter within 1 unit of threshold (°F) or 0.5°C
5. **CLOB price gate** — fetch midpoint; skip if already ≥ 0.98 (no edge) or ≤ 0.02
6. **Already-entered guard** — reuses `_already_entered_today()` keyed on `(slug, side)`
7. **HKO Hong Kong flag** — Hong Kong marked `basis_risk=True`; skipped for A1/A2 by default (oracle doesn't publish intraday; TWC proxy may diverge)
8. **Station audit** — `CITY_STATIONS` map is the verified source; code reads directly from it

---

## Task 1: `ConfirmedSignal` dataclass + pure signal evaluator

**Files:**
- Create: `examples/live/polymarket/weather_confirmed_signal.py`

This module is pure functions with no network calls — fully unit-testable.

```python
# weather_confirmed_signal.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


StrategyId = Literal["A1", "A2", "B2"]

# Safety margins: if daily_max is within this buffer of the threshold,
# we don't consider it confirmed.
SAFETY_MARGIN_C = 0.5   # °C
SAFETY_MARGIN_F = 1.0   # °F

# B2: temperature must be at least this far below threshold after 15:00 local
B2_GAP_C = 5.0
B2_GAP_F = 9.0   # 5°C ≈ 9°F

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


@dataclass(frozen=True, slots=True)
class ConfirmedSignal:
    """A trade signal produced by comparing WU daily_max to a market threshold."""
    strategy: StrategyId
    market_slug: str
    city: str
    observation_date: str
    threshold_f: float        # threshold value (unit matches market unit)
    unit: str                 # "F" or "C"
    token_id: str
    token_side: str           # "yes" or "no"
    max_entry_price: float
    preset_name: str
    arena: str
    stop_loss_price: float
    take_profit_price: float
    wu_daily_max: float
    wu_as_of_utc: str         # ISO8601


def _safety_margin(unit: str) -> float:
    return SAFETY_MARGIN_F if unit == "F" else SAFETY_MARGIN_C


def _b2_gap(unit: str) -> float:
    return B2_GAP_F if unit == "F" else B2_GAP_C


def _is_data_fresh(as_of_utc: datetime, now: datetime) -> bool:
    age_secs = (now - as_of_utc).total_seconds()
    return age_secs <= MAX_DATA_AGE_SECS


def _spike_detected(daily_max: float, prev_daily_max: float | None, unit: str) -> bool:
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
    confirm_count: int,  # how many consecutive polls have shown this breach
) -> bool:
    """A1: or_higher market — is YES outcome confirmed?"""
    margin = _safety_margin(unit)
    return (
        daily_max >= threshold + margin
        and confirm_count >= MIN_CONFIRM_POLLS
    )


def evaluate_a2(
    *,
    daily_max: float,
    threshold: float,  # lower bound of exact band
    unit: str,
    confirm_count: int,
) -> bool:
    """A2: exact band market — has daily_max exceeded the upper bound (threshold+1)?"""
    band_upper = threshold + 1.0
    margin = _safety_margin(unit)
    return (
        daily_max > band_upper + margin
        and confirm_count >= MIN_CONFIRM_POLLS
    )


def evaluate_b2(
    *,
    daily_max: float,
    threshold: float,
    unit: str,
    local_hour: int,    # 0-23, city's local hour
) -> bool:
    """B2: or_higher market — is temperature too far below threshold after 15:00?"""
    gap = _b2_gap(unit)
    return (
        daily_max < threshold - gap
        and local_hour >= 15
    )
```

**Tests:** See Task 2.

**Commit:** `feat: add weather confirmed signal evaluator (A1/A2/B2)`

---

## Task 2: Unit tests for signal evaluator

**Files:**
- Create: `tests/unit_tests/examples/test_weather_confirmed_signal.py`

```python
# test_weather_confirmed_signal.py
import pytest
from datetime import datetime, UTC, timedelta
from examples.live.polymarket.weather_confirmed_signal import (
    evaluate_a1, evaluate_a2, evaluate_b2,
    _is_data_fresh, _spike_detected,
    SAFETY_MARGIN_C, SAFETY_MARGIN_F, B2_GAP_C, B2_GAP_F,
    MIN_CONFIRM_POLLS,
)


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
    assert _spike_detected(30.0, 25.0, "C")   # 5°C jump > 4°C limit

def test_spike_not_detected_below_threshold():
    assert not _spike_detected(28.0, 25.0, "C")  # 3°C jump < 4°C limit

def test_spike_not_detected_on_first_poll():
    assert not _spike_detected(28.0, None, "C")
```

**Run:** `cd /home/atlas/EL/nautilus && uv run --extra polymarket --with pytest python -m pytest tests/unit_tests/examples/test_weather_confirmed_signal.py -v`

**Expected:** All 17 tests pass.

**Commit:** `test: add confirmed signal evaluator unit tests`

---

## Task 3: `MarketSignalEvaluator` — market + obs → signal

**Files:**
- Modify: `examples/live/polymarket/weather_confirmed_signal.py` (add `build_signal`)

Add to the bottom of `weather_confirmed_signal.py`:

```python
import zoneinfo as _zi
from examples.live.polymarket.weather_daily_temperature_resolver import DailyTemperatureMarket

# Cities that use HKO oracle — intraday data is proxy only, skip A1/A2
HKO_CITIES: frozenset[str] = frozenset({"Hong Kong"})


def build_signal(
    market: "DailyTemperatureMarket",
    daily_max: float,
    unit: str,
    as_of_utc: "datetime",
    confirm_count: int,
    prev_daily_max: float | None,
    now: "datetime",
    city_tz: str,           # IANA timezone name for local_hour computation
) -> "ConfirmedSignal | None":
    """
    Evaluate all enabled strategies for one (market, obs) pair.
    Returns the first matching signal, or None.

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
        if evaluate_a1(daily_max=daily_max, threshold=threshold,
                        unit=unit, confirm_count=confirm_count):
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
        if evaluate_a2(daily_max=daily_max, threshold=threshold,
                        unit=unit, confirm_count=confirm_count):
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
            tz = _zi.ZoneInfo(city_tz)
            local_hour = now.astimezone(tz).hour
        except Exception:
            local_hour = now.hour  # fallback to UTC
        if evaluate_b2(daily_max=daily_max, threshold=threshold,
                        unit=unit, local_hour=local_hour):
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
```

**Tests:** Add to `test_weather_confirmed_signal.py`:

```python
# --- build_signal integration tests ---
# Uses a minimal mock DailyTemperatureMarket-like object

from dataclasses import dataclass as _dc
from datetime import date as _date

@_dc
class _FakeMarket:
    slug: str
    city: str
    observation_date: object
    threshold_f: float
    band_type: str
    yes_token_id: str = "yes-tok"
    no_token_id: str = "no-tok"

from examples.live.polymarket.weather_confirmed_signal import build_signal

_NOW = datetime(2026, 4, 22, 16, 0, 0, tzinfo=UTC)
_FRESH = _NOW - timedelta(minutes=30)

def test_build_signal_a1_returns_yes_token():
    market = _FakeMarket("slug-paris-15c", "Paris", _date(2026, 4, 22), 15.0, "or_higher")
    sig = build_signal(
        market=market, daily_max=16.0, unit="C", as_of_utc=_FRESH,
        confirm_count=2, prev_daily_max=15.8, now=_NOW,
        city_tz="Europe/Paris",
    )
    assert sig is not None
    assert sig.strategy == "A1"
    assert sig.token_side == "yes"
    assert sig.token_id == "yes-tok"

def test_build_signal_a2_returns_no_token():
    market = _FakeMarket("slug-nyc-54-55f", "NYC", _date(2026, 4, 22), 54.0, "exact")
    sig = build_signal(
        market=market, daily_max=56.2, unit="F", as_of_utc=_FRESH,
        confirm_count=2, prev_daily_max=56.0, now=_NOW,
        city_tz="America/New_York",
    )
    assert sig is not None
    assert sig.strategy == "A2"
    assert sig.token_side == "no"

def test_build_signal_b2_triggers_after_15_local():
    market = _FakeMarket("slug-london-20c", "London", _date(2026, 4, 22), 20.0, "or_higher")
    # London BST = UTC+1; _NOW is 16:00 UTC = 17:00 local
    sig = build_signal(
        market=market, daily_max=13.0, unit="C", as_of_utc=_FRESH,
        confirm_count=1, prev_daily_max=12.8, now=_NOW,
        city_tz="Europe/London",
    )
    assert sig is not None
    assert sig.strategy == "B2"

def test_build_signal_skips_stale_data():
    market = _FakeMarket("slug-tokyo-30c", "Tokyo", _date(2026, 4, 22), 25.0, "or_higher")
    stale = _NOW - timedelta(minutes=95)
    sig = build_signal(
        market=market, daily_max=26.0, unit="C", as_of_utc=stale,
        confirm_count=2, prev_daily_max=25.8, now=_NOW,
        city_tz="Asia/Tokyo",
    )
    assert sig is None

def test_build_signal_skips_spike():
    market = _FakeMarket("slug-miami-90f", "Miami", _date(2026, 4, 22), 85.0, "or_higher")
    sig = build_signal(
        market=market, daily_max=92.0, unit="F", as_of_utc=_FRESH,
        confirm_count=2, prev_daily_max=83.0,  # 9F jump > 7F limit
        now=_NOW, city_tz="America/New_York",
    )
    assert sig is None

def test_build_signal_skips_hong_kong_for_a1():
    market = _FakeMarket("slug-hk-30c", "Hong Kong", _date(2026, 4, 22), 28.0, "or_higher")
    sig = build_signal(
        market=market, daily_max=30.0, unit="C", as_of_utc=_FRESH,
        confirm_count=3, prev_daily_max=29.8, now=_NOW,
        city_tz="Asia/Hong_Kong",
    )
    assert sig is None  # HKO oracle intraday proxy not reliable
```

**Run:** `uv run --extra polymarket --with pytest python -m pytest tests/unit_tests/examples/test_weather_confirmed_signal.py -v`
**Expected:** All 23 tests pass.

**Commit:** `feat: add build_signal market+obs evaluator`

---

## Task 4: Confirm-count tracker

**Files:**
- Modify: `examples/live/polymarket/weather_confirmed_signal.py` (add `ConfirmTracker`)

The daemon polls cities on a 300–900s schedule. A1/A2 require two consecutive polls confirming the breach. This stateful tracker handles that.

```python
@dataclass
class ConfirmTracker:
    """
    Tracks consecutive poll confirmations per (slug, strategy) key.

    On each poll, call `record(slug, strategy, confirmed)`.
    Returns the current consecutive-confirmation count.
    """
    _counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def record(self, slug: str, strategy: str, confirmed: bool) -> int:
        key = (slug, strategy)
        if confirmed:
            self._counts[key] = self._counts.get(key, 0) + 1
        else:
            self._counts.pop(key, None)
        return self._counts.get(key, 0)

    def get(self, slug: str, strategy: str) -> int:
        return self._counts.get((slug, strategy), 0)

    def clear_slug(self, slug: str) -> None:
        """Remove all confirmation counts for a slug (after entry)."""
        for key in list(self._counts):
            if key[0] == slug:
                del self._counts[key]
```

Add tests in `test_weather_confirmed_signal.py`:

```python
from examples.live.polymarket.weather_confirmed_signal import ConfirmTracker

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
```

**Run & pass, then commit:** `feat: add ConfirmTracker for two-poll confirmation gate`

---

## Task 5: Main daemon `weather_confirmed_entry_daemon.py`

**Files:**
- Create: `examples/live/polymarket/weather_confirmed_entry_daemon.py`

Key structure — mirrors `polymarket_weather_daily_temperature_live_daemon.py` style:

```python
#!/usr/bin/env python3
"""
Confirmed-entry daemon for Polymarket weather temperature markets.

Polls Wunderground for each city's running daily high and executes CLOB
orders when the temperature confirms or strongly implies a market outcome.

Three strategies (in priority order per poll cycle):
  A1 — or_higher + daily_max confirmed above threshold  → BUY YES  (max 0.97)
  A2 — exact band + daily_max confirmed above upper band → BUY NO   (max 0.96)
  B2 — or_higher + daily_max 5°C+ below threshold after 15:00 local → BUY NO (max 0.93)

Execution: direct py_clob_client FOK BUY — no TradingNode.
Output: strategy_result JSONL events compatible with settlement + take-profit watchers.
"""
```

**Core loop (`_run_poll_cycle`):**
1. `await _default_resolve_markets()` — get today's active markets
2. Filter to city-local-date tradeable markets (reuse `_city_local_date`)
3. For each market, look up `CITY_STATIONS[market.city]` for station + unit + tz
4. Call `fetch_daily_high(market.city)` → `StationObs | None`
5. Record prev_daily_max, call `ConfirmTracker.record()` for A1/A2 checks
6. Call `build_signal(market, obs, ...)` → `ConfirmedSignal | None`
7. Skip if already entered today (`_already_entered_today` keyed on `(slug, side)`)
8. Fetch CLOB mid → skip if mid >= 0.98 or mid <= 0.02
9. Check budget remaining
10. Submit FOK BUY via `_build_clob_client_for_entry()`
11. Write `strategy_result` JSONL event (includes `wu_daily_max`, `wu_as_of_utc`, `strategy_type`)
12. `ConfirmTracker.clear_slug(slug)` after entry

**Adaptive poll interval:**
```python
def _next_poll_secs(markets, latest_obs: dict[str, StationObs]) -> float:
    """300s if any city is within 2°C/4°F of a threshold, else 900s."""
    for m in markets:
        obs = latest_obs.get(m.city)
        if obs is None:
            continue
        unit = obs.unit
        gap = abs(obs.daily_max - m.threshold_f)
        near_threshold = gap <= (4.0 if unit == "F" else 2.0)
        if near_threshold:
            return 300.0
    return 900.0
```

**JSONL event schema** (extends existing `strategy_result`):
```python
{
    "event": "strategy_result",
    "asset_class": "weather",
    "weather_market_type": "daily_temperature",
    "preset_name": signal.preset_name,         # "temp_confirmed_a1" etc.
    "arena": signal.arena,                      # "temp_confirmed"
    "mode": "confirmed",
    "market_slug": signal.market_slug,
    "city": signal.city,
    "observation_date": signal.observation_date,
    "threshold_f": signal.threshold_f,
    "metric": market.metric,
    "token_side": signal.token_side,
    "instrument_id": f"{cond_id}-{signal.token_id}.POLYMARKET",
    "entry_price": mid,
    "shares": float(shares),
    "stake": float(stake),
    "accounting_status": "open",
    "resolved": False,
    "exit_reason": "position_open",
    "entry_time": now_fn().isoformat(),
    "exit_time": None,
    "pnl": None,
    "stop_loss_price": signal.stop_loss_price,
    "take_profit_price": signal.take_profit_price,
    "strategy_type": signal.strategy,          # "A1", "A2", "B2"
    "wu_daily_max": signal.wu_daily_max,
    "wu_as_of_utc": signal.wu_as_of_utc,
    "timestamp": now_fn().isoformat(),
    "clob_response": str(resp_order),
}
```

**CLI args** (reuse `_build_parser` pattern):
```
--output-dir   (default: /workspace/nautilus/outputs)
--budget       (default: 20.0  — separate daily budget from main live daemon)
--dry-run      (fetch WU + evaluate signals but skip CLOB submission)
--max-rounds   (default: 0 = infinite)
```

**Output file path:**
```
outputs/polymarket/runs/weather_confirmed_live_{timestamp}.jsonl
```

The file is named differently from `weather_temp_live_*` so settlement poller doesn't accidentally merge them until confirmed stable.

**Commit:** `feat: add weather_confirmed_entry_daemon`

---

## Task 6: Tests for daemon logic

**Files:**
- Create: `tests/unit_tests/examples/test_weather_confirmed_entry_daemon.py`

Test the non-network parts of the daemon:

```python
# test_weather_confirmed_entry_daemon.py

import json
import tempfile
from datetime import datetime, UTC, date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# Import the daemon under test
import sys
sys.path.insert(0, str(Path(__file__).parents[3]))

from examples.live.polymarket.weather_confirmed_entry_daemon import (
    _next_poll_secs,
    _build_confirmed_entry_event,
)
from examples.live.polymarket.weather_confirmed_signal import ConfirmedSignal
from weather_wunderground_fetcher import StationObs


def _make_obs(city, daily_max, unit="C"):
    return StationObs(
        city=city, station="XXXX", daily_max=daily_max,
        unit=unit, obs_count=8, as_of_utc=datetime.now(UTC),
        oracle_type="wu", fetch_source="twc_historical",
    )


# --- Adaptive poll interval ---

def test_next_poll_secs_fast_when_near_threshold(monkeypatch):
    from dataclasses import dataclass
    @dataclass
    class FakeMarket:
        city = "Paris"
        threshold_f = 15.0
        band_type = "or_higher"
        observation_date = date.today()

    obs_map = {"Paris": _make_obs("Paris", 13.5, "C")}  # 1.5°C gap < 2.0
    secs = _next_poll_secs([FakeMarket()], obs_map)
    assert secs == 300.0


def test_next_poll_secs_slow_when_far_from_threshold():
    from dataclasses import dataclass
    @dataclass
    class FakeMarket:
        city = "NYC"
        threshold_f = 80.0
        band_type = "or_higher"
        observation_date = date.today()

    obs_map = {"NYC": _make_obs("NYC", 65.0, "F")}  # 15°F gap > 4
    secs = _next_poll_secs([FakeMarket()], obs_map)
    assert secs == 900.0


# --- JSONL event builder ---

def test_build_confirmed_entry_event_schema():
    from dataclasses import dataclass
    @dataclass
    class FakeMarket:
        slug = "highest-temperature-in-paris-on-2026-04-22-15c"
        city = "Paris"
        observation_date = date(2026, 4, 22)
        threshold_f = 15.0
        metric = "high"
        condition_id = "0xabc"
        band_type = "or_higher"

    sig = ConfirmedSignal(
        strategy="A1", market_slug=FakeMarket.slug, city="Paris",
        observation_date="2026-04-22", threshold_f=15.0, unit="C",
        token_id="yes-tok-123", token_side="yes",
        max_entry_price=0.97, preset_name="temp_confirmed_a1",
        arena="temp_confirmed", stop_loss_price=0.85, take_profit_price=0.99,
        wu_daily_max=16.2, wu_as_of_utc="2026-04-22T14:00:00+00:00",
    )

    event = _build_confirmed_entry_event(
        signal=sig, market=FakeMarket(), mid=0.94, shares=2.1277,
        stake=2.0, run_id="abc123", clob_response="{'success': True}",
        now_fn=lambda: datetime(2026, 4, 22, 14, 30, tzinfo=UTC),
    )

    assert event["event"] == "strategy_result"
    assert event["token_side"] == "yes"
    assert event["strategy_type"] == "A1"
    assert event["wu_daily_max"] == 16.2
    assert event["accounting_status"] == "open"
    assert event["stop_loss_price"] == 0.85
    assert "wu_as_of_utc" in event
    assert event["mode"] == "confirmed"
```

**Run:** `uv run --extra polymarket --with pytest --with pytest-asyncio python -m pytest tests/unit_tests/examples/test_weather_confirmed_entry_daemon.py --noconftest -v`
**Expected:** All tests pass.

**Commit:** `test: add confirmed entry daemon unit tests`

---

## Task 7: Wire settlement to pick up confirmed entries

The `weather_daily_temperature_settlement.py` already reads any file matching `weather_temp_live_*.jsonl` + `settlement_live.jsonl` + `take_profit.jsonl`. The confirmed daemon writes to `weather_confirmed_live_*.jsonl` — a different prefix.

**Files:**
- Modify: `examples/live/polymarket/weather_daily_temperature_settlement.py`

In `_live_jsonl_files()`, add `weather_confirmed_live_*.jsonl`:

```python
def _live_jsonl_files(jsonl_dir: Path) -> list[Path]:
    files = sorted(jsonl_dir.glob("weather_temp_live_*.jsonl"))
    files += sorted(jsonl_dir.glob("weather_confirmed_live_*.jsonl"))
    for extra_name in ("settlement_live.jsonl", "take_profit.jsonl"):
        extra_file = jsonl_dir / extra_name
        if extra_file.exists():
            files.append(extra_file)
    return files
```

Also update `weather_daily_temperature_take_profit.py` `load_open_positions()` to scan `weather_confirmed_live_*.jsonl` as well:

```python
live_files: list[Path] = sorted(jsonl_dir.glob("weather_temp_live_*.jsonl"))
live_files += sorted(jsonl_dir.glob("weather_confirmed_live_*.jsonl"))
for extra in ("settlement_live.jsonl", "take_profit.jsonl"):
    ...
```

**Tests:** Add one test to `test_weather_confirmed_entry_daemon.py` confirming `_live_jsonl_files` includes confirmed files (or add to existing settlement test file if it exists).

**Commit:** `feat: wire settlement + take-profit watcher to confirmed entry files`

---

## Task 8: Docker compose service

**Files:**
- Modify: `.docker/docker-compose.yml`

Add after the `weather-live-daemon-vpn` service:

```yaml
  weather-confirmed-entry-vpn:
    image: nautilus-recorder:latest
    container_name: nautilus-weather-confirmed-vpn
    network_mode: "service:nordvpn"
    depends_on:
      nordvpn:
        condition: service_healthy
    restart: unless-stopped
    volumes:
      - ../nautilus:/workspace/nautilus
    working_dir: /workspace/nautilus
    environment:
      - POLYMARKET_PRIVATE_KEY=${POLYMARKET_PRIVATE_KEY}
      - POLYMARKET_FUNDER_ADDRESS=${POLYMARKET_FUNDER_ADDRESS}
      - POLYMARKET_SIGNATURE_TYPE=${POLYMARKET_SIGNATURE_TYPE:-0}
      - POLYMARKET_CLOB_HOST=${POLYMARKET_CLOB_HOST:-https://clob.polymarket.com}
      - POLYMARKET_CLOB_API_KEY=${POLYMARKET_CLOB_API_KEY:-}
      - POLYMARKET_CLOB_API_SECRET=${POLYMARKET_CLOB_API_SECRET:-}
      - POLYMARKET_CLOB_PASSPHRASE=${POLYMARKET_CLOB_PASSPHRASE:-}
      - TWC_API_KEY=${TWC_API_KEY:-}
    command: >
      python3 examples/live/polymarket/weather_confirmed_entry_daemon.py
      --budget 20
    profiles:
      - vpn
```

**Note:** Budget of $20/day separate from the main live daemon's $50 budget. Both can run simultaneously.

**Commit:** `feat: add weather-confirmed-entry-vpn docker service`

---

## Task 9: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

Add to the Docker services table:

```markdown
| `weather-confirmed-entry-vpn` | `nautilus-weather-confirmed-vpn` | `nautilus-recorder:latest` | Confirmed-entry daemon (WU signal, $20 budget) |
```

Add to the Settlement Architecture section:

```
Output files: weather_confirmed_live_*.jsonl
Picked up by: settlement poller + take-profit watcher automatically
Strategies: A1 (or_higher confirmed YES), A2 (exact band confirmed NO), B2 (late-day NO)
```

**Commit:** `docs: update CLAUDE.md with confirmed entry daemon`

---

## Pre-Deploy Checklist (execute before starting container)

Run these from inside the live daemon container:

```bash
# 1. Verify WU station smoke-test (all 50 cities)
docker exec nautilus-weather-live-daemon-vpn python3 -c "
import asyncio
from examples.live.polymarket.weather_wunderground_fetcher import fetch_daily_high, CITY_STATIONS
async def check():
    results = await asyncio.gather(*[fetch_daily_high(c) for c in list(CITY_STATIONS)[:10]])
    for r in results:
        if r: print(f'{r.city}: {r.daily_max}{r.unit}  as_of={r.as_of_utc}')
asyncio.run(check())
"

# 2. Verify signal evaluator dry-run (no orders)
docker exec nautilus-weather-live-daemon-vpn python3 \
  examples/live/polymarket/weather_confirmed_entry_daemon.py \
  --dry-run --max-rounds 1

# 3. Confirm settlement poller picks up confirmed files
docker exec nautilus-weather-settlement-vpn python3 -c "
from pathlib import Path
from examples.live.polymarket.weather_daily_temperature_settlement import _live_jsonl_files
files = _live_jsonl_files(Path('/workspace/nautilus/outputs/polymarket/runs'))
print([f.name for f in files])
"
```

---

## Summary

| Task | File | Status |
|------|------|--------|
| 1 | `weather_confirmed_signal.py` (core evaluators) | TODO |
| 2 | `test_weather_confirmed_signal.py` | TODO |
| 3 | `weather_confirmed_signal.py` + `build_signal` | TODO |
| 4 | `weather_confirmed_signal.py` + `ConfirmTracker` | TODO |
| 5 | `weather_confirmed_entry_daemon.py` | TODO |
| 6 | `test_weather_confirmed_entry_daemon.py` | TODO |
| 7 | Settlement + take-profit watcher wiring | TODO |
| 8 | `docker-compose.yml` service | TODO |
| 9 | `CLAUDE.md` docs | TODO |
