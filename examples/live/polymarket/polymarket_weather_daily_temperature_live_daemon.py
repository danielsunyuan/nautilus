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


# How long to keep the node (and quote streams) open before a market refresh.
# One hour is long enough to cover most daily-temperature entry windows while
# still picking up newly-listed markets regularly.
_NODE_RUNTIME_SECS = 3600.0

# How often to scan the cache for newly-opened positions and flush them to JSONL.
_POSITION_POLL_SECS = 30.0

# Pause between the end of one session and the start of the next (market refresh).
_RESTART_PAUSE_SECS = 10.0

# Hard cap on the whole session (build + runtime + stop).
# 300s for instrument loading + 3600s runtime + 120s buffer for stop
_SESSION_TIMEOUT_SECS = _NODE_RUNTIME_SECS + 420.0


def _build_node_and_strategies(
    *,
    markets: list[DailyTemperatureMarket],
    preset_set: str,
    session_stake_cap: Decimal | None = None,
) -> tuple[TradingNode, tuple, dict[str, StrategyId]]:
    """Create a configured TradingNode with strategies attached. Does not call build()."""
    presets = _strategy_presets_for_set(preset_set)
    instrument_ids = [_build_instrument_id(market, "yes") for market in markets]
    config = build_live_daemon_node_config(
        instrument_ids=instrument_ids,
        trader_id="LIVE-WEATHER-DAEMON",
        cache_host=os.getenv("NAUTILUS_CACHE_HOST", DEFAULT_CACHE_HOST),
        cache_port=int(os.getenv("NAUTILUS_CACHE_PORT", str(DEFAULT_CACHE_PORT))),
    )
    node = TradingNode(config=config)
    strategy_ids_by_key: dict[str, StrategyId] = {}

    families: dict[tuple[str, str, str], list[InstrumentId]] = {}
    for market in markets:
        key = (market.city, str(market.observation_date), market.metric)
        families.setdefault(key, []).append(InstrumentId.from_str(_build_instrument_id(market, "yes")))

    family_ids_by_slug: dict[str, tuple[InstrumentId, ...]] = {
        market.slug: tuple(families.get((market.city, str(market.observation_date), market.metric), []))
        for market in markets
    }

    live_mode = str(preset_set).strip().lower() in {"live_90_basic", "live-weather-v1"}
    # Cap this session's open stake at the remaining budget (never exceed $50 default either).
    # At $5/market × 10 positions = $50 max per day.
    if live_mode:
        default_cap = Decimal("50")
        open_stake_cap = min(default_cap, session_stake_cap) if session_stake_cap is not None else default_cap
    else:
        open_stake_cap = None

    for market in markets:
        inst_id_str = _build_instrument_id(market, "yes")
        for preset in presets:
            strategy_key = f"{market.slug}:{preset.name}"
            strategy = WeatherDailyTemperaturePaperStrategy(
                config=WeatherDailyTemperaturePaperStrategyConfig(
                    strategy_id=f"LIVE-WTHR-{preset.name.upper()}",
                    instrument_id=InstrumentId.from_str(inst_id_str),
                    preset=preset,
                    order_qty=Decimal(str(preset.order_qty)),
                    token_side="yes",
                    family_instrument_ids=family_ids_by_slug.get(market.slug, ()),
                    target_usd_per_market=(Decimal("5") if live_mode else None),
                    min_order_size_shares=(Decimal("5") if live_mode else Decimal("0")),
                    max_stake_per_market=(Decimal("5.25") if live_mode else None),
                    max_open_positions=(10 if live_mode else None),
                    max_total_open_stake=open_stake_cap,
                ),
            )
            node.trader.add_strategy(strategy)
            strategy_ids_by_key[strategy_key] = strategy.id

    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
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
        instrument_id = InstrumentId.from_str(_build_instrument_id(market, "yes"))
        for preset in presets:
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
                    "token_side": "yes",
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


def _already_entered_today(output_dir: Path, today) -> set[str]:
    """Return market slugs that already have an open position entry for *today*.

    Scans all ``*.jsonl`` files under ``<output_dir>/polymarket/runs/`` for
    ``strategy_result`` rows whose ``observation_date`` matches *today* and
    whose ``accounting_status`` is ``"open"``.  The returned set of slugs is
    used to exclude already-entered markets from the current session so that a
    daemon restart does not double-buy a position.
    """
    slugs: set[str] = set()
    runs_dir = Path(output_dir).resolve(strict=False) / "polymarket" / "runs"
    if not runs_dir.exists():
        return slugs
    for jsonl_file in runs_dir.glob("*.jsonl"):
        with jsonl_file.open() as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if (
                    row.get("event") == "strategy_result"
                    and row.get("observation_date") == str(today)
                    and row.get("accounting_status") == "open"
                ):
                    slugs.add(row["market_slug"])
    return slugs


async def _run_continuous_session(
    *,
    markets: list[DailyTemperatureMarket],
    preset_set: str,
    writer: JsonlRunWriter,
    run_id: str,
    now_fn: Callable[[], datetime],
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
        preset_set=preset_set,
        session_stake_cap=session_stake_cap,
    )
    seen_position_ids: set[str] = set()

    try:
        _node_build_with_sigalrm(node)
    except RecoverableDaemonError:
        raise
    except Exception as exc:
        raise RecoverableDaemonError(f"node.build() failed: {exc}") from exc

    run_task = asyncio.create_task(node.run_async())

    # Hard kill for the entire session runtime.  asyncio.wait_for() won't fire
    # if node.run_async() makes a blocking call that stalls the event loop.
    # SIGALRM fires on the main thread regardless — raising RecoverableDaemonError
    # causes the outer _run_main_loop to restart the session cleanly.
    _session_runtime_alarm_secs = int(_NODE_RUNTIME_SECS + 420)  # 420 = 300s instrument load + 120s stop buffer

    def _session_alarm_handler(signum: int, frame: object) -> None:
        raise RecoverableDaemonError(
            f"session timed out after {_session_runtime_alarm_secs}s "
            "(node.run_async() blocked the event loop)"
        )

    old_session_handler = signal.signal(signal.SIGALRM, _session_alarm_handler)
    signal.alarm(_session_runtime_alarm_secs)

    try:
        elapsed = 0.0
        while elapsed < _NODE_RUNTIME_SECS:
            await asyncio.sleep(_POSITION_POLL_SECS)
            elapsed += _POSITION_POLL_SECS
            _flush_new_positions(
                node=node,
                markets=markets,
                presets=presets,
                strategy_ids_by_key=strategy_ids_by_key,
                seen_position_ids=seen_position_ids,
                writer=writer,
                run_id=run_id,
                now_fn=now_fn,
            )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_session_handler)
        try:
            await asyncio.wait_for(node.stop_async(), timeout=30.0)
        except (asyncio.TimeoutError, Exception):
            run_task.cancel()
        try:
            await asyncio.wait_for(run_task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
        _flush_new_positions(
            node=node,
            markets=markets,
            presets=presets,
            strategy_ids_by_key=strategy_ids_by_key,
            seen_position_ids=seen_position_ids,
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
    from datetime import date as _date
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

        # --- capital budget gate ---
        if budget is not None:
            deployed = _compute_total_deployed(output_dir, f"live_{preset_set}", date=_date.today())
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
                    "timestamp": started_at.isoformat(),
                })
                # Sleep until midnight UTC, then resume with a fresh daily budget.
                midnight = datetime.combine(
                    started_at.date() + _timedelta(days=1),
                    _time.min,
                    tzinfo=UTC,
                )
                sleep_secs = (midnight - started_at).total_seconds()
                await asyncio.sleep(max(sleep_secs, 60.0))
                # Roll over to a new JSONL file for the new day.
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

        writer.write({
            "run_id": run_id,
            "event": "session_start",
            "asset_class": "weather",
            "weather_market_type": "daily_temperature",
            "preset_set": preset_set,
            "markets_found": len(markets),
            "timestamp": started_at.isoformat(),
        })

        # Same-day markets only — CLOB price discovery is active today; future-date markets
        # have no real asks (bid=0.01, ask=0.99 wall only). No cap — subscribe to all for
        # first-mover advantage over Gamma's ~2-minute refresh lag.
        today = _date.today()
        tradeable = [m for m in markets if m.observation_date == today]
        already_entered = _already_entered_today(Path(output_dir), today)
        if already_entered:
            _log.info("Skipping %d already-entered markets: %s", len(already_entered), already_entered)
        tradeable = [m for m in tradeable if m.slug not in already_entered]

        if not tradeable:
            writer.write({
                "run_id": run_id,
                "event": "session_end",
                "asset_class": "weather",
                "weather_market_type": "daily_temperature",
                "preset_set": preset_set,
                "reason": "no_tradeable_markets",
                "timestamp": now_fn().isoformat(),
            })
            sessions_completed += 1
            await asyncio.sleep(_backoff_delay(reconnect_delay))
            continue

        # --- run continuous session ---
        try:
            await asyncio.wait_for(
                _run_continuous_session(
                    markets=tradeable,
                    preset_set=preset_set,
                    writer=writer,
                    run_id=run_id,
                    now_fn=now_fn,
                    session_stake_cap=remaining,
                ),
                timeout=_SESSION_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            writer.write({
                "run_id": run_id,
                "event": "error",
                "asset_class": "weather",
                "weather_market_type": "daily_temperature",
                "preset_set": preset_set,
                "reason": f"session timed out after {_SESSION_TIMEOUT_SECS}s",
                "timestamp": now_fn().isoformat(),
            })
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
