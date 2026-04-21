#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------
"""
Long-running Polymarket weather daily-temperature paper-trading daemon.

This daemon discovers daily-temperature weather markets, creates strategy
instances for each matching preset, and runs them via a Nautilus paper-trading
node with SandboxExecutionClientConfig.  Results are persisted as JSONL events.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import date as _date
from datetime import datetime
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import signal
import sys
import uuid
from typing import Any

try:
    from examples.live.polymarket.weather_daily_temperature_resolver import (
        DailyTemperatureMarket,
        discover_daily_temperature_markets,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_daily_temperature_resolver"
    module_path = Path(__file__).resolve().with_name("weather_daily_temperature_resolver.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    DailyTemperatureMarket = module.DailyTemperatureMarket
    discover_daily_temperature_markets = module.discover_daily_temperature_markets

try:
    from examples.live.polymarket.weather_daily_temperature_strategy_library import (
        WeatherTemperatureStrategyPreset,
        daily_temperature_price_arena_presets,
        should_enter_temperature_market,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_daily_temperature_strategy_library"
    module_path = Path(__file__).resolve().with_name("weather_daily_temperature_strategy_library.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    WeatherTemperatureStrategyPreset = module.WeatherTemperatureStrategyPreset
    daily_temperature_price_arena_presets = module.daily_temperature_price_arena_presets
    should_enter_temperature_market = module.should_enter_temperature_market

try:
    from examples.live.polymarket.weather_daily_temperature_live_strategy import (
        WeatherDailyTemperaturePaperStrategy,
        WeatherDailyTemperaturePaperStrategyConfig,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_daily_temperature_live_strategy"
    module_path = Path(__file__).resolve().with_name("weather_daily_temperature_live_strategy.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    WeatherDailyTemperaturePaperStrategy = module.WeatherDailyTemperaturePaperStrategy
    WeatherDailyTemperaturePaperStrategyConfig = module.WeatherDailyTemperaturePaperStrategyConfig

from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket import PolymarketLiveDataClientFactory
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.config import CacheConfig
from nautilus_trader.config import DatabaseConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import MessageBusConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId

DEFAULT_OUTPUT_DIR = "/workspace/outputs"
DEFAULT_RECONNECT_DELAY = 2.0
DEFAULT_CACHE_HOST = "redis"
DEFAULT_CACHE_PORT = 6379
_SAFE_PRESET_SET = re.compile(r"^[A-Za-z0-9_-]+$")


class RecoverableDaemonError(RuntimeError):
    """Raised for recoverable session or round runtime failures."""


_NODE_BUILD_TIMEOUT_SECS = 180


def _node_build_with_sigalrm(node: Any) -> None:
    """Call node.build() with a SIGALRM hard timeout.

    node.build() makes synchronous HTTP calls (instrument loading) that can
    block the event loop indefinitely.  asyncio.wait_for cannot interrupt it;
    SIGALRM fires even while the loop is blocked.
    """

    def _alarm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        raise RecoverableDaemonError(
            f"node.build() timed out after {_NODE_BUILD_TIMEOUT_SECS}s"
        )

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(_NODE_BUILD_TIMEOUT_SECS)
    try:
        node.build()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


class JsonlRunWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.write("\n")
            handle.flush()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset-set", default="all", help="strategy preset set to run")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="base output directory")
    parser.add_argument("--max-rounds", type=int, default=0, help="stop after N rounds, 0 means run forever")
    parser.add_argument("--report-md", default="", help="optional path to refresh markdown report")
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=DEFAULT_RECONNECT_DELAY,
        help="seconds to wait after recoverable errors",
    )
    parser.add_argument(
        "--capital-budget",
        type=float,
        default=None,
        help="max USD to deploy lifetime for this preset set (e.g. 50.0). Daemon sleeps "
             "until midnight UTC when exhausted. Omit for no limit.",
    )
    return parser


def build_output_path(*, output_dir: str | Path, preset_set: str, now: datetime) -> Path:
    root = Path(output_dir)
    if ".." in root.parts:
        raise ValueError("output_dir must not contain '..'")
    if not _SAFE_PRESET_SET.fullmatch(str(preset_set).strip()):
        raise ValueError("preset_set must contain only letters, numbers, '_' or '-'")
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return root.resolve(strict=False) / "polymarket" / "runs" / f"weather_temp_{preset_set}_{stamp}.jsonl"


def _compute_total_deployed(
    output_dir: str | Path,
    preset_set_prefix: str,
    date=None,
) -> Decimal:
    """Sum stake values from strategy_result events across JSONL files for this daemon.

    When *date* is provided (a datetime.date), only events whose ``timestamp``
    starts with that date's ISO prefix (YYYY-MM-DD) are counted.  This enables
    a per-day budget cap — the default ``--capital-budget`` semantics.
    """
    total = Decimal("0")
    runs_dir = Path(output_dir).resolve(strict=False) / "polymarket" / "runs"
    if not runs_dir.exists():
        return total
    date_prefix: str | None = date.isoformat() if date is not None else None
    for path in runs_dir.glob(f"*{preset_set_prefix}*.jsonl"):
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    obj = json.loads(line)
                    if obj.get("event") == "strategy_result":
                        if date_prefix is not None:
                            ts = obj.get("timestamp", "")
                            if not str(ts).startswith(date_prefix):
                                continue
                        stake = obj.get("stake")
                        if stake is not None:
                            total += Decimal(str(stake))
        except Exception:
            pass
    return total


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _backoff_delay(reconnect_delay: float) -> float:
    base = max(0.0, float(reconnect_delay))
    return base + float(random.uniform(0.0, max(0.5, base * 0.5)))


def _strategy_presets_for_set(
    preset_set: str,
) -> tuple[WeatherTemperatureStrategyPreset, ...]:
    normalized = str(preset_set).strip().lower()
    all_presets = daily_temperature_price_arena_presets()
    if normalized in {"all", "full"}:
        return all_presets
    if normalized in {"live_90_basic", "live-weather-v1"}:
        return tuple(p for p in all_presets if p.name in {"temp_90c_basic", "temp_90c_no_basic"})
    if normalized in {"band_only", "band-only"}:
        return tuple(p for p in all_presets if p.mode == "band_only")
    if normalized == "basic":
        return tuple(p for p in all_presets if p.mode == "basic")
    if normalized == "support":
        return tuple(p for p in all_presets if p.mode == "support")
    if normalized == "smoke":
        return (all_presets[0],)
    raise ValueError(f"unsupported preset set {preset_set!r}")


def build_daemon_node_config(
    *,
    instrument_ids: list[str],
    trader_id: str,
    cache_host: str,
    cache_port: int,
) -> TradingNodeConfig:
    instrument_provider_config = PolymarketInstrumentProviderConfig(load_ids=frozenset(instrument_ids))
    return TradingNodeConfig(
        trader_id=TraderId(trader_id),
        logging=LoggingConfig(log_level="INFO", use_pyo3=True),
        exec_engine=LiveExecEngineConfig(
            load_cache=False,
            reconciliation=False,
            open_check_interval_secs=5.0,
            snapshot_orders=True,
            snapshot_positions=True,
            snapshot_positions_interval_secs=5.0,
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
            streams_prefix="polymarket-weather-temp",
            use_trader_prefix=True,
            use_trader_id=True,
            use_instance_id=True,
            stream_per_topic=False,
            autotrim_mins=60,
            heartbeat_interval_secs=1,
        ),
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
            ),
        },
        exec_clients={
            POLYMARKET: SandboxExecutionClientConfig(
                venue=str(POLYMARKET_VENUE),
                base_currency=str(USDC_POS),
                account_type="CASH",
                starting_balances=[f"1_000 {USDC_POS}"],
                fee_model_path="nautilus_trader.adapters.polymarket.fee_model.PolymarketFeeModel",
            ),
        },
        timeout_connection=90.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )


def _build_instrument_id(market: DailyTemperatureMarket, token_side: str = "yes") -> str:
    token_id = market.yes_token_id if token_side == "yes" else market.no_token_id
    return f"{market.condition_id}-{token_id}.POLYMARKET"


async def run_daemon(
    *,
    preset_set: str,
    resolve_markets: Callable[[], Awaitable[list[DailyTemperatureMarket]]],
    run_round: Callable[..., Awaitable[list[dict[str, Any]]]],
    sleep_between_rounds: Callable[..., Awaitable[None]],
    writer: JsonlRunWriter,
    now_fn: Callable[[], datetime],
    max_rounds: int = 0,
    run_id: str | None = None,
    backoff_sleep: Callable[[float], Awaitable[None]] | None = None,
    reconnect_delay: float = DEFAULT_RECONNECT_DELAY,
) -> None:
    rounds_completed = 0
    daemon_run_id = run_id or uuid.uuid4().hex
    backoff = backoff_sleep or asyncio.sleep

    while max_rounds <= 0 or rounds_completed < max_rounds:
        started_at = now_fn().astimezone(UTC)

        # --- discover markets ---
        try:
            markets = await resolve_markets()
        except Exception as exc:
            error_reason = str(exc)
            writer.write(
                {
                    "run_id": daemon_run_id,
                    "event": "error",
                    "asset_class": "weather",
                    "weather_market_type": "daily_temperature",
                    "preset_set": preset_set,
                    "reason": error_reason,
                    "timestamp": started_at.isoformat(),
                },
            )
            rounds_completed += 1
            await backoff(_backoff_delay(reconnect_delay))
            continue

        # --- round_start ---
        writer.write(
            {
                "run_id": daemon_run_id,
                "event": "round_start",
                "asset_class": "weather",
                "weather_market_type": "daily_temperature",
                "preset_set": preset_set,
                "markets_found": len(markets),
                "timestamp": started_at.isoformat(),
            },
        )

        # --- market_discovered events ---
        for market in markets:
            writer.write(
                {
                    "run_id": daemon_run_id,
                    "event": "market_discovered",
                    "asset_class": "weather",
                    "weather_market_type": "daily_temperature",
                    "market_slug": market.slug,
                    "city": market.city,
                    "observation_date": str(market.observation_date),
                    "threshold_f": market.threshold_f,
                    "timestamp": started_at.isoformat(),
                },
            )

        # --- filter to near-term markets only (observation_date >= today) ---
        today = _date.today()
        tradeable_markets = [m for m in markets if m.observation_date >= today]
        # Sort by observation_date ascending (nearest first) and cap at 100
        tradeable_markets.sort(key=lambda m: m.observation_date)
        tradeable_markets = tradeable_markets[:100]

        writer.write(
            {
                "run_id": daemon_run_id,
                "event": "markets_filtered",
                "asset_class": "weather",
                "weather_market_type": "daily_temperature",
                "preset_set": preset_set,
                "markets_total": len(markets),
                "markets_tradeable": len(tradeable_markets),
                "filter_date": str(today),
                "timestamp": started_at.isoformat(),
            },
        )

        if not tradeable_markets:
            writer.write(
                {
                    "run_id": daemon_run_id,
                    "event": "round_end",
                    "asset_class": "weather",
                    "weather_market_type": "daily_temperature",
                    "preset_set": preset_set,
                    "reason": "no_tradeable_markets",
                    "timestamp": started_at.isoformat(),
                },
            )
            rounds_completed += 1
            await sleep_between_rounds()
            continue

        # --- run round ---
        try:
            rows = await run_round(
                markets=tradeable_markets,
                preset_set=preset_set,
            )
        except RecoverableDaemonError as exc:
            error_reason = str(exc)
            writer.write(
                {
                    "run_id": daemon_run_id,
                    "event": "error",
                    "asset_class": "weather",
                    "weather_market_type": "daily_temperature",
                    "preset_set": preset_set,
                    "reason": error_reason,
                    "timestamp": now_fn().astimezone(UTC).isoformat(),
                },
            )
            rounds_completed += 1
            await backoff(_backoff_delay(reconnect_delay))
            continue

        for row in rows:
            payload = dict(row)
            payload.setdefault("run_id", daemon_run_id)
            payload.setdefault("asset_class", "weather")
            payload.setdefault("weather_market_type", "daily_temperature")
            payload.setdefault("timestamp", now_fn().astimezone(UTC).isoformat())
            writer.write(payload)

        # --- round_end ---
        writer.write(
            {
                "run_id": daemon_run_id,
                "event": "round_end",
                "asset_class": "weather",
                "weather_market_type": "daily_temperature",
                "preset_set": preset_set,
                "timestamp": now_fn().astimezone(UTC).isoformat(),
            },
        )

        rounds_completed += 1
        if max_rounds > 0 and rounds_completed >= max_rounds:
            break
        await sleep_between_rounds()


def _as_decimal(value: Any) -> Decimal:
    if hasattr(value, "as_decimal"):
        return Decimal(str(value.as_decimal()))
    return Decimal(str(value))


def _iso8601_from_unix_nanos(timestamp_ns: int | None) -> str | None:
    if not timestamp_ns:
        return None
    return unix_nanos_to_dt(int(timestamp_ns)).isoformat()


def extract_weather_strategy_results(
    *,
    cache: Any,
    markets: list[DailyTemperatureMarket],
    presets: tuple[Any, ...],
    strategy_ids_by_key: dict[str, StrategyId],
) -> list[dict[str, Any]]:
    """Extract open positions from the cache for each (market, preset) pair."""
    rows: list[dict[str, Any]] = []
    for market in markets:
        instrument_id_str = _build_instrument_id(market, "yes")
        cache_instrument_id = InstrumentId.from_str(instrument_id_str)
        for preset in presets:
            strategy_key = f"{market.slug}:{preset.name}"
            strategy_id = strategy_ids_by_key.get(strategy_key)
            if strategy_id is None:
                continue

            open_positions = list(
                cache.positions_open(
                    instrument_id=cache_instrument_id,
                    strategy_id=strategy_id,
                ),
            )
            if open_positions:
                pos = open_positions[-1]
                shares = float(_as_decimal(getattr(pos, "peak_qty", None) or pos.quantity))
                entry_price = float(pos.avg_px_open)
                stake = float(Decimal(str(entry_price)) * Decimal(str(shares)))
                rows.append(
                    {
                        "event": "strategy_result",
                        "preset_name": preset.name,
                        "arena": preset.arena,
                        "mode": preset.mode,
                        "market_slug": market.slug,
                        "city": market.city,
                        "observation_date": str(market.observation_date),
                        "threshold_f": market.threshold_f,
                        "metric": market.metric,
                        "token_side": "yes",
                        "condition_id": market.condition_id,
                        "instrument_id": instrument_id_str,
                        "entry_price": entry_price,
                        "shares": shares,
                        "stake": stake,
                        "accounting_status": "open",
                        "resolved": False,
                        "exit_reason": "position_open",
                        "entry_time": _iso8601_from_unix_nanos(getattr(pos, "ts_opened", 0)),
                        "exit_time": None,
                        "pnl": None,
                    },
                )
            else:
                rows.append(
                    {
                        "event": "strategy_result",
                        "preset_name": preset.name,
                        "arena": preset.arena,
                        "mode": preset.mode,
                        "market_slug": market.slug,
                        "city": market.city,
                        "observation_date": str(market.observation_date),
                        "threshold_f": market.threshold_f,
                        "metric": market.metric,
                        "token_side": "yes",
                        "condition_id": market.condition_id,
                        "instrument_id": instrument_id_str,
                        "entry_price": None,
                        "shares": None,
                        "stake": None,
                        "accounting_status": "no_position",
                        "resolved": False,
                        "exit_reason": "no_position",
                        "entry_time": None,
                        "exit_time": None,
                        "pnl": None,
                    },
                )
    return rows


async def _default_run_round(
    *,
    markets: list[DailyTemperatureMarket],
    preset_set: str,
) -> list[dict[str, Any]]:
    presets = _strategy_presets_for_set(preset_set)
    instrument_ids: list[str] = []
    for market in markets:
        instrument_ids.append(_build_instrument_id(market, "yes"))

    config = build_daemon_node_config(
        instrument_ids=instrument_ids,
        trader_id="PAPER-WEATHER-DAEMON",
        cache_host=os.getenv("NAUTILUS_CACHE_HOST", DEFAULT_CACHE_HOST),
        cache_port=int(os.getenv("NAUTILUS_CACHE_PORT", str(DEFAULT_CACHE_PORT))),
    )
    node = TradingNode(config=config)
    strategy_ids_by_key: dict[str, StrategyId] = {}
    family_instrument_ids_by_market_slug: dict[str, tuple[InstrumentId, ...]] = {}

    families: dict[tuple[str, str, str], list[InstrumentId]] = {}
    for market in markets:
        family_key = (market.city, str(market.observation_date), market.metric)
        families.setdefault(family_key, []).append(InstrumentId.from_str(_build_instrument_id(market, "yes")))
    for market in markets:
        family_key = (market.city, str(market.observation_date), market.metric)
        family_instrument_ids_by_market_slug[market.slug] = tuple(families.get(family_key, []))

    live_mode = str(preset_set).strip().lower() in {"live_90_basic", "live-weather-v1"}

    for market in markets:
        inst_id_str = _build_instrument_id(market, "yes")
        for preset in presets:
            strategy_key = f"{market.slug}:{preset.name}"
            strategy = WeatherDailyTemperaturePaperStrategy(
                config=WeatherDailyTemperaturePaperStrategyConfig(
                    strategy_id=f"WTHR-{preset.name.upper()}",
                    instrument_id=InstrumentId.from_str(inst_id_str),
                    preset=preset,
                    order_qty=Decimal(str(preset.order_qty)),
                    token_side="yes",
                    family_instrument_ids=family_instrument_ids_by_market_slug.get(market.slug, ()),
                    target_usd_per_market=(Decimal("5") if live_mode else None),
                    min_order_size_shares=(Decimal("5") if live_mode else Decimal("0")),
                    max_stake_per_market=(Decimal("5.25") if live_mode else None),
                    max_open_positions=(8 if live_mode else None),
                    max_total_open_stake=(Decimal("40") if live_mode else None),
                ),
            )
            node.trader.add_strategy(strategy)
            strategy_ids_by_key[strategy_key] = strategy.id

    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    node.add_exec_client_factory(POLYMARKET, SandboxLiveExecClientFactory)
    _node_build_with_sigalrm(node)

    try:
        run_task = asyncio.create_task(node.run_async())
        # Weather markets run longer; wait for resolution or external stop
        await asyncio.sleep(90.0)
        try:
            await asyncio.wait_for(node.stop_async(), timeout=30.0)
        except asyncio.TimeoutError:
            run_task.cancel()
        try:
            await asyncio.wait_for(run_task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return extract_weather_strategy_results(
            cache=node.cache,
            markets=markets,
            presets=presets,
            strategy_ids_by_key=strategy_ids_by_key,
        )
    except Exception as exc:
        raise RecoverableDaemonError(str(exc)) from exc
    finally:
        try:
            node.kernel.dispose()
        except Exception:
            pass
        if node.kernel.executor:
            node.kernel.executor.shutdown(wait=False, cancel_futures=True)


_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


async def _default_resolve_markets() -> list[DailyTemperatureMarket]:
    from nautilus_trader.core.nautilus_pyo3 import HttpClient

    http_client = HttpClient(
        timeout_secs=15,
        default_headers={"User-Agent": _BROWSER_UA},
    )
    return await discover_daily_temperature_markets(
        http_client=http_client,
        gamma_base_url="https://gamma-api.polymarket.com",
    )


async def _run_main_loop(
    *,
    preset_set: str,
    output_dir: str,
    max_rounds: int,
    reconnect_delay: float,
    report_md: str,
) -> None:
    output_path = build_output_path(
        output_dir=output_dir,
        preset_set=preset_set,
        now=datetime.now(tz=UTC),
    )
    writer = JsonlRunWriter(output_path)
    await run_daemon(
        preset_set=preset_set,
        resolve_markets=_default_resolve_markets,
        run_round=_default_run_round,
        sleep_between_rounds=lambda: asyncio.sleep(300.0),
        writer=writer,
        now_fn=lambda: datetime.now(tz=UTC),
        max_rounds=max_rounds,
        reconnect_delay=reconnect_delay,
    )


def main() -> int:
    args = _build_parser().parse_args()
    asyncio.run(
        _run_main_loop(
            preset_set=str(args.preset_set),
            output_dir=str(args.output_dir),
            max_rounds=int(args.max_rounds),
            reconnect_delay=float(args.reconnect_delay),
            report_md=str(args.report_md),
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
