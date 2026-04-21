#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
# -------------------------------------------------------------------------------------------------
"""
Long-running Polymarket weather daily-temperature live-trading daemon.

This daemon keeps the weather strategy logic and bankroll controls from the paper
runner, but routes execution through Nautilus' real Polymarket live execution
client. Use with care: this path is intended for real-money orders.
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
import signal
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

# py_clob_client uses a module-level httpx.Client(http2=True) singleton that is NOT thread-safe
# under concurrent asyncio.to_thread usage. The h2 state machine corrupts when multiple threads
# access it simultaneously, causing create_market_order to hang indefinitely.
# Replace with HTTP/1.1 (thread-safe connection pool) before any threads are spawned.
import py_clob_client.http_helpers.helpers as _poly_helpers  # noqa: E402
import httpx as _httpx  # noqa: E402
_poly_helpers._http_client = _httpx.Client(http2=False)
del _poly_helpers, _httpx

from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import DEFAULT_CACHE_HOST
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import DEFAULT_CACHE_PORT
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import DEFAULT_OUTPUT_DIR
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import JsonlRunWriter
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import RecoverableDaemonError
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import _build_instrument_id
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import _build_parser
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import _default_resolve_markets
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import _as_decimal
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import _backoff_delay
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import _env_first
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import _iso8601_from_unix_nanos
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import _compute_total_deployed
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import _strategy_presets_for_set
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import build_output_path
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import extract_weather_strategy_results
from examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon import run_daemon
from examples.live.polymarket.weather_daily_temperature_live_strategy import WeatherDailyTemperaturePaperStrategy
from examples.live.polymarket.weather_daily_temperature_live_strategy import WeatherDailyTemperaturePaperStrategyConfig
from examples.live.polymarket.weather_daily_temperature_resolver import DailyTemperatureMarket
from examples.live.polymarket.weather_temperature_data_client import (
    WundergroundDataClientConfig,
    WundergroundDataClientFactory,
)
try:
    from examples.live.polymarket.weather_wunderground_fetcher import CITY_STATIONS as _CITY_STATIONS
except ModuleNotFoundError:
    import importlib.util as _ilu
    _wu_spec = _ilu.spec_from_file_location(
        "examples.live.polymarket.weather_wunderground_fetcher",
        Path(__file__).resolve().with_name("weather_wunderground_fetcher.py"),
    )
    _wu_mod = _ilu.module_from_spec(_wu_spec)  # type: ignore[arg-type]
    _wu_spec.loader.exec_module(_wu_mod)  # type: ignore[union-attr]
    _CITY_STATIONS = _wu_mod.CITY_STATIONS
from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket import PolymarketLiveDataClientFactory
from nautilus_trader.adapters.polymarket import PolymarketLiveExecClientFactory
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig
from nautilus_trader.config import CacheConfig
from nautilus_trader.config import DatabaseConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import MessageBusConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.config import PortfolioConfig

_log = __import__("logging").getLogger(__name__)

# City → IANA timezone name.  Used to compute each city's local calendar date
# so we only trade markets whose observation_date matches the city's current
# local date, not the server's UTC date.
_CITY_TIMEZONES: dict[str, str] = {
    "NYC": "America/New_York",
    "Chicago": "America/Chicago",
    "Miami": "America/New_York",
    "Los Angeles": "America/Los_Angeles",
    "San Francisco": "America/Los_Angeles",
    "Seattle": "America/Los_Angeles",
    "Denver": "America/Denver",
    "Houston": "America/Chicago",
    "Dallas": "America/Chicago",
    "Austin": "America/Chicago",
    "Atlanta": "America/New_York",
    "London": "Europe/London",
    "Paris": "Europe/Paris",
    "Madrid": "Europe/Madrid",
    "Amsterdam": "Europe/Amsterdam",
    "Munich": "Europe/Berlin",
    "Milan": "Europe/Rome",
    "Warsaw": "Europe/Warsaw",
    "Helsinki": "Europe/Helsinki",
    "Ankara": "Europe/Istanbul",
    "Istanbul": "Europe/Istanbul",
    "Tokyo": "Asia/Tokyo",
    "Seoul": "Asia/Seoul",
    "Busan": "Asia/Seoul",
    "Taipei": "Asia/Taipei",
    "Singapore": "Asia/Singapore",
    "Kuala Lumpur": "Asia/Kuala_Lumpur",
    "Jakarta": "Asia/Jakarta",
    "Manila": "Asia/Manila",
    "Beijing": "Asia/Shanghai",
    "Shanghai": "Asia/Shanghai",
    "Shenzhen": "Asia/Shanghai",
    "Guangzhou": "Asia/Shanghai",
    "Chongqing": "Asia/Shanghai",
    "Chengdu": "Asia/Shanghai",
    "Wuhan": "Asia/Shanghai",
    "Hong Kong": "Asia/Hong_Kong",
    "Lucknow": "Asia/Kolkata",
    "Karachi": "Asia/Karachi",
    "Jeddah": "Asia/Riyadh",
    "Lagos": "Africa/Lagos",
    "Cape Town": "Africa/Johannesburg",
    "Buenos Aires": "America/Argentina/Buenos_Aires",
    "Sao Paulo": "America/Sao_Paulo",
    "Mexico City": "America/Mexico_City",
    "Toronto": "America/Toronto",
    "Panama City": "America/Panama",
    "Wellington": "Pacific/Auckland",
    "Moscow": "Europe/Moscow",
    "Tel Aviv": "Asia/Jerusalem",
}


def _city_local_date(city: str) -> "date":
    """Return the current calendar date at the city's local timezone.

    Falls back to UTC date if the city is not in _CITY_TIMEZONES.
    """
    import zoneinfo
    from datetime import date as _date

    tz_name = _CITY_TIMEZONES.get(city)
    if tz_name is None:
        return _date.today()
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        return _date.today()
    return datetime.now(tz).date()


def _session_trading_day(now: datetime) -> "date":
    """Return the trading day that *now* belongs to.

    Sessions run from SESSION_END_HOUR_UTC:00 UTC to SESSION_END_HOUR_UTC:00 UTC
    the following calendar day.  A timestamp before SESSION_END_HOUR_UTC:00 UTC
    belongs to the *previous* calendar day's session — the session started
    yesterday morning and has not yet ended because US markets are still live.

    Examples (SESSION_END_HOUR_UTC=9):
      2026-04-21 09:00 UTC → 2026-04-21  (new session just opened)
      2026-04-22 02:00 UTC → 2026-04-21  (same session; LA resolves at 07:00 UTC)
      2026-04-22 09:00 UTC → 2026-04-22  (next session begins)
    """
    from datetime import timedelta as _timedelta

    if now.hour < SESSION_END_HOUR_UTC:
        return (now - _timedelta(days=1)).date()
    return now.date()


def _ensure_clob_credentials() -> None:
    """Derive CLOB API credentials from the wallet private key if not already set.

    Polymarket issues deterministic L2 credentials (api_key/secret/passphrase)
    from a wallet private key via `create_or_derive_api_creds()`.  This function
    derives and injects those credentials into the process environment so that
    downstream Nautilus factory code can find them via the standard env-var names.
    """
    has_key = bool(_env_first("POLYMARKET_CLOB_API_KEY", "POLYMARKET_API_KEY"))
    has_secret = bool(_env_first("POLYMARKET_CLOB_API_SECRET", "POLYMARKET_API_SECRET"))
    has_pass = bool(_env_first("POLYMARKET_CLOB_PASSPHRASE", "POLYMARKET_PASSPHRASE"))
    if has_key and has_secret and has_pass:
        return  # already configured

    private_key = _env_first("POLYMARKET_PRIVATE_KEY", "POLYMARKET_PK")
    funder = _env_first("POLYMARKET_FUNDER_ADDRESS", "POLYMARKET_FUNDER")
    if not private_key or not funder:
        raise RuntimeError(
            "Cannot derive CLOB credentials: POLYMARKET_PRIVATE_KEY and "
            "POLYMARKET_FUNDER_ADDRESS are required."
        )

    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

    base_url = _env_first("POLYMARKET_CLOB_HOST") or "https://clob.polymarket.com"
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
    _log.info("Deriving CLOB API credentials from private key (one-time network call)...")
    client = ClobClient(
        base_url,
        chain_id=POLYGON,
        signature_type=signature_type,
        key=private_key,
        funder=funder,
    )
    creds = client.create_or_derive_api_creds()
    os.environ["POLYMARKET_API_KEY"] = creds.api_key
    os.environ["POLYMARKET_API_SECRET"] = creds.api_secret
    os.environ["POLYMARKET_PASSPHRASE"] = creds.api_passphrase
    _log.info("CLOB credentials derived and set in environment.")


def build_live_daemon_node_config(
    *,
    instrument_ids: list[str],
    trader_id: str,
    cache_host: str,
    cache_port: int,
) -> TradingNodeConfig:
    instrument_provider_config = PolymarketInstrumentProviderConfig(
        load_ids=frozenset(instrument_ids),
        use_gamma_markets=True,  # Use Gamma API batched calls (~6×100) vs CLOB bulk pagination (500k+ markets)
    )
    return TradingNodeConfig(
        trader_id=TraderId(trader_id),
        logging=LoggingConfig(log_level="INFO", use_pyo3=True),
        exec_engine=LiveExecEngineConfig(
            reconciliation=False,
            open_check_interval_secs=5.0,
            open_check_open_only=True,
            snapshot_orders=True,
            snapshot_positions=True,
            snapshot_positions_interval_secs=5.0,
            graceful_shutdown_on_exception=True,
            allow_overfills=True,  # Polymarket FOK fills return fractional share differences due to USDC→shares rounding
        ),
        cache=CacheConfig(
            database=DatabaseConfig(host=cache_host, port=cache_port),
            timestamps_as_iso8601=True,
            persist_account_events=True,
            buffer_interval_ms=100,
            flush_on_start=False,
            use_instance_id=True,
        ),
        message_bus=MessageBusConfig(
            database=DatabaseConfig(host=cache_host, port=cache_port),
            timestamps_as_iso8601=True,
            buffer_interval_ms=100,
            streams_prefix="polymarket-weather-live",
            use_trader_prefix=True,
            use_trader_id=True,
            use_instance_id=True,
            stream_per_topic=False,
            autotrim_mins=60,
            heartbeat_interval_secs=1,
        ),
        portfolio=PortfolioConfig(min_account_state_logging_interval_ms=1_000),
        data_clients={
            POLYMARKET: PolymarketDataClientConfig(
                instrument_config=instrument_provider_config,
                private_key=_env_first("POLYMARKET_PRIVATE_KEY", "POLYMARKET_PK"),
                signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
                funder=_env_first("POLYMARKET_FUNDER_ADDRESS", "POLYMARKET_FUNDER"),
                api_key=_env_first("POLYMARKET_CLOB_API_KEY", "POLYMARKET_API_KEY"),
                api_secret=_env_first("POLYMARKET_CLOB_API_SECRET", "POLYMARKET_API_SECRET"),
                passphrase=_env_first("POLYMARKET_CLOB_PASSPHRASE", "POLYMARKET_PASSPHRASE"),
                base_url_http=_env_first("POLYMARKET_CLOB_HOST"),
                update_instruments_interval_mins=None,  # Disable mid-session refresh; each session reloads at startup
                ws_connection_initial_delay_secs=1.0,  # Reduced from 5s default; all 539 subscriptions queue in <100ms
                ws_max_subscriptions_per_connection=500,  # Polymarket's actual limit; 539 markets → 2 connections vs default 3
            ),
            "WEATHER": WundergroundDataClientConfig(
                poll_interval_secs=900,
                cities=(),  # poll all 50 cities
            ),
        },
        exec_clients={
            POLYMARKET: PolymarketExecClientConfig(
                instrument_config=instrument_provider_config,
                private_key=_env_first("POLYMARKET_PRIVATE_KEY", "POLYMARKET_PK"),
                signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
                funder=_env_first("POLYMARKET_FUNDER_ADDRESS", "POLYMARKET_FUNDER"),
                api_key=_env_first("POLYMARKET_CLOB_API_KEY", "POLYMARKET_API_KEY"),
                api_secret=_env_first("POLYMARKET_CLOB_API_SECRET", "POLYMARKET_API_SECRET"),
                passphrase=_env_first("POLYMARKET_CLOB_PASSPHRASE", "POLYMARKET_PASSPHRASE"),
                base_url_http=_env_first("POLYMARKET_CLOB_HOST"),
                generate_order_history_from_trades=False,
            ),
        },
        timeout_connection=300.0,  # Instrument bulk-load paginates all Polymarket markets (~140k+, ~100-150s); wait for it before starting strategies
        timeout_reconciliation=10.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )


# How often to scan the cache for newly-opened positions and flush them to JSONL.
_POSITION_POLL_SECS = 30.0

# Pause between the end of one session and the start of the next (market refresh).
_RESTART_PAUSE_SECS = 10.0

# Sessions run from SESSION_END_HOUR_UTC:00 UTC to SESSION_END_HOUR_UTC:00 UTC the next day.
# Chosen so that all known city markets resolve before the session ends:
#   Latest city: America/Los_Angeles, midnight = 08:00 UTC in PST winter (UTC-8).
#   09:00 UTC gives a 1-hour buffer even in the worst-case winter offset.
# If a new city is added whose local midnight exceeds 08:00 UTC, raise this value.
SESSION_END_HOUR_UTC: int = 9


def _build_node_and_strategies(
    *,
    markets: list[DailyTemperatureMarket],
    preset_set: str,
    monitoring_markets: list[DailyTemperatureMarket] = (),
    session_stake_cap: Decimal | None = None,
) -> tuple[TradingNode, tuple, dict[str, StrategyId]]:
    """Create a configured TradingNode with strategies attached. Does not call build()."""
    presets = _strategy_presets_for_set(preset_set)
    all_markets_for_instruments = list(markets) + list(monitoring_markets)
    has_no_presets = any(getattr(p, "token_side", "yes") == "no" for p in presets)
    instrument_ids = [_build_instrument_id(market, "yes") for market in all_markets_for_instruments]
    if has_no_presets:
        instrument_ids += [_build_instrument_id(market, "no") for market in all_markets_for_instruments]
    config = build_live_daemon_node_config(
        instrument_ids=instrument_ids,
        trader_id="LIVE-WEATHER-DAEMON",
        cache_host=os.getenv("NAUTILUS_CACHE_HOST", DEFAULT_CACHE_HOST),
        cache_port=int(os.getenv("NAUTILUS_CACHE_PORT", str(DEFAULT_CACHE_PORT))),
    )
    node = TradingNode(config=config)
    strategy_ids_by_key: dict[str, StrategyId] = {}

    families: dict[tuple[str, str, str], list[InstrumentId]] = {}
    for market in all_markets_for_instruments:
        key = (market.city, str(market.observation_date), market.metric)
        families.setdefault(key, []).append(InstrumentId.from_str(_build_instrument_id(market, "yes")))

    family_ids_by_slug: dict[str, tuple[InstrumentId, ...]] = {
        market.slug: tuple(families.get((market.city, str(market.observation_date), market.metric), []))
        for market in all_markets_for_instruments
    }

    live_mode = str(preset_set).strip().lower() in {"live_90_basic", "live-weather-v1"}
    # Cap this session's open stake at the remaining budget (never exceed $50 default either).
    # At $2/market × 25 positions = $50 max per day.
    if live_mode:
        default_cap = Decimal("50")
        open_stake_cap = min(default_cap, session_stake_cap) if session_stake_cap is not None else default_cap
    else:
        open_stake_cap = None

    for market in markets:
        for preset in presets:
            side = getattr(preset, "token_side", "yes")
            inst_id_str = _build_instrument_id(market, side)
            strategy_key = f"{market.slug}:{preset.name}"
            strategy = WeatherDailyTemperaturePaperStrategy(
                config=WeatherDailyTemperaturePaperStrategyConfig(
                    strategy_id=f"LIVE-WTHR-{preset.name.upper()}",
                    instrument_id=InstrumentId.from_str(inst_id_str),
                    preset=preset,
                    order_qty=Decimal(str(preset.order_qty)),
                    token_side=side,
                    family_instrument_ids=family_ids_by_slug.get(market.slug, ()),
                    target_usd_per_market=(Decimal("2") if live_mode else None),
                    min_order_size_shares=(Decimal("2") if live_mode else Decimal("0")),
                    max_stake_per_market=(Decimal("2.10") if live_mode else None),
                    max_open_positions=(25 if live_mode else None),
                    max_total_open_stake=open_stake_cap,
                    city=market.city,
                    threshold=float(market.threshold_f),
                    threshold_unit=(_CITY_STATIONS.get(market.city, ("", "", "C", ""))[2]),
                    band_type=market.band_type,
                ),
            )
            node.trader.add_strategy(strategy)
            strategy_ids_by_key[strategy_key] = strategy.id

    for market in monitoring_markets:
        for preset in presets:
            side = getattr(preset, "token_side", "yes")
            inst_id_str = _build_instrument_id(market, side)
            strategy_key = f"{market.slug}:{preset.name}:monitor"
            strategy = WeatherDailyTemperaturePaperStrategy(
                config=WeatherDailyTemperaturePaperStrategyConfig(
                    strategy_id=f"LIVE-WTHR-MON-{preset.name.upper()}",
                    instrument_id=InstrumentId.from_str(inst_id_str),
                    preset=preset,
                    order_qty=Decimal("0"),
                    token_side=side,
                    skip_entry=True,
                ),
            )
            node.trader.add_strategy(strategy)
            strategy_ids_by_key[strategy_key] = strategy.id

    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    node.add_data_client_factory("WEATHER", WundergroundDataClientFactory)
    node.add_exec_client_factory(POLYMARKET, PolymarketLiveExecClientFactory)
    return node, presets, strategy_ids_by_key


def _flush_new_positions(
    *,
    node: TradingNode,
    markets: list[DailyTemperatureMarket],
    presets: tuple,
    strategy_ids_by_key: dict[str, StrategyId],
    seen_position_ids: set[str],
    writer: JsonlRunWriter,
    run_id: str,
    now_fn: Callable[[], datetime],
) -> int:
    """Write any newly-opened positions to JSONL. Returns count written."""
    written = 0
    for market in markets:
        for preset in presets:
            side = getattr(preset, "token_side", "yes")
            instrument_id = InstrumentId.from_str(_build_instrument_id(market, side))
            strategy_key = f"{market.slug}:{preset.name}"
            strategy_id = strategy_ids_by_key.get(strategy_key)
            if strategy_id is None:
                continue
            try:
                open_positions = list(node.cache.positions_open(
                    instrument_id=instrument_id,
                    strategy_id=strategy_id,
                ))
            except Exception:
                continue
            for pos in open_positions:
                pid = str(getattr(pos, "id", id(pos)))
                if pid in seen_position_ids:
                    continue
                seen_position_ids.add(pid)
                shares = float(_as_decimal(getattr(pos, "peak_qty", None) or pos.quantity))
                entry_price = float(pos.avg_px_open)
                stake = float(Decimal(str(entry_price)) * Decimal(str(shares)))
                writer.write({
                    "run_id": run_id,
                    "event": "strategy_result",
                    "asset_class": "weather",
                    "weather_market_type": "daily_temperature",
                    "preset_name": preset.name,
                    "arena": preset.arena,
                    "mode": preset.mode,
                    "market_slug": market.slug,
                    "city": market.city,
                    "observation_date": str(market.observation_date),
                    "threshold_f": market.threshold_f,
                    "metric": market.metric,
                    "token_side": side,
                    "instrument_id": str(instrument_id),
                    "entry_price": entry_price,
                    "shares": shares,
                    "stake": stake,
                    "accounting_status": "open",
                    "resolved": False,
                    "exit_reason": "position_open",
                    "entry_time": _iso8601_from_unix_nanos(getattr(pos, "ts_opened", 0)),
                    "exit_time": None,
                    "pnl": None,
                    "stop_loss_price": preset.stop_loss_price,
                    "take_profit_price": preset.take_profit_price,
                    "timestamp": now_fn().isoformat(),
                })
                written += 1
    return written


def _flush_closed_positions(
    *,
    node: TradingNode,
    markets: list[DailyTemperatureMarket],
    presets: tuple,
    strategy_ids_by_key: dict[str, StrategyId],
    seen_closed_position_ids: set[str],
    writer: JsonlRunWriter,
    run_id: str,
    now_fn: Callable[[], datetime],
) -> int:
    """Write any newly-closed positions to JSONL. Returns count written."""
    written = 0
    for market in markets:
        for preset in presets:
            side = getattr(preset, "token_side", "yes")
            instrument_id = InstrumentId.from_str(_build_instrument_id(market, side))
            strategy_key = f"{market.slug}:{preset.name}"
            strategy_id = strategy_ids_by_key.get(strategy_key)
            if strategy_id is None:
                continue
            try:
                closed_positions = list(node.cache.positions_closed(
                    instrument_id=instrument_id,
                    strategy_id=strategy_id,
                ))
            except Exception:
                continue
            for pos in closed_positions:
                pid = str(getattr(pos, "id", id(pos)))
                if pid in seen_closed_position_ids:
                    continue
                seen_closed_position_ids.add(pid)
                # Determine exit reason: take_profit if positive P&L, else stop_loss
                realized_pnl = float(pos.realized_pnl or 0)
                exit_reason = "take_profit" if realized_pnl > 0 else "stop_loss"
                writer.write({
                    "run_id": run_id,
                    "event": "position_exit",
                    "asset_class": "weather",
                    "weather_market_type": "daily_temperature",
                    "preset_name": preset.name,
                    "arena": preset.arena,
                    "market_slug": market.slug,
                    "city": market.city,
                    "observation_date": str(market.observation_date),
                    "instrument_id": str(instrument_id),
                    "exit_reason": exit_reason,
                    "realized_pnl": realized_pnl,
                    "exit_time": _iso8601_from_unix_nanos(getattr(pos, "ts_closed", 0)),
                    "timestamp": now_fn().isoformat(),
                })
                written += 1
    return written


# How long node.build() (which loads instruments via CLOB HTTP) is allowed to
# block before we treat it as a hung session and retry.
_NODE_BUILD_TIMEOUT_SECS = 180


def _node_build_with_sigalrm(node: TradingNode) -> None:
    """Call node.build() with a SIGALRM-based hard timeout.

    node.build() makes synchronous HTTP calls to the Polymarket CLOB API
    (instrument loading) and can hang indefinitely if the API is slow or
    rate-limiting.  asyncio.wait_for() cannot interrupt it because it blocks
    the event loop.  SIGALRM fires even when the event loop is blocked,
    interrupting whatever blocking call is in-flight.
    """

    def _alarm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        raise RecoverableDaemonError(
            f"node.build() timed out after {_NODE_BUILD_TIMEOUT_SECS}s "
            "(CLOB instrument loading hung)"
        )

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(_NODE_BUILD_TIMEOUT_SECS)
    try:
        node.build()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


async def _node_stop_with_sigalrm(
    node: TradingNode,
    run_task: asyncio.Task,
) -> None:
    """Stop node.stop_async() with a SIGALRM-based hard timeout, then cancel run_task.

    If node.stop_async() blocks the event loop indefinitely, SIGALRM will
    fire and interrupt it. On any exception (including alarm), run_task is
    cancelled and we continue — the node is going down anyway.
    """
    _NODE_STOP_TIMEOUT_SECS = 45

    def _alarm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        raise RecoverableDaemonError(
            f"node.stop_async() timed out after {_NODE_STOP_TIMEOUT_SECS}s "
            "(event loop blocked or stop hung)"
        )

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(_NODE_STOP_TIMEOUT_SECS)
    try:
        await asyncio.wait_for(node.stop_async(), timeout=_NODE_STOP_TIMEOUT_SECS)
    except (asyncio.TimeoutError, RecoverableDaemonError, Exception):
        # Alarm fired, timeout fired, or stop_async raised. Either way, force cleanup.
        _log.warning("node.stop_async() did not complete; cancelling run_task")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        if not run_task.done():
            run_task.cancel()


def _already_entered_today(output_dir: Path, session_trading_day) -> set[str]:
    """Return market slugs that already have an open position entry for *session_trading_day*.

    Scans all ``*.jsonl`` files under ``<output_dir>/polymarket/runs/`` for
    ``strategy_result`` rows whose ``observation_date`` matches *session_trading_day*
    and whose ``accounting_status`` is ``"open"``.  The returned set of slugs is
    used to exclude already-entered markets from the current session so that a
    daemon restart does not double-buy a position.

    *session_trading_day* must be the value returned by ``_session_trading_day()``,
    NOT ``datetime.date.today()``.  The distinction matters after UTC midnight but
    before SESSION_END_HOUR_UTC:00 UTC, when the session is still tracking the
    previous calendar day's markets.
    """
    slugs: set[str] = set()
    runs_dir = Path(output_dir).resolve(strict=False) / "polymarket" / "runs"
    if not runs_dir.exists():
        return slugs
    target_date_str = str(session_trading_day)
    for jsonl_file in runs_dir.glob("*.jsonl"):
        with jsonl_file.open() as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if (
                    row.get("event") == "strategy_result"
                    and row.get("observation_date") == target_date_str
                    and row.get("accounting_status") == "open"
                ):
                    slugs.add(row["market_slug"])
    return slugs


def _build_clob_client_for_entry():
    """Build a py_clob_client ClobClient for order submission."""
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    from py_clob_client.clob_types import ApiCreds
    host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ["POLYMARKET_FUNDER_ADDRESS"]
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
    client = ClobClient(host, chain_id=POLYGON, key=pk, funder=funder, signature_type=sig_type)
    api_key = os.environ.get("POLYMARKET_CLOB_API_KEY", "")
    api_secret = os.environ.get("POLYMARKET_CLOB_API_SECRET", "")
    passphrase = os.environ.get("POLYMARKET_CLOB_PASSPHRASE", "")
    if not (api_key and api_secret and passphrase):
        creds = client.create_or_derive_api_creds()
        api_key, api_secret, passphrase = creds.api_key, creds.api_secret, creds.api_passphrase
    client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase))
    return client


def _token_id_from_market(market: "DailyTemperatureMarket") -> str:
    """Extract the YES token_id from a DailyTemperatureMarket's instrument-like data."""
    # instrument_id format: {condition_id}-{token_id}.POLYMARKET
    return market.yes_token_id if hasattr(market, "yes_token_id") else ""


async def _run_direct_clob_entry_session(
    *,
    markets: list["DailyTemperatureMarket"],
    writer: "JsonlRunWriter",
    run_id: str,
    now_fn: Callable[[], datetime],
    session_end_time: datetime,
    session_stake_cap: "Decimal | None" = None,
) -> None:
    """
    Enter qualifying markets directly via py_clob_client — no TradingNode required.

    Two passes are made per session:
      1. YES tokens whose CLOB mid is in [0.90, 0.99]  (temp_90c_basic)
      2. NO  tokens whose CLOB mid is in [0.90, 0.98]  (temp_90c_no_basic)

    For each qualifying market, submits a $2 FOK BUY market order and writes a
    strategy_result event.  After both passes, sleeps until session_end_time.

    This replaces _run_continuous_session for the live daemon because the Nautilus
    TradingNode.build() blocks the Rust event loop on futex calls that SIGALRM cannot
    interrupt, causing an unrecoverable hang when loading >~5 instruments.
    """
    import httpx as _httpx
    from decimal import Decimal as _Dec
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    TARGET_USD = _Dec("2")

    # (token_side, token_id_attr, min_ask, max_ask, tp_price, sl_price, preset_name, arena)
    ENTRY_PASSES = [
        ("yes", "yes_token_id", 0.90, 0.99, 0.99, 0.85, "temp_90c_basic",    "temp_90c"),
        ("no",  "no_token_id",  0.90, 0.98, 0.99, 0.85, "temp_90c_no_basic", "temp_90c_no"),
    ]

    clob_host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    budget_remaining = session_stake_cap if session_stake_cap is not None else _Dec("50")

    try:
        clob_client = await asyncio.to_thread(_build_clob_client_for_entry)
    except Exception as exc:
        _log.error("Failed to build CLOB client: %s", exc)
        raise RecoverableDaemonError(f"CLOB client build failed: {exc}") from exc

    # Track (slug, side) so a restart won't re-enter the same side twice.
    entered_this_session: set[tuple[str, str]] = set()

    async with _httpx.AsyncClient(timeout=10.0) as http:
        for (side, token_attr, min_ask, max_ask, tp_price, sl_price, preset_name, arena) in ENTRY_PASSES:
            if now_fn() >= session_end_time:
                break
            if budget_remaining <= _Dec("0"):
                _log.info("Session budget exhausted, stopping entries.")
                break

            _log.info("Entry pass: side=%s  band=[%.2f, %.2f]  preset=%s", side, min_ask, max_ask, preset_name)

            for market in markets:
                if now_fn() >= session_end_time:
                    break
                if budget_remaining <= _Dec("0"):
                    _log.info("Session budget exhausted, stopping entries.")
                    break

                slug = market.slug
                if (slug, side) in entered_this_session:
                    continue

                token_id = getattr(market, token_attr, "") or ""
                if not token_id:
                    _log.warning("No %s for %s — skipping", token_attr, slug)
                    continue

                # Fetch live mid from CLOB
                try:
                    resp = await http.get(f"{clob_host}/midpoint?token_id={token_id}")
                    mid = float(resp.json().get("mid", 0) or 0)
                except Exception as exc:
                    _log.warning("CLOB midpoint fetch failed for %s (%s): %s", slug, side, exc)
                    continue

                if not (min_ask <= mid <= max_ask):
                    _log.info("SKIP %s [%s]  mid=%.4f (outside [%.2f, %.2f])", slug, side, mid, min_ask, max_ask)
                    continue

                # Shares to buy = $2 / mid, rounded down to 4dp
                raw_shares = TARGET_USD / _Dec(str(mid))
                shares = raw_shares.quantize(_Dec("0.0001"))
                stake = (shares * _Dec(str(mid))).quantize(_Dec("0.0001"))

                if budget_remaining < stake:
                    _log.info("SKIP %s [%s]  stake=%.4f > remaining=%.4f", slug, side, float(stake), float(budget_remaining))
                    continue

                _log.info("ENTER %s [%s]  mid=%.4f  shares=%.4f  stake=%.4f", slug, side, mid, float(shares), float(stake))

                try:
                    order_args = MarketOrderArgs(token_id=token_id, amount=float(shares), side=BUY)
                    signed_order = await asyncio.to_thread(clob_client.create_market_order, order_args)
                    resp_order = await asyncio.to_thread(clob_client.post_order, signed_order, OrderType.FOK)
                    _log.info("ORDER resp: %s", resp_order)
                except Exception as exc:
                    _log.error("BUY FAILED for %s [%s]: %s", slug, side, exc)
                    continue

                # Build instrument_id string matching the Nautilus convention: {cond_id}-{token_id}.POLYMARKET
                cond_id = getattr(market, "condition_id", "") or ""
                instrument_id_str = f"{cond_id}-{token_id}.POLYMARKET"

                writer.write({
                    "run_id": run_id,
                    "event": "strategy_result",
                    "asset_class": "weather",
                    "weather_market_type": "daily_temperature",
                    "preset_name": preset_name,
                    "arena": arena,
                    "mode": "basic",
                    "market_slug": slug,
                    "city": getattr(market, "city", ""),
                    "observation_date": str(getattr(market, "observation_date", "")),
                    "threshold_f": getattr(market, "threshold_f", None),
                    "metric": getattr(market, "metric", "high"),
                    "token_side": side,
                    "instrument_id": instrument_id_str,
                    "entry_price": mid,
                    "shares": float(shares),
                    "stake": float(stake),
                    "accounting_status": "open",
                    "resolved": False,
                    "exit_reason": "position_open",
                    "entry_time": now_fn().isoformat(),
                    "exit_time": None,
                    "pnl": None,
                    "stop_loss_price": sl_price,
                    "take_profit_price": tp_price,
                    "timestamp": now_fn().isoformat(),
                    "clob_response": str(resp_order),
                })

                entered_this_session.add((slug, side))
                budget_remaining -= stake
                _log.info("Entered %s [%s]  remaining_budget=%.4f", slug, side, float(budget_remaining))

    # Sleep until session boundary
    sleep_secs = max((session_end_time - now_fn()).total_seconds(), 0)
    _log.info("All entries done. Sleeping %.0fs until session boundary.", sleep_secs)
    await asyncio.sleep(sleep_secs)


async def _run_continuous_session(
    *,
    markets: list[DailyTemperatureMarket],
    monitoring_markets: list[DailyTemperatureMarket],
    preset_set: str,
    writer: JsonlRunWriter,
    run_id: str,
    now_fn: Callable[[], datetime],
    session_end_time: datetime,
    session_stake_cap: Decimal | None = None,
) -> None:
    """
    Run the full Nautilus session — node construction, build, quote streaming,
    and position flushing — directly on the main asyncio loop.

    node.build() is a blocking synchronous call that installs signal handlers
    (requiring the main thread) and connects to Polymarket's CLOB API.  We
    cannot run it in a worker thread or subprocess.  Instead we use a
    SIGALRM-based hard timeout (_node_build_with_sigalrm) that fires even
    when the event loop is blocked, raising RecoverableDaemonError if the
    CLOB API hangs longer than _NODE_BUILD_TIMEOUT_SECS.
    """
    node, presets, strategy_ids_by_key = _build_node_and_strategies(
        markets=markets,
        monitoring_markets=monitoring_markets,
        preset_set=preset_set,
        session_stake_cap=session_stake_cap,
    )
    seen_position_ids: set[str] = set()
    seen_closed_position_ids: set[str] = set()

    try:
        _node_build_with_sigalrm(node)
    except RecoverableDaemonError:
        raise
    except Exception as exc:
        raise RecoverableDaemonError(f"node.build() failed: {exc}") from exc

    run_task = asyncio.create_task(node.run_async())

    # Compute session runtime and set SIGALRM. 420 = 300s instrument load + 120s stop buffer
    session_runtime_secs = max((session_end_time - now_fn()).total_seconds(), 60.0)
    _session_runtime_alarm_secs = int(session_runtime_secs + 420)

    def _session_alarm_handler(signum: int, frame: object) -> None:
        raise RecoverableDaemonError(
            f"session timed out after {_session_runtime_alarm_secs}s "
            "(node.run_async() blocked the event loop)"
        )

    old_session_handler = signal.signal(signal.SIGALRM, _session_alarm_handler)
    signal.alarm(_session_runtime_alarm_secs)

    try:
        all_markets = list(markets) + list(monitoring_markets)
        while now_fn() < session_end_time:
            await asyncio.sleep(_POSITION_POLL_SECS)
            _flush_new_positions(
                node=node,
                markets=all_markets,
                presets=presets,
                strategy_ids_by_key=strategy_ids_by_key,
                seen_position_ids=seen_position_ids,
                writer=writer,
                run_id=run_id,
                now_fn=now_fn,
            )
            _flush_closed_positions(
                node=node,
                markets=all_markets,
                presets=presets,
                strategy_ids_by_key=strategy_ids_by_key,
                seen_closed_position_ids=seen_closed_position_ids,
                writer=writer,
                run_id=run_id,
                now_fn=now_fn,
            )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_session_handler)
        await _node_stop_with_sigalrm(node, run_task)
        try:
            await asyncio.wait_for(run_task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
        _flush_new_positions(
            node=node,
            markets=all_markets,
            presets=presets,
            strategy_ids_by_key=strategy_ids_by_key,
            seen_position_ids=seen_position_ids,
            writer=writer,
            run_id=run_id,
            now_fn=now_fn,
        )
        _flush_closed_positions(
            node=node,
            markets=all_markets,
            presets=presets,
            strategy_ids_by_key=strategy_ids_by_key,
            seen_closed_position_ids=seen_closed_position_ids,
            writer=writer,
            run_id=run_id,
            now_fn=now_fn,
        )
        try:
            node.kernel.dispose()
        except Exception:
            pass
        if node.kernel.executor:
            node.kernel.executor.shutdown(wait=False, cancel_futures=True)


async def _run_main_loop(
    *,
    preset_set: str,
    output_dir: str,
    max_rounds: int,
    reconnect_delay: float,
    capital_budget_usd: float | None = None,
) -> None:
    import uuid as _uuid
    from datetime import time as _time
    from datetime import timedelta as _timedelta

    # Derive CLOB credentials from private key if not explicitly configured.
    _ensure_clob_credentials()

    output_path = build_output_path(
        output_dir=output_dir,
        preset_set=f"live_{preset_set}",
        now=datetime.now(tz=UTC),
    )
    writer = JsonlRunWriter(output_path)
    now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC)
    sessions_completed = 0
    budget = Decimal(str(capital_budget_usd)) if capital_budget_usd is not None else None

    while max_rounds <= 0 or sessions_completed < max_rounds:
        run_id = _uuid.uuid4().hex
        started_at = now_fn()

        # The trading day this session covers.  Sessions run from SESSION_END_HOUR_UTC:00
        # UTC to SESSION_END_HOUR_UTC:00 UTC the next day, so a process that starts at
        # 02:00 UTC April 22 is still in the April 21 session and must scan JSONL for
        # April 21 entries and monitor April 21 open positions.
        session_trading_day = _session_trading_day(started_at)

        # The UTC time at which this session ends and the next one begins.
        next_session_boundary = datetime.combine(
            session_trading_day + _timedelta(days=1),
            _time(SESSION_END_HOUR_UTC, 0),
            tzinfo=UTC,
        )

        # --- capital budget gate ---
        if budget is not None:
            deployed = _compute_total_deployed(output_dir, f"live_{preset_set}", date=session_trading_day)
            remaining = budget - deployed
            if remaining <= Decimal("0"):
                writer.write({
                    "run_id": run_id,
                    "event": "budget_exhausted",
                    "asset_class": "weather",
                    "weather_market_type": "daily_temperature",
                    "preset_set": preset_set,
                    "capital_budget_usd": float(budget),
                    "total_deployed_usd": float(deployed),
                    "session_trading_day": str(session_trading_day),
                    "timestamp": started_at.isoformat(),
                })
                # Sleep until the next session boundary, then resume with a fresh budget.
                sleep_secs = (next_session_boundary - now_fn()).total_seconds()
                await asyncio.sleep(max(sleep_secs, 60.0))
                # Roll over to a new JSONL file for the new trading day.
                output_path = build_output_path(
                    output_dir=output_dir,
                    preset_set=f"live_{preset_set}",
                    now=datetime.now(tz=UTC),
                )
                writer = JsonlRunWriter(output_path)
                continue
        else:
            remaining = None

        # --- discover markets ---
        try:
            markets = await _default_resolve_markets()
        except Exception as exc:
            writer.write({
                "run_id": run_id,
                "event": "error",
                "asset_class": "weather",
                "weather_market_type": "daily_temperature",
                "preset_set": preset_set,
                "reason": str(exc),
                "timestamp": started_at.isoformat(),
            })
            sessions_completed += 1
            await asyncio.sleep(_backoff_delay(reconnect_delay))
            continue

        # Same-day markets only, resolved against each city's LOCAL calendar date.
        # A city like LA (UTC-7) is still on yesterday when UTC reads 06:50; including
        # its "tomorrow" markets inflates the pool with zero-obs AMM-wall entries.
        # Conversely, Asian cities (UTC+8/+9) start their day 7-9 h before UTC does,
        # so using the server's UTC date would miss the early entry window.
        #
        # Note: _city_local_date() is used (not session_trading_day) for the tradeable
        # filter so that each city is gated on its own local calendar date rather than
        # the session anchor.  The two agree for all cities except in the narrow window
        # between UTC midnight and SESSION_END_HOUR_UTC:00 UTC, where US cities have
        # rolled to the next local calendar day but the session still belongs to the
        # previous trading day — which is also correct: those US markets have resolved.
        tradeable = [m for m in markets if m.observation_date == _city_local_date(m.city)]
        already_entered = _already_entered_today(Path(output_dir), session_trading_day)
        if already_entered:
            _log.info("Skipping %d already-entered markets: %s", len(already_entered), already_entered)
        tradeable = [m for m in tradeable if m.slug not in already_entered]

        # Pre-filter tradeable to markets plausibly in the strategy's price band using
        # Gamma's bestAsk / bestBid. This avoids loading thousands of instruments for markets
        # that will never pass the strategy's entry filter. Markets without a price are kept
        # (they may have just been listed).
        # YES band: 0.87–0.99 (slightly below min_ask=0.90).
        # NO band: check bestBid for yes side as a proxy (bestBid ≈ 1 - NO ask).
        _yes_presets = any(getattr(p, "token_side", "yes") == "yes" for p in _strategy_presets_for_set(preset_set))
        _no_presets  = any(getattr(p, "token_side", "yes") == "no"  for p in _strategy_presets_for_set(preset_set))
        tradeable = [
            m for m in tradeable
            if (
                m.best_ask is None
                or (_yes_presets and 0.87 <= m.best_ask <= 0.99)
                or (_no_presets  and m.best_ask is not None and 0.01 <= m.best_ask <= 0.13)
            )
        ]

        # Hard cap: node.build() makes sequential CLOB API calls for every instrument.
        # Loading >~15 instruments takes so long it blocks the Rust event loop past
        # SIGALRM's reach, causing an unrecoverable hang.  Sort by best-ask proximity
        # to the take-profit band (0.99) — highest-probability markets first — and cap
        # at MAX_MARKETS_PER_SESSION.  Remaining markets will be picked up in the next
        # session cycle after the session boundary rolls.
        _MAX_MARKETS_PER_SESSION = 12
        if len(tradeable) > _MAX_MARKETS_PER_SESSION:
            def _sort_key(m):
                ask = m.best_ask
                if ask is None:
                    return 1.0  # no price info: lowest priority
                return abs(ask - 0.99)  # closest to take-profit first
            tradeable = sorted(tradeable, key=_sort_key)[:_MAX_MARKETS_PER_SESSION]
            _log.info("Capped tradeable markets to %d (sorted by proximity to 0.99)", _MAX_MARKETS_PER_SESSION)

        # Markets with open positions that should be monitored for stop-loss/take-profit.
        # Use local date here too so we don't drop monitoring for cities whose local date
        # differs from the session trading day.
        open_position_markets = [
            m for m in markets
            if m.observation_date == _city_local_date(m.city) and m.slug in already_entered
        ]

        if not tradeable and not open_position_markets:
            writer.write({
                "run_id": run_id,
                "event": "session_end",
                "asset_class": "weather",
                "weather_market_type": "daily_temperature",
                "preset_set": preset_set,
                "reason": "no_tradeable_markets",
                "session_trading_day": str(session_trading_day),
                "timestamp": now_fn().isoformat(),
            })
            sessions_completed += 1
            sleep_secs = (next_session_boundary - now_fn()).total_seconds()
            await asyncio.sleep(max(sleep_secs, 60.0))
            continue

        # --- run continuous session ---
        session_runtime_secs = max((next_session_boundary - now_fn()).total_seconds(), 60.0)
        session_timeout_secs = session_runtime_secs + 420.0

        writer.write({
            "run_id": run_id,
            "event": "session_start",
            "asset_class": "weather",
            "weather_market_type": "daily_temperature",
            "preset_set": preset_set,
            "session_trading_day": str(session_trading_day),
            "session_end_time": next_session_boundary.isoformat(),
            "tradeable_markets_count": len(tradeable),
            "monitoring_markets_count": len(open_position_markets),
            "timestamp": started_at.isoformat(),
        })

        try:
            # Direct CLOB entry: bypasses TradingNode.build() which hangs indefinitely
            # because the Rust thread pool blocks on futex calls that SIGALRM cannot
            # interrupt.  _run_direct_clob_entry_session uses py_clob_client directly —
            # the same path the take-profit watcher uses for sells.
            await _run_direct_clob_entry_session(
                markets=tradeable,
                writer=writer,
                run_id=run_id,
                now_fn=now_fn,
                session_end_time=next_session_boundary,
                session_stake_cap=remaining,
            )
        except RecoverableDaemonError as exc:
            writer.write({
                "run_id": run_id,
                "event": "error",
                "asset_class": "weather",
                "weather_market_type": "daily_temperature",
                "preset_set": preset_set,
                "reason": str(exc),
                "timestamp": now_fn().isoformat(),
            })
            sessions_completed += 1
            await asyncio.sleep(_backoff_delay(reconnect_delay))
            continue

        writer.write({
            "run_id": run_id,
            "event": "session_end",
            "asset_class": "weather",
            "weather_market_type": "daily_temperature",
            "preset_set": preset_set,
            "session_trading_day": str(session_trading_day),
            "timestamp": now_fn().isoformat(),
        })

        sessions_completed += 1
        if max_rounds > 0 and sessions_completed >= max_rounds:
            break

        # Brief pause for market list refresh before reconnecting
        await asyncio.sleep(_RESTART_PAUSE_SECS)


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(
            _run_main_loop(
                preset_set=str(args.preset_set),
                output_dir=str(args.output_dir or DEFAULT_OUTPUT_DIR),
                max_rounds=int(args.max_rounds or 0),
                reconnect_delay=float(args.reconnect_delay or 2.0),
                capital_budget_usd=float(args.capital_budget) if args.capital_budget is not None else None,
            ),
        )
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
