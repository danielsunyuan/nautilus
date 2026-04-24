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
Weather ensemble paper-trading daemon.

Polls Open-Meteo ensemble forecasts for cities, generates trading signals,
and runs Nautilus sandbox strategies to collect price quotes and paper trades.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
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
from typing import Literal

try:
    from examples.live.polymarket.weather_daily_temperature_resolver import (
        DailyTemperatureMarket,
        filter_tradeable_daily_temperature_markets,
    )
    from examples.live.polymarket.weather_daily_temperature_resolver import (
        resolve_daily_temperature_markets,
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
    filter_tradeable_daily_temperature_markets = module.filter_tradeable_daily_temperature_markets
    resolve_daily_temperature_markets = module.resolve_daily_temperature_markets

try:
    from examples.live.polymarket.weather_ensemble_forecast import (
        OpenMeteoEnsembleForecastClient,
        OpenMeteoEnsembleForecastConfig,
        probability_high_above,
        probability_high_below,
        probability_low_above,
        probability_low_below,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_ensemble_forecast"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_forecast.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    OpenMeteoEnsembleForecastClient = module.OpenMeteoEnsembleForecastClient
    OpenMeteoEnsembleForecastConfig = module.OpenMeteoEnsembleForecastConfig
    probability_high_above = module.probability_high_above
    probability_high_below = module.probability_high_below
    probability_low_above = module.probability_low_above
    probability_low_below = module.probability_low_below

try:
    from examples.live.polymarket.weather_ensemble_models import WeatherMarketSnapshot
    from examples.live.polymarket.weather_ensemble_signal_engine import (
        WeatherEnsembleSignalEngine,
        WeatherEnsembleSignalConfig,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_ensemble_models"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_models.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    WeatherMarketSnapshot = module.WeatherMarketSnapshot

    module_name = "examples.live.polymarket.weather_ensemble_signal_engine"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_signal_engine.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    WeatherEnsembleSignalEngine = module.WeatherEnsembleSignalEngine
    WeatherEnsembleSignalConfig = module.WeatherEnsembleSignalConfig

try:
    from examples.live.polymarket.weather_ensemble_strategy_library import (
        normalize_candidate_payload,
        should_enter_weather_ensemble_market,
        weather_ensemble_presets,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_ensemble_strategy_library"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_strategy_library.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    normalize_candidate_payload = module.normalize_candidate_payload
    should_enter_weather_ensemble_market = module.should_enter_weather_ensemble_market
    weather_ensemble_presets = module.weather_ensemble_presets

try:
    from examples.live.polymarket.weather_ensemble_live_strategy import (
        WeatherEnsemblePaperStrategy,
        WeatherEnsemblePaperStrategyConfig,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_ensemble_live_strategy"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_live_strategy.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    WeatherEnsemblePaperStrategy = module.WeatherEnsemblePaperStrategy
    WeatherEnsemblePaperStrategyConfig = module.WeatherEnsemblePaperStrategyConfig

from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket import PolymarketLiveDataClientFactory
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.config import CacheConfig
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

# HttpClient is imported dynamically inside functions to support test isolation

DEFAULT_OUTPUT_DIR = "/workspace/outputs"
DEFAULT_RECONNECT_DELAY = 2.0
DEFAULT_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_CACHE_HOST = "redis"
DEFAULT_CACHE_PORT = 6379
_SAFE_PRESET_SET = re.compile(r"^[A-Za-z0-9_-]+$")

# City coordinates for Open-Meteo ensemble forecast
CITY_COORDINATES = {
    "NYC": (40.7128, -74.0060),
    "Chicago": (41.8781, -87.6298),
    "Miami": (25.7617, -80.1918),
    "Los Angeles": (34.0522, -118.2437),
    "San Francisco": (37.7749, -122.4194),
    "Seattle": (47.6062, -122.3321),
    "Denver": (39.7392, -104.9903),
    "Houston": (29.7604, -95.3698),
    "Dallas": (32.7767, -96.7970),
    "Austin": (30.2672, -97.7431),
    "Atlanta": (33.7490, -84.3880),
    "London": (51.5074, -0.1278),
    "Paris": (48.8566, 2.3522),
    "Madrid": (40.4168, -3.7038),
    "Amsterdam": (52.3676, 4.9041),
    "Munich": (48.1351, 11.5820),
    "Milan": (45.4642, 9.1900),
    "Warsaw": (52.2297, 21.0122),
    "Helsinki": (60.1695, 24.9354),
    "Ankara": (39.9334, 32.8597),
    "Tokyo": (35.6762, 139.6503),
    "Seoul": (37.5665, 126.9780),
    "Busan": (35.1796, 129.0756),
    "Taipei": (25.0330, 121.5654),
    "Singapore": (1.3521, 103.8198),
    "Kuala Lumpur": (3.1390, 101.6869),
    "Jakarta": (-6.2088, 106.8456),
    "Manila": (14.5994, 120.9842),
    "Beijing": (39.9042, 116.4074),
    "Shanghai": (31.2304, 121.4737),
    "Shenzhen": (22.5431, 114.0579),
    "Guangzhou": (23.1291, 113.2644),
    "Chongqing": (29.4316, 106.9123),
    "Chengdu": (30.5728, 104.0668),
    "Wuhan": (30.5928, 114.3055),
    "Lucknow": (26.8467, 80.9462),
    "Karachi": (24.8607, 67.0011),
    "Jeddah": (21.5433, 39.1728),
    "Lagos": (6.5244, 3.3792),
    "Cape Town": (-33.9249, 18.4241),
    "Buenos Aires": (-34.6037, -58.3816),
    "Sao Paulo": (-23.5505, -46.6333),
    "Mexico City": (19.4326, -99.1332),
    "Toronto": (43.6532, -79.3832),
    "Panama City": (8.9824, -79.5199),
    "Wellington": (-41.2865, 174.7762),
    "Istanbul": (41.0082, 28.9784),
    "Moscow": (55.7558, 37.6173),
    "Tel Aviv": (32.0853, 34.7818),
    "Hong Kong": (22.3193, 114.1694),
}


class RecoverableDaemonError(RuntimeError):
    """Raised for recoverable session or round runtime failures."""


_NODE_BUILD_TIMEOUT_SECS = 180


def _run_with_timeout(fn: Any, timeout_secs: int = _NODE_BUILD_TIMEOUT_SECS) -> Any:
    """Run a callable with a hard timeout using a lightweight shell watchdog."""
    import subprocess

    _pid = os.getpid()
    watchdog_proc = subprocess.Popen(
        ["/bin/sh", "-c", f"sleep {timeout_secs}; kill -9 {_pid}"],
        close_fds=True,
    )

    def _cancel_watchdog() -> None:
        try:
            watchdog_proc.terminate()
            watchdog_proc.wait(timeout=2)
        except Exception:
            pass

    try:
        result = fn()
        _cancel_watchdog()
        return result
    except Exception:
        _cancel_watchdog()
        raise


class JsonlRunWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()


def build_output_path(*, output_dir: str | Path, preset_set: str, now: datetime) -> Path:
    root = Path(output_dir)
    if ".." in root.parts:
        raise ValueError("output_dir must not contain '..'")
    if not _SAFE_PRESET_SET.fullmatch(str(preset_set).strip()):
        raise ValueError("preset_set must contain only letters, numbers, '_' or '-'")
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        root.resolve(strict=False)
        / "polymarket"
        / "weather_ensemble"
        / f"weather_ensemble_{preset_set}_{stamp}.jsonl"
    )


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _backoff_delay(reconnect_delay: float) -> float:
    base = max(0.0, float(reconnect_delay))
    return base + float(random.uniform(0.0, max(0.5, base * 0.5)))


def _strategy_presets_for_set(preset_set: str):
    normalized = str(preset_set).strip().lower()
    if normalized == "weather_ensemble_baseline":
        return weather_ensemble_presets()
    raise ValueError(f"unsupported preset set {preset_set!r}")


def build_daemon_node_config(
    *,
    instrument_ids: list[str],
    trader_id: str,
    cache_host: str,
    cache_port: int,
) -> TradingNodeConfig:
    # Cache/MessageBus: in-memory only (Redis unreachable via VPN routing)
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
            database=None,  # in-memory only
            timestamps_as_iso8601=True,
            persist_account_events=True,
            buffer_interval_ms=100,
            flush_on_start=False,
            use_instance_id=True,
        ),
        message_bus=MessageBusConfig(
            database=None,  # in-memory only
            timestamps_as_iso8601=True,
            buffer_interval_ms=100,
            streams_prefix="polymarket-weather-ensemble",
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
                starting_balances=[f"100 {USDC_POS}"],
                fee_model_path="nautilus_trader.adapters.polymarket.fee_model.PolymarketFeeModel",
            ),
        },
        timeout_connection=20.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )


async def _discover_weather_markets(
    http_client: Any,
    gamma_base_url: str,
) -> list[dict[str, Any]]:
    """Query Gamma API for active weather markets."""
    url = f"{gamma_base_url}/markets?tag=Weather&closed=false&limit=100"
    try:
        response = await http_client.request("GET", url, timeout_ms=15000)
        if response.status != 200:
            raise RecoverableDaemonError(f"Gamma API returned {response.status}")
        return json.loads(response.body)
    except Exception as e:
        raise RecoverableDaemonError(f"Failed to discover markets: {e}") from e


async def _fetch_forecasts(
    markets: list[DailyTemperatureMarket],
    forecast_client: OpenMeteoEnsembleForecastClient,
) -> dict[str, Any]:
    """Fetch ensemble forecasts for all cities in markets using httpx."""
    import httpx

    forecasts = {}
    cities_seen = set()

    async with httpx.AsyncClient() as http_client:
        for market in markets:
            if market.city in cities_seen:
                continue
            if market.city not in CITY_COORDINATES:
                continue

            cities_seen.add(market.city)
            try:
                lat, lon = CITY_COORDINATES[market.city]
                snapshot = await forecast_client.fetch_snapshot(
                    http_client=http_client,
                    latitude=lat,
                    longitude=lon,
                    target_date=market.observation_date,
                )
                if snapshot is not None:
                    forecasts[market.city] = snapshot
            except Exception:
                pass

    return forecasts


def _strategy_id_for_preset_and_market(preset_name: str, market_slug: str) -> StrategyId:
    """Generate unique strategy ID."""
    sanitized = re.sub(r"[^a-z0-9_-]", "_", market_slug.lower())
    return StrategyId(f"WEATHER-{preset_name.upper()}-{sanitized}")


async def _default_run_round(
    *,
    markets: list[DailyTemperatureMarket],
    candidates: list[Any],
    preset_set: str,
    http_client: Any,
) -> list[dict[str, Any]]:
    """Run trading node for ~90 seconds to collect quotes and enter trades."""
    presets = _strategy_presets_for_set(preset_set)
    accepted_candidates = [c for c in candidates if c.filter_status == "accepted"]

    if not accepted_candidates:
        return []

    instrument_ids = [c.condition_id for c in accepted_candidates]
    config = build_daemon_node_config(
        instrument_ids=instrument_ids,
        trader_id="PAPER-WEATHER-ENSEMBLE",
        cache_host=os.getenv("NAUTILUS_CACHE_HOST", DEFAULT_CACHE_HOST),
        cache_port=int(os.getenv("NAUTILUS_CACHE_PORT", str(DEFAULT_CACHE_PORT))),
    )

    def _create_and_build() -> TradingNode:
        n = TradingNode(config=config)
        for candidate in accepted_candidates:
            strategy = WeatherEnsemblePaperStrategy(
                config=WeatherEnsemblePaperStrategyConfig(
                    strategy_id=str(_strategy_id_for_preset_and_market(candidate.strategy_name, candidate.market_slug)),
                    condition_id=candidate.condition_id,
                    yes_token_id=candidate.yes_token_id,
                    no_token_id=candidate.no_token_id,
                    model_yes_probability=candidate.model_yes_probability,
                    market_yes_price=candidate.market_yes_price,
                    edge=candidate.edge,
                    selected_side=candidate.selected_side,
                    confidence=candidate.confidence,
                    close_positions_on_stop=True,
                ),
            )
            n.trader.add_strategy(strategy)
        n.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
        n.add_exec_client_factory(POLYMARKET, SandboxLiveExecClientFactory)
        n.build()
        return n

    node = _run_with_timeout(_create_and_build)

    # Run node for ~90 seconds to collect quotes and enter
    await _run_node_until_deadline(node=node, duration_seconds=90.0)

    # Extract results from cache
    rows = []
    cache = node.cache
    for candidate in accepted_candidates:
        strategy_id = _strategy_id_for_preset_and_market(candidate.strategy_name, candidate.market_slug)
        instrument_id = InstrumentId.from_str(candidate.condition_id)

        closed_positions = list(cache.positions_closed(instrument_id=instrument_id, strategy_id=strategy_id))
        open_positions = list(cache.positions_open(instrument_id=instrument_id, strategy_id=strategy_id))

        if closed_positions:
            position = closed_positions[-1]
            entry_price = float(position.avg_px_open)
            exit_price = float(position.avg_px_close) if getattr(position, "avg_px_close", 0.0) else None
            shares = float(position.peak_qty or position.quantity)
            stake = entry_price * shares
            pnl = float(position.realized_pnl.as_double()) if getattr(position, "realized_pnl", None) else None
        elif open_positions:
            position = open_positions[-1]
            entry_price = float(position.avg_px_open)
            exit_price = None
            shares = float(position.quantity)
            stake = entry_price * shares
            pnl = None
        else:
            entry_price = None
            exit_price = None
            shares = None
            stake = None
            pnl = None

        rows.append(
            {
                "event": "strategy_result",
                "strategy_name": candidate.strategy_name,
                "market_slug": candidate.market_slug,
                "city": candidate.city,
                "threshold": candidate.threshold,
                "band_type": candidate.band_type,
                "forecast_source": candidate.forecast_source,
                "model_yes_probability": candidate.model_yes_probability,
                "market_yes_price": candidate.market_yes_price,
                "edge": candidate.edge,
                "selected_side": candidate.selected_side,
                "confidence": candidate.confidence,
                "condition_id": candidate.condition_id,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "shares": shares,
                "stake": stake,
                "pnl": pnl,
                "resolved": False,
                "observation_date": candidate.observation_date,
                "metric": candidate.metric,
            },
        )

    return rows


async def _run_node_until_deadline(
    *,
    node: TradingNode,
    duration_seconds: float,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run node for bounded duration, then stop."""
    run_task = asyncio.create_task(node.run_async())
    try:
        await sleeper(max(0.0, float(duration_seconds)))
        try:
            await asyncio.wait_for(node.stop_async(), timeout=30.0)
        except asyncio.TimeoutError:
            run_task.cancel()
        try:
            await asyncio.wait_for(run_task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    finally:
        if not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass


async def run_daemon(
    *,
    preset_set: str,
    resolve_markets: Callable[[], Awaitable[list[DailyTemperatureMarket]]],
    build_candidates: Callable[
        [list[DailyTemperatureMarket]], Awaitable[list[Any]]
    ],
    run_round: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    sleep_between_rounds: Callable[[float], Awaitable[None]] = asyncio.sleep,
    writer: JsonlRunWriter | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    max_rounds: int = 0,
    poll_interval: float = 300.0,
) -> None:
    """Run daemon loop: discover markets, evaluate signals, run trading rounds."""
    if run_round is None:
        run_round = _default_run_round

    round_num = 0
    while True:
        if max_rounds > 0 and round_num >= max_rounds:
            break

        round_num += 1
        now = now_fn()

        try:
            writer.write({"event": "round_start", "round": round_num, "timestamp": now.isoformat()})

            # Discover markets
            markets = await resolve_markets()
            if not markets:
                writer.write(
                    {
                        "event": "round_skipped",
                        "round": round_num,
                        "reason": "no_markets",
                        "timestamp": now.isoformat(),
                    },
                )
                await sleep_between_rounds(poll_interval)
                continue

            writer.write(
                {
                    "event": "market_discovered",
                    "market_count": len(markets),
                    "timestamp": now.isoformat(),
                },
            )

            # Build candidates
            candidates = await build_candidates(markets)
            writer.write(
                {
                    "event": "markets_filtered",
                    "market_count": len(markets),
                    "candidate_count": len(candidates),
                    "accepted_count": sum(1 for c in candidates if c.filter_status == "accepted"),
                    "timestamp": now.isoformat(),
                },
            )

            for candidate in candidates:
                writer.write(
                    {
                        "event": "candidate_evaluated",
                        "strategy_name": candidate.strategy_name,
                        "market_slug": candidate.market_slug,
                        "city": candidate.city,
                        "threshold": candidate.threshold,
                        "band_type": candidate.band_type,
                        "forecast_source": candidate.forecast_source,
                        "model_yes_probability": candidate.model_yes_probability,
                        "market_yes_price": candidate.market_yes_price,
                        "edge": candidate.edge,
                        "selected_side": candidate.selected_side,
                        "confidence": candidate.confidence,
                        "filter_status": candidate.filter_status,
                        "filter_reasons": list(candidate.filter_reasons),
                        "observation_date": candidate.observation_date,
                        "metric": candidate.metric,
                        "timestamp": now.isoformat(),
                    },
                )

            # Run trading round only if there are accepted candidates
            accepted_candidates = [c for c in candidates if c.filter_status == "accepted"]
            results: list[dict[str, Any]] = []
            if accepted_candidates:
                # Only instantiate HttpClient if using default run_round
                if run_round is _default_run_round:
                    from nautilus_trader.core.nautilus_pyo3 import HttpClient as _HttpClient
                    http_client_arg = _HttpClient()
                    results = await run_round(
                        markets=markets,
                        candidates=candidates,
                        preset_set=preset_set,
                        http_client=http_client_arg,
                    )
                else:
                    results = await run_round(
                        markets=markets,
                        candidates=candidates,
                        preset_set=preset_set,
                    )

                for result in results:
                    writer.write(result)

            writer.write(
                {
                    "event": "round_end",
                    "round": round_num,
                    "result_count": len(results),
                    "timestamp": now.isoformat(),
                },
            )

        except RecoverableDaemonError as e:
            writer.write(
                {
                    "event": "error",
                    "round": round_num,
                    "error_type": "recoverable",
                    "message": str(e),
                    "timestamp": now.isoformat(),
                },
            )
            delay = _backoff_delay(DEFAULT_RECONNECT_DELAY)
            await sleep_between_rounds(delay)
        except Exception as e:
            writer.write(
                {
                    "event": "error",
                    "round": round_num,
                    "error_type": "unexpected",
                    "message": str(e),
                    "timestamp": now.isoformat(),
                },
            )
            delay = _backoff_delay(DEFAULT_RECONNECT_DELAY * 2)
            await sleep_between_rounds(delay)

        await sleep_between_rounds(poll_interval)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset-set", default="weather_ensemble_baseline", help="strategy preset set to run")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="base output directory")
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL, help="Gamma API base URL")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout seconds")
    parser.add_argument("--max-rounds", type=int, default=0, help="stop after N rounds, 0 means run forever")
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=DEFAULT_RECONNECT_DELAY,
        help="seconds to wait after recoverable errors",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=300.0,
        help="seconds between rounds",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    output_path = build_output_path(
        output_dir=args.output_dir,
        preset_set=args.preset_set,
        now=datetime.now(tz=UTC),
    )
    writer = JsonlRunWriter(output_path)

    def handle_signal(signum: int, frame: Any) -> None:
        writer.write({"event": "daemon_stopped", "signal": signum})
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    async def _resolve_markets() -> list[DailyTemperatureMarket]:
        """Resolve markets via Polymarket resolver.

        Discovers all near-term weather temperature markets (today/tomorrow) with
        supported band types (or_higher, or_lower). Filters out exact band types
        since ensemble forecasters cannot reliably predict exact temperature.
        """
        try:
            from nautilus_trader.core.nautilus_pyo3 import HttpClient as _HttpClient
            markets = await resolve_daily_temperature_markets(
                http_client=_HttpClient(),
                gamma_base_url=args.gamma_host,
                timeout_seconds=args.timeout,
            )
            # Discover ALL near-term markets across all 50 cities, not just 20
            # Filter returns: or_higher + or_lower markets for today/tomorrow, sorted by date+type+city
            return filter_tradeable_daily_temperature_markets(
                markets,
                today=date.today(),
            )
        except Exception as e:
            raise RecoverableDaemonError(f"Failed to resolve markets: {e}") from e

    async def _build_candidates(markets: list[DailyTemperatureMarket]) -> list[Any]:
        """Build signal-evaluated candidates."""
        forecast_client = OpenMeteoEnsembleForecastClient(
            config=OpenMeteoEnsembleForecastConfig(),
        )
        presets = _strategy_presets_for_set(args.preset_set)
        signal_engine = WeatherEnsembleSignalEngine(config=WeatherEnsembleSignalConfig())

        forecasts = await _fetch_forecasts(markets, forecast_client)
        candidates = []

        for market in markets:
            if market.city not in forecasts:
                candidates.append(
                    normalize_candidate_payload(
                        payload={
                            "strategy_name": presets[0].name,
                            "filter_status": "skipped",
                            "filter_reasons": ("forecast_unavailable",),
                        },
                        preset=presets[0],
                        market_slug=market.slug,
                        city=market.city,
                        threshold=market.threshold_f,
                        band_type=market.band_type,
                        condition_id=market.condition_id,
                        yes_token_id=market.yes_token_id,
                        no_token_id=market.no_token_id,
                        observation_date=str(market.observation_date),
                        metric=market.metric,
                    ),
                )
                continue

            forecast = forecasts[market.city]
            market_snapshot = WeatherMarketSnapshot(
                market_slug=market.slug,
                city=market.city,
                observation_date=market.observation_date,
                metric=market.metric,
                band_type=market.band_type,
                threshold=market.threshold_f,
                yes_price=market.best_ask if market.best_ask is not None else 0.5,
            )
            for preset in presets:
                decision = signal_engine.evaluate(
                    market=market_snapshot,
                    forecast=forecast,
                )

                candidate = normalize_candidate_payload(
                    payload={
                        "strategy_name": preset.name,
                        "model_yes_probability": decision.model_yes_probability,
                        "market_yes_price": decision.market_yes_price,
                        "edge": decision.edge,
                        "selected_side": decision.selected_side,
                        "confidence": decision.confidence,
                        "filter_status": decision.filter_status,
                        "filter_reasons": decision.filter_reasons,
                    },
                    preset=preset,
                    market_slug=market.slug,
                    city=market.city,
                    threshold=market.threshold_f,
                    band_type=market.band_type,
                    condition_id=market.condition_id,
                    yes_token_id=market.yes_token_id,
                    no_token_id=market.no_token_id,
                    observation_date=str(market.observation_date),
                    metric=market.metric,
                )
                candidates.append(candidate)

        return candidates

    try:
        asyncio.run(
            run_daemon(
                preset_set=args.preset_set,
                resolve_markets=_resolve_markets,
                build_candidates=_build_candidates,
                writer=writer,
                max_rounds=args.max_rounds,
                poll_interval=args.poll_interval,
            ),
        )
        return 0
    except KeyboardInterrupt:
        writer.write({"event": "daemon_interrupted"})
        return 130
    except Exception as e:
        writer.write({"event": "daemon_fatal_error", "message": str(e)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
