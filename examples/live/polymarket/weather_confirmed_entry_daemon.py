#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
# -------------------------------------------------------------------------------------------------
"""
Confirmed-entry daemon for Polymarket weather daily temperature markets.

Runs Weather Underground live data through three confirmed-signal strategies (A1/A2/B2)
that only enter trades when the daily high temperature is confirmed or near-certain.
Uses py_clob_client for direct CLOB order submission (no TradingNode required).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

# py_clob_client uses a module-level httpx.Client(http2=True) singleton that is NOT thread-safe
# under concurrent asyncio.to_thread usage. Replace with HTTP/1.1 (thread-safe).
import py_clob_client.http_helpers.helpers as _poly_helpers
import httpx as _httpx
_poly_helpers._http_client = _httpx.Client(http2=False)
del _poly_helpers, _httpx

from examples.live.polymarket.polymarket_weather_daily_temperature_live_daemon import (
    _build_clob_client_for_entry,
    _already_entered_today,
    _session_trading_day,
    _city_local_date,
    _ensure_clob_credentials,
    _backoff_delay,
    SESSION_END_HOUR_UTC,
    _CITY_TIMEZONES,
)
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import (
    _default_resolve_markets,
    JsonlRunWriter,
    build_output_path,
    DEFAULT_OUTPUT_DIR,
)
from examples.live.polymarket.weather_confirmed_signal import (
    ConfirmTracker,
    SAFETY_MARGIN_C,
    SAFETY_MARGIN_F,
    build_signal,
    HKO_CITIES,
)
from examples.live.polymarket.weather_wunderground_fetcher import (
    fetch_daily_high,
    CITY_STATIONS,
    StationObs,
)

_log = __import__("logging").getLogger(__name__)

# Per-trade target USD
TARGET_USD = Decimal("2")


def _next_poll_secs(
    markets: list,
    latest_obs: dict[str, StationObs],
) -> float:
    """Return poll interval based on proximity to thresholds.

    Returns 300.0 if any market has a city with abs(obs.daily_max - market.threshold_f) <= 2.0°C / 4.0°F.
    Returns 900.0 otherwise.
    """
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
        "real_order": True,  # distinguishes real CLOB fills from paper sandbox entries
    }


async def _run_poll_cycle(
    *,
    markets: list,
    writer: JsonlRunWriter,
    run_id: str,
    now_fn: Callable[[], datetime],
    budget_remaining: Decimal,
    dry_run: bool,
    confirm_tracker: ConfirmTracker,
    latest_obs: dict[str, StationObs],
    entered_this_run: set[tuple[str, str]],
    output_dir: Path,
    session_trading_day,
) -> tuple[Decimal, float]:
    """Core poll cycle: fetch obs, evaluate signals, submit orders.

    Temperature-ladder strategy:
    - One fetch per unique city (not per market) — avoids redundant API calls.
    - Markets processed highest threshold first per city (intra-city sort only).
    - A1 (or_higher YES): once entered for a city this cycle, lower thresholds skipped.
    - B2 (or_higher NO): suppressed if daily_max >= threshold (temperature already reached it).

    Returns (budget_remaining, next_poll_secs) computed from fresh observations.
    """
    import httpx as _httpx
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    clob_host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")

    try:
        clob_client = await asyncio.to_thread(_build_clob_client_for_entry)
    except Exception as exc:
        _log.error("Failed to build CLOB client: %s", exc)
        next_poll_secs = _next_poll_secs(markets, latest_obs)
        return budget_remaining, next_poll_secs

    # --- Phase 1: Pre-fetch observations — one fetch per unique city ---
    unique_cities = {m.city for m in markets}
    # Snapshot prev daily_max values before updating latest_obs this cycle
    prev_obs: dict[str, float | None] = {
        city: (latest_obs[city].daily_max if city in latest_obs else None)
        for city in unique_cities
    }
    # Track which cities received a fresh observation this cycle.
    # Cities that fail (exception or None) are excluded from Phase 2 to prevent
    # stale cached observations from advancing ConfirmTracker counts.
    cities_with_fresh_obs: set[str] = set()
    for city in unique_cities:
        try:
            obs = await fetch_daily_high(city)
        except Exception as exc:
            _log.warning("fetch_daily_high failed for %s: %s", city, exc)
            continue
        if obs is None:
            _log.warning("No observation available for %s", city)
            continue
        latest_obs[city] = obs
        cities_with_fresh_obs.add(city)

    # Compute next_poll_secs from the freshly-updated latest_obs so that
    # the first-cycle cadence reflects real data, not pre-fetch stale state.
    next_poll_secs = _next_poll_secs(markets, latest_obs)

    # --- Phase 2: Process markets — highest threshold first within each city ---
    # Intra-city sort only: sort descending threshold within each city's slice
    # while preserving the original cross-city ordering from the resolver.
    # This ensures the A1 ladder enters the most-confirmed (highest) rung first
    # without unexpectedly changing cross-city budget priority.
    city_order: list[str] = []
    seen_cities: set[str] = set()
    for m in markets:
        if m.city not in seen_cities:
            city_order.append(m.city)
            seen_cities.add(m.city)
    city_rank = {city: i for i, city in enumerate(city_order)}
    sorted_markets = sorted(markets, key=lambda m: (city_rank[m.city], -m.threshold_f))

    # Per-cycle set: cities for which an A1 entry has already been made this cycle.
    # Latched only after a candidate passes all gating checks AND order submission
    # succeeds (or dry_run is accepted), so a higher rung that fails gating does
    # not suppress eligible lower rungs.
    city_a1_entered: set[str] = set()

    async with _httpx.AsyncClient(timeout=10.0) as http:
        for market in sorted_markets:
            if budget_remaining <= Decimal("0"):
                break

            city = market.city

            # Skip cities that did not receive a fresh observation this cycle.
            # This prevents stale cached observations from advancing ConfirmTracker.
            if city not in cities_with_fresh_obs:
                _log.debug(
                    "SKIP %s  no fresh obs for %s this cycle (stale-fetch guard)",
                    market.slug, city,
                )
                continue

            city_info = CITY_STATIONS.get(city)
            if not city_info:
                _log.warning("No CITY_STATIONS entry for %s, skipping", city)
                continue

            _, _, unit, _ = city_info

            obs = latest_obs.get(city)
            if obs is None:
                continue

            safety_margin = SAFETY_MARGIN_F if unit == "F" else SAFETY_MARGIN_C

            # Temperature-ladder gate: once A1 has fired for a city this cycle,
            # skip lower or_higher thresholds — but still update confirm tracker.
            if city in city_a1_entered and market.band_type == "or_higher":
                _log.debug(
                    "SKIP %s  ladder: A1 already entered for %s at higher threshold this cycle",
                    market.slug, city,
                )
                a1_breach = obs.daily_max >= market.threshold_f + safety_margin
                confirm_tracker.record(market.slug, "A1", a1_breach)
                continue

            prev_max = prev_obs.get(city)

            # Compute confirmation counts for A1 and A2 independently
            a1_breach = obs.daily_max >= market.threshold_f + safety_margin
            confirm_tracker.record(market.slug, "A1", a1_breach)
            a1_count = confirm_tracker.get(market.slug, "A1")

            a2_breach = obs.daily_max > (market.threshold_f + 1.0) + safety_margin
            confirm_tracker.record(market.slug, "A2", a2_breach)
            a2_count = confirm_tracker.get(market.slug, "A2")

            confirm_counts = {"A1": a1_count, "A2": a2_count}

            city_tz = _CITY_TIMEZONES.get(city, "UTC")

            signal = build_signal(
                market,
                obs.daily_max,
                unit,
                obs.as_of_utc,
                confirm_counts,
                prev_max,
                now_fn(),
                city_tz,
            )

            if signal is None:
                continue

            # B2 NO gate: never short a threshold the temperature has already
            # reached or exceeded — it could still resolve YES from here.
            if signal.strategy == "B2" and obs.daily_max >= market.threshold_f:
                _log.info(
                    "SKIP %s [B2-gate]  daily_max=%.2f >= threshold=%.2f (reachable)",
                    market.slug, obs.daily_max, market.threshold_f,
                )
                continue

            # Skip if already entered this session (main loop pre-filters, but guard here too)
            if (market.slug, signal.token_side) in entered_this_run:
                continue

            # Fetch CLOB mid
            try:
                resp = await http.get(
                    f"{clob_host}/midpoint",
                    params={"token_id": signal.token_id},
                )
                resp.raise_for_status()
                mid = float(resp.json().get("mid", 0) or 0)
            except Exception as exc:
                _log.warning("CLOB midpoint fetch failed for %s: %s", market.slug, exc)
                continue

            # Skip if price at extremes (no edge)
            if mid >= 0.98 or mid <= 0.02:
                _log.info("SKIP %s [%s]  mid=%.4f (at extreme)", market.slug, signal.token_side, mid)
                continue

            # Skip if already priced in
            if mid > signal.max_entry_price:
                _log.info("SKIP %s [%s]  mid=%.4f > max=%.2f",
                         market.slug, signal.token_side, mid, signal.max_entry_price)
                continue

            # Compute shares and stake
            raw_shares = TARGET_USD / Decimal(str(mid))
            shares = raw_shares.quantize(Decimal("0.0001"))
            stake = (shares * Decimal(str(mid))).quantize(Decimal("0.0001"))

            if budget_remaining < stake:
                _log.info("SKIP %s [%s]  stake=%.4f > remaining=%.4f",
                         market.slug, signal.token_side, float(stake), float(budget_remaining))
                continue

            # Log entry
            _log.info(
                "CONFIRMED ENTER [%s] %s  wu_max=%.2f  threshold=%.2f  mid=%.4f",
                signal.strategy,
                market.slug,
                signal.wu_daily_max,
                signal.threshold_f,
                mid,
            )

            # Submit order (unless dry_run)
            clob_response = None
            if not dry_run:
                try:
                    order_args = MarketOrderArgs(
                        token_id=signal.token_id,
                        amount=float(shares),
                        side=BUY,
                    )
                    signed_order = await asyncio.to_thread(
                        clob_client.create_market_order,
                        order_args,
                    )
                    clob_response = await asyncio.to_thread(
                        clob_client.post_order,
                        signed_order,
                        OrderType.FOK,
                    )
                    _log.info("ORDER resp: %s", clob_response)
                except Exception as exc:
                    _log.error("BUY FAILED for %s [%s]: %s", market.slug, signal.token_side, exc)
                    continue

            # A1 ladder: latch city only after all gating checks pass and order
            # submission succeeds (or dry_run).  This prevents a higher rung that
            # fails quote/price/budget gating from suppressing eligible lower rungs.
            if signal.strategy == "A1":
                city_a1_entered.add(city)

            # Write event
            writer.write(
                _build_confirmed_entry_event(
                    signal=signal,
                    market=market,
                    mid=mid,
                    shares=shares,
                    stake=stake,
                    run_id=run_id,
                    clob_response=clob_response,
                    now=now_fn(),
                )
            )

            # Clear tracker and record entry
            confirm_tracker.clear_slug(market.slug)
            entered_this_run.add((market.slug, signal.token_side))
            budget_remaining -= stake

    return budget_remaining, next_poll_secs


async def _run_main_loop(
    *,
    output_dir: str | Path,
    budget_usd: float,
    dry_run: bool,
    max_rounds: int,
) -> None:
    """Main event loop for confirmed entry daemon."""
    _ensure_clob_credentials()

    output_dir = Path(output_dir)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    runs_dir = output_dir / "polymarket" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    output_path = runs_dir / f"weather_confirmed_live_{ts}.jsonl"

    writer = JsonlRunWriter(output_path)
    now_fn = lambda: datetime.now(tz=UTC)
    budget = Decimal(str(budget_usd))
    confirm_tracker = ConfirmTracker()
    latest_obs: dict[str, StationObs] = {}
    entered_this_session: set[tuple[str, str]] = set()
    rounds = 0

    while max_rounds <= 0 or rounds < max_rounds:
        run_id = uuid.uuid4().hex
        started_at = now_fn()
        session_trading_day = _session_trading_day(started_at)

        # Resolve markets
        try:
            markets = await _default_resolve_markets()
        except Exception as exc:
            _log.error("Market resolution failed: %s", exc)
            await asyncio.sleep(60)
            continue

        # Filter to markets whose observation_date matches city local date
        tradeable = [
            m for m in markets
            if m.observation_date == _city_local_date(m.city)
        ]

        # Exclude already entered this trading day
        already_entered_slugs = _already_entered_today(output_dir, session_trading_day)
        tradeable = [m for m in tradeable if m.slug not in already_entered_slugs]

        # Exclude already entered this session
        tradeable = [
            m for m in tradeable
            if not any((m.slug, side) in entered_this_session for side in ("yes", "no"))
        ]

        # poll_secs is computed inside _run_poll_cycle from freshly-fetched obs.
        # We default to 900.0 for the poll_cycle event timestamp; the actual sleep
        # uses the value returned by _run_poll_cycle so the first cycle reflects
        # real data, not stale state from before the fetch.
        poll_secs = 900.0

        # Write poll_cycle event (poll_interval_secs updated below after cycle)
        writer.write(
            {
                "run_id": run_id,
                "event": "poll_cycle",
                "tradeable_markets_count": len(tradeable),
                "poll_interval_secs": poll_secs,
                "session_trading_day": str(session_trading_day),
                "dry_run": dry_run,
                "timestamp": started_at.isoformat(),
            }
        )

        # Run poll cycle if there are tradeable markets
        if tradeable:
            budget, poll_secs = await _run_poll_cycle(
                markets=tradeable,
                writer=writer,
                run_id=run_id,
                now_fn=now_fn,
                budget_remaining=budget,
                dry_run=dry_run,
                confirm_tracker=confirm_tracker,
                latest_obs=latest_obs,
                entered_this_run=entered_this_session,
                output_dir=output_dir,
                session_trading_day=session_trading_day,
            )

        rounds += 1
        _log.info(
            "Poll cycle %d done. budget_remaining=%.2f  sleeping=%.0fs",
            rounds,
            float(budget),
            poll_secs,
        )
        await asyncio.sleep(poll_secs)


def main() -> int:
    """Entry point for confirmed entry daemon."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Confirmed-entry daemon for Polymarket weather temperature markets"
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Base output directory for JSONL files",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=20.0,
        help="Daily USD budget for confirmed entries",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate signals but skip order submission",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=0,
        help="Max poll cycles (0=infinite)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(
            _run_main_loop(
                output_dir=args.output_dir,
                budget_usd=args.budget,
                dry_run=args.dry_run,
                max_rounds=args.max_rounds,
            )
        )
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
