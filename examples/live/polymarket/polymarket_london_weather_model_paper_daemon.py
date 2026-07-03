#!/usr/bin/env python3
"""
London highest-temperature model paper daemon.

This runner is intentionally paper-only: live Polymarket data is allowed only
after the preflight passes, while all order flow is routed through Nautilus
sandbox execution.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import date
from datetime import datetime
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

try:
    from examples.live.polymarket.london_weather_market_filter import (
        resolve_tradeable_london_weather_markets,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.london_weather_market_filter"
    module_path = Path(__file__).resolve().with_name("london_weather_market_filter.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    resolve_tradeable_london_weather_markets = module.resolve_tradeable_london_weather_markets

try:
    from examples.live.polymarket.london_weather_model_bridge import build_london_model_candidates
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.london_weather_model_bridge"
    module_path = Path(__file__).resolve().with_name("london_weather_model_bridge.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    build_london_model_candidates = module.build_london_model_candidates

try:
    from examples.live.polymarket.polymarket_london_weather_preflight import run_preflight
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.polymarket_london_weather_preflight"
    module_path = Path(__file__).resolve().with_name("polymarket_london_weather_preflight.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    run_preflight = module.run_preflight

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

try:
    from examples.live.polymarket.weather_ensemble_strategy_library import (
        WeatherEnsembleCandidate,
        WeatherEnsembleStrategyPreset,
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
    WeatherEnsembleCandidate = module.WeatherEnsembleCandidate
    WeatherEnsembleStrategyPreset = module.WeatherEnsembleStrategyPreset

from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket import PolymarketLiveDataClientFactory
try:
    from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
except ModuleNotFoundError:
    def get_polymarket_instrument_id(condition_id: str, token_id: str | int) -> InstrumentId:
        return InstrumentId.from_str(f"{condition_id}-{token_id}.{POLYMARKET_VENUE}")
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.config import CacheConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import MessageBusConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId


DEFAULT_OUTPUT_DIR = "/workspace/outputs"
DEFAULT_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_CACHE_HOST = "redis"
DEFAULT_CACHE_PORT = 6379
DEFAULT_TRADER_ID = "PAPER-LONDON-WEATHER-MODEL"
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_-]+$")


class JsonlRunWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.write("\n")
            handle.flush()


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def build_output_path(*, output_dir: str | Path, label: str, now: datetime) -> Path:
    if not _SAFE_LABEL.fullmatch(label):
        raise ValueError("label must contain only letters, numbers, '_' or '-'")
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        Path(output_dir).resolve(strict=False)
        / "polymarket"
        / "london_weather_model"
        / f"london_weather_model_{label}_{stamp}.jsonl"
    )


def london_weather_model_preset(
    *,
    min_edge: float,
    target_usd_per_market: float,
    max_total_open_stake: float,
) -> WeatherEnsembleStrategyPreset:
    return WeatherEnsembleStrategyPreset(
        name="london_weather_model",
        max_entry_price=0.90,
        max_spread=0.04,
        min_ask_size=1.0,
        min_edge=float(min_edge),
        min_confidence=0.0,
        target_usd_per_market=float(target_usd_per_market),
        min_order_size_shares=1.0,
        max_stake_per_market=float(target_usd_per_market),
        max_open_positions=1,
        max_total_open_stake=float(max_total_open_stake),
        one_position_per_family=True,
    )


def build_daemon_node_config(
    *,
    instrument_ids: list[str],
    trader_id: str = DEFAULT_TRADER_ID,
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
            database=None,
            timestamps_as_iso8601=True,
            persist_account_events=True,
            buffer_interval_ms=100,
            flush_on_start=False,
            use_instance_id=True,
        ),
        message_bus=MessageBusConfig(
            database=None,
            timestamps_as_iso8601=True,
            buffer_interval_ms=100,
            streams_prefix="polymarket-london-weather-model",
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


def _strategy_id_for_candidate(candidate: WeatherEnsembleCandidate) -> StrategyId:
    slug = re.sub(r"[^a-z0-9_-]", "_", candidate.market_slug.lower())
    return StrategyId(f"LONDON-WEATHER-{slug}")


def _instrument_id_for_candidate(candidate: WeatherEnsembleCandidate) -> InstrumentId:
    token_id = candidate.no_token_id if candidate.selected_side == "no" else candidate.yes_token_id
    return get_polymarket_instrument_id(candidate.condition_id, token_id)


def _family_instrument_ids_for_candidate(candidate: WeatherEnsembleCandidate) -> tuple[str, str]:
    return (
        str(get_polymarket_instrument_id(candidate.condition_id, candidate.yes_token_id)),
        str(get_polymarket_instrument_id(candidate.condition_id, candidate.no_token_id)),
    )


def _strategy_for_candidate(
    *,
    candidate: WeatherEnsembleCandidate,
    preset: WeatherEnsembleStrategyPreset,
    family_instrument_ids: tuple[str, ...],
) -> WeatherEnsemblePaperStrategy:
    instrument_id = _instrument_id_for_candidate(candidate)
    config = WeatherEnsemblePaperStrategyConfig(
        strategy_id=str(_strategy_id_for_candidate(candidate)),
        instrument_id=instrument_id,
        condition_id=candidate.condition_id,
        yes_token_id=candidate.yes_token_id,
        no_token_id=candidate.no_token_id,
        model_yes_probability=candidate.model_yes_probability,
        market_yes_price=candidate.market_yes_price,
        edge=candidate.edge,
        selected_side=candidate.selected_side,
        confidence=candidate.confidence,
        preset=preset,
        family_instrument_ids=family_instrument_ids,
        target_usd_per_market=Decimal(str(preset.target_usd_per_market)),
        min_order_size_shares=Decimal(str(preset.min_order_size_shares)),
        max_stake_per_market=Decimal(str(preset.max_stake_per_market)),
        max_open_positions=preset.max_open_positions,
        max_total_open_stake=Decimal(str(preset.max_total_open_stake)),
        close_positions_on_stop=True,
    )
    return WeatherEnsemblePaperStrategy(config=config)


async def _run_node_until_deadline(
    *,
    node: TradingNode,
    duration_seconds: float,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    loop = asyncio.get_running_loop()
    run_future = loop.run_in_executor(None, node.run)
    try:
        await sleeper(max(0.0, duration_seconds))
    finally:
        await loop.run_in_executor(None, node.stop)
        await run_future


async def run_paper_round(
    *,
    candidates: list[WeatherEnsembleCandidate],
    preset: WeatherEnsembleStrategyPreset,
    duration_seconds: float = 90.0,
    node_factory: Callable[[TradingNodeConfig], TradingNode] = TradingNode,
) -> list[dict[str, Any]]:
    accepted = [candidate for candidate in candidates if candidate.filter_status == "accepted"]
    if not accepted:
        return []

    instrument_ids = [str(_instrument_id_for_candidate(candidate)) for candidate in accepted]
    config = build_daemon_node_config(instrument_ids=instrument_ids)
    node = node_factory(config)
    for candidate in accepted:
        node.trader.add_strategy(
            _strategy_for_candidate(
                candidate=candidate,
                preset=preset,
                family_instrument_ids=_family_instrument_ids_for_candidate(candidate),
            ),
        )
    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    node.add_exec_client_factory(POLYMARKET, SandboxLiveExecClientFactory)
    node.build()

    await _run_node_until_deadline(node=node, duration_seconds=duration_seconds)
    return [_result_row(candidate) for candidate in accepted]


def _result_row(candidate: WeatherEnsembleCandidate) -> dict[str, Any]:
    return {
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
        "instrument_id": str(_instrument_id_for_candidate(candidate)),
        "token_side": candidate.selected_side,
        "yes_token_id": candidate.yes_token_id,
        "no_token_id": candidate.no_token_id,
        "entry_price": None,
        "stake": None,
        "accounting_status": "submitted_to_sandbox",
    }


async def run_daemon(
    *,
    preflight: dict[str, Any],
    resolve_markets: Callable[[], Awaitable[list[Any]]],
    build_candidates: Callable[[list[Any]], Awaitable[list[WeatherEnsembleCandidate]]],
    writer: JsonlRunWriter,
    preset: WeatherEnsembleStrategyPreset,
    max_rounds: int,
    run_round: Callable[..., Awaitable[list[dict[str, Any]]]] = run_paper_round,
    now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> None:
    writer.write({"event": "preflight", **preflight})
    if not preflight.get("ready_for_paper_round"):
        writer.write({"event": "blocked", "timestamp": now_fn().isoformat()})
        return

    rounds = max(1, int(max_rounds))
    for round_index in range(rounds):
        writer.write({"event": "round_start", "round_index": round_index, "timestamp": now_fn().isoformat()})
        markets = await resolve_markets()
        for market in markets:
            writer.write(
                {
                    "event": "market_discovered",
                    "market_slug": getattr(market, "slug", ""),
                    "city": getattr(market, "city", ""),
                    "threshold": getattr(market, "threshold_f", None),
                    "band_type": getattr(market, "band_type", ""),
                    "condition_id": getattr(market, "condition_id", ""),
                },
            )
        candidates = await build_candidates(markets)
        for candidate in candidates:
            writer.write(
                {
                    "event": "candidate_evaluated",
                    "strategy_name": candidate.strategy_name,
                    "market_slug": candidate.market_slug,
                    "filter_status": candidate.filter_status,
                    "filter_reasons": list(candidate.filter_reasons),
                    "model_yes_probability": candidate.model_yes_probability,
                    "market_yes_price": candidate.market_yes_price,
                    "edge": candidate.edge,
                    "selected_side": candidate.selected_side,
                    "condition_id": candidate.condition_id,
                },
            )
        for row in await run_round(candidates=candidates, preset=preset):
            writer.write(row)
        writer.write({"event": "round_end", "round_index": round_index, "timestamp": now_fn().isoformat()})


def _model_snapshot_rows(preflight: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = preflight.get("model_snapshot")
    if snapshot is None:
        return []
    if isinstance(snapshot, list):
        return [dict(row) for row in snapshot if isinstance(row, dict)]
    if isinstance(snapshot, dict):
        return [snapshot]
    return []


def _markets_from_preflight(preflight: dict[str, Any]) -> list[Any]:
    markets = preflight.get("accepted_markets")
    if not isinstance(markets, list):
        return []
    return [SimpleMarket(**market) for market in markets if isinstance(market, dict)]


class SimpleMarket:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-json", default="", help="deterministic preflight/model fixture")
    parser.add_argument("--max-rounds", type=int, default=1)
    parser.add_argument("--min-edge", type=float, default=0.08)
    parser.add_argument("--target-usd-per-market", type=float, default=1.0)
    parser.add_argument("--max-total-open-stake", type=float, default=5.0)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--round-seconds", type=float, default=90.0)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    output_path = build_output_path(
        output_dir=args.output_dir,
        label="paper",
        now=datetime.now(tz=UTC),
    )
    writer = JsonlRunWriter(output_path)
    preflight = run_preflight(
        fixture_path=args.fixture_json or None,
        no_network=bool(args.fixture_json),
    )
    preset = london_weather_model_preset(
        min_edge=args.min_edge,
        target_usd_per_market=args.target_usd_per_market,
        max_total_open_stake=args.max_total_open_stake,
    )

    async def _resolve_markets() -> list[Any]:
        preflight_markets = _markets_from_preflight(preflight)
        if preflight_markets:
            return preflight_markets

        from nautilus_trader.core.nautilus_pyo3 import HttpClient

        result = await resolve_tradeable_london_weather_markets(
            http_client=HttpClient(),
            gamma_base_url=args.gamma_host,
            today=date.today(),
            timeout_seconds=args.timeout,
        )
        return result.accepted

    async def _build_candidates(markets: list[Any]) -> list[WeatherEnsembleCandidate]:
        return build_london_model_candidates(
            markets,
            _model_snapshot_rows(preflight),
            min_edge=args.min_edge,
            preset=preset,
        )

    asyncio.run(
        run_daemon(
            preflight=preflight,
            resolve_markets=_resolve_markets,
            build_candidates=_build_candidates,
            writer=writer,
            preset=preset,
            max_rounds=args.max_rounds,
            run_round=lambda **kwargs: run_paper_round(duration_seconds=args.round_seconds, **kwargs),
        ),
    )
    print(f"Wrote London weather model paper log: {output_path}")
    return 0 if preflight.get("ready_for_paper_round") else 1


if __name__ == "__main__":
    raise SystemExit(main())
