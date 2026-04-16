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
Long-running Polymarket 5-minute BTC paper-trading daemon.

This daemon rolls from one 5-minute session to the next, persists JSONL run
metadata under ``outputs/polymarket/runs/``, and reuses the existing 5-minute
session resolver and strategy preset library.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import uuid
from typing import Any
from typing import Literal

try:
    from examples.live.polymarket._crypto_5m_support import DEFAULT_GAMMA_BASE_URL
    from examples.live.polymarket._crypto_5m_support import SUPPORTED_ASSETS
    from examples.live.polymarket._crypto_5m_support import resolve_crypto_5m_session
    from examples.live.polymarket._crypto_5m_support import validate_http_base_url
except ModuleNotFoundError:
    module_name = "examples.live.polymarket._crypto_5m_support"
    module_path = Path(__file__).resolve().with_name("_crypto_5m_support.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    DEFAULT_GAMMA_BASE_URL = module.DEFAULT_GAMMA_BASE_URL
    SUPPORTED_ASSETS = module.SUPPORTED_ASSETS
    resolve_crypto_5m_session = module.resolve_crypto_5m_session
    validate_http_base_url = module.validate_http_base_url

try:
    from examples.live.polymarket.crypto_5m_strategy_library import all_strategy_presets
    from examples.live.polymarket.crypto_5m_strategy_library import entry_grid_strategy_presets
    from examples.live.polymarket.crypto_5m_strategy_library import first_wave_strategy_presets
    from examples.live.polymarket.crypto_5m_strategy_library import live_edge_strategy_presets
    from examples.live.polymarket.crypto_5m_strategy_library import research_strategy_presets
    from examples.live.polymarket.crypto_5m_strategy_library import smoke_test_strategy_presets
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.crypto_5m_strategy_library"
    module_path = Path(__file__).resolve().with_name("crypto_5m_strategy_library.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    all_strategy_presets = module.all_strategy_presets
    entry_grid_strategy_presets = module.entry_grid_strategy_presets
    first_wave_strategy_presets = module.first_wave_strategy_presets
    live_edge_strategy_presets = module.live_edge_strategy_presets
    research_strategy_presets = module.research_strategy_presets
    smoke_test_strategy_presets = module.smoke_test_strategy_presets

try:
    from examples.live.polymarket.crypto_5m_live_strategy import PolymarketCrypto5mPaperStrategy
    from examples.live.polymarket.crypto_5m_live_strategy import PolymarketCrypto5mPaperStrategyConfig
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.crypto_5m_live_strategy"
    module_path = Path(__file__).resolve().with_name("crypto_5m_live_strategy.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    PolymarketCrypto5mPaperStrategy = module.PolymarketCrypto5mPaperStrategy
    PolymarketCrypto5mPaperStrategyConfig = module.PolymarketCrypto5mPaperStrategyConfig

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
from nautilus_trader.core.nautilus_pyo3 import HttpClient
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.identifiers import TraderId
DEFAULT_OUTPUT_DIR = "/workspace/outputs"
DEFAULT_RECONNECT_DELAY = 2.0
DEFAULT_EXECUTION_CUTOFF_SECONDS = 15.0
DEFAULT_CACHE_HOST = "redis"
DEFAULT_CACHE_PORT = 6379
_SAFE_PRESET_SET = re.compile(r"^[A-Za-z0-9_-]+$")


class RecoverableDaemonError(RuntimeError):
    """Raised for recoverable session or round runtime failures."""


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
    parser.add_argument("--asset", default="BTC", help=f"one of {', '.join(SUPPORTED_ASSETS)}")
    parser.add_argument("--preset-set", default="quant", help="strategy preset set to run")
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
        "--execution-cutoff-seconds",
        type=float,
        default=DEFAULT_EXECUTION_CUTOFF_SECONDS,
        help="stop the round runner this many seconds before market end",
    )
    return parser


def build_output_path(*, output_dir: str | Path, preset_set: str, now: datetime) -> Path:
    root = Path(output_dir)
    if ".." in root.parts:
        raise ValueError("output_dir must not contain '..'")
    if not _SAFE_PRESET_SET.fullmatch(str(preset_set).strip()):
        raise ValueError("preset_set must contain only letters, numbers, '_' or '-'")
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return root.resolve(strict=False) / "polymarket" / "runs" / f"overnight_{preset_set}_{stamp}.jsonl"


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
    if normalized == "quant":
        return first_wave_strategy_presets()
    if normalized == "grid":
        return entry_grid_strategy_presets()
    if normalized == "smoke":
        return smoke_test_strategy_presets()
    if normalized == "edge":
        return live_edge_strategy_presets()
    if normalized == "research":
        return research_strategy_presets()
    if normalized in {"all", "advanced"}:
        return all_strategy_presets()
    if normalized == "momentum":
        return tuple(preset for preset in all_strategy_presets() if "momentum" in preset.mode)
    if normalized == "flow":
        return tuple(preset for preset in all_strategy_presets() if "flow" in preset.mode)
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
            streams_prefix="polymarket-5m",
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
        timeout_connection=20.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )


def _build_paper_strategy(
    *,
    preset: Any,
    instrument_id: Any,
    market_end_time: datetime,
    order_qty: Decimal = Decimal(10),
    token_side: Literal["up", "down"] = "up",
) -> PolymarketCrypto5mPaperStrategy:
    normalized_side = _normalize_token_side(token_side)
    return PolymarketCrypto5mPaperStrategy(
        config=PolymarketCrypto5mPaperStrategyConfig(
            strategy_id=str(_strategy_id_for_preset(preset, normalized_side)),
            instrument_id=instrument_id,
            preset=preset,
            market_end_time=market_end_time,
            order_qty=Decimal(str(order_qty)),
            token_side=normalized_side,
            close_positions_on_stop=True,
        ),
    )


def _normalize_token_side(token_side: str) -> Literal["up", "down"]:
    normalized = str(token_side).strip().lower()
    if normalized not in {"up", "down"}:
        raise ValueError(f"token_side must be 'up' or 'down', got {token_side!r}")
    return normalized  # type: ignore[return-value]


def _strategy_name_for_preset(preset: Any, token_side: str) -> str:
    return f"{preset.name}_{_normalize_token_side(token_side)}"


def _strategy_key_for_preset(preset: Any, token_side: str) -> str:
    return _strategy_name_for_preset(preset, token_side)


def _strategy_id_for_preset(preset: Any, token_side: str = "up") -> StrategyId:
    return StrategyId(f"PM5M-{preset.name.upper()}-{_normalize_token_side(token_side).upper()}")


def _as_decimal(value: Any) -> Decimal:
    if hasattr(value, "as_decimal"):
        return Decimal(str(value.as_decimal()))
    return Decimal(str(value))


def _iso8601_from_unix_nanos(timestamp_ns: int | None) -> str | None:
    if not timestamp_ns:
        return None
    return unix_nanos_to_dt(int(timestamp_ns)).isoformat()


def _configured_max_entry_price(preset: Any) -> float:
    max_entry_price = float(getattr(preset, "max_entry_price", 0.0) or 0.0)
    return max_entry_price if max_entry_price > 0 else float(preset.exit_price)


def _configured_entry_fields(preset: Any) -> dict[str, float]:
    return {
        "configured_entry_price": float(preset.entry_price),
        "configured_max_entry_price": _configured_max_entry_price(preset),
    }


def _position_entry_side(position: Any) -> str:
    return str(getattr(getattr(position, "entry", None), "name", "BUY")).lower()


def _entry_in_configured_band(*, preset: Any, entry_price: float, entry_side: str) -> bool:
    if entry_side != "buy":
        return False
    return float(preset.entry_price) <= entry_price <= _configured_max_entry_price(preset)


def _position_row(
    *,
    position: Any,
    preset: Any,
    instrument_id: str,
    token_side: str,
    asset: str,
    slug: str,
    exit_reason: str,
    settled: bool,
) -> dict[str, Any]:
    shares_quantity = getattr(position, "peak_qty", None) or position.quantity
    shares = float(_as_decimal(shares_quantity))
    entry_price = float(position.avg_px_open)
    stake = float(Decimal(str(entry_price)) * Decimal(str(shares)))
    entry_side = _position_entry_side(position)
    entry_in_band = _entry_in_configured_band(
        preset=preset,
        entry_price=entry_price,
        entry_side=entry_side,
    )
    accounting_status = "settled" if settled else "open"
    row_exit_reason = exit_reason
    row_settled = settled
    if entry_side != "buy":
        accounting_status = "invalid_entry_side"
        row_exit_reason = "accounting_invalid"
        row_settled = False
    elif row_settled and not entry_in_band:
        accounting_status = "invalid_entry_band"
        row_exit_reason = "accounting_invalid"
        row_settled = False

    realized_pnl = None
    if row_settled and getattr(position, "realized_pnl", None) is not None:
        realized_pnl = float(position.realized_pnl.as_double())
    realized_return = float(getattr(position, "realized_return", 0.0)) if row_settled else None
    exit_price = None
    if row_settled:
        exit_price = float(position.avg_px_close) if getattr(position, "avg_px_close", 0.0) else preset.exit_price

    return {
        "event": "strategy_result",
        "strategy_name": _strategy_name_for_preset(preset, token_side),
        "preset_name": preset.name,
        "token_side": _normalize_token_side(token_side),
        "strategy_mode": preset.mode,
        "rationale": preset.rationale,
        "runner": "polymarket_crypto_5m_paper_strategy",
        "entry_price": entry_price,
        **_configured_entry_fields(preset),
        "entry_in_configured_band": entry_in_band,
        "accounting_status": accounting_status,
        "exit_price": exit_price,
        "stop_loss_price": preset.stop_loss_price,
        "instrument_id": str(instrument_id),
        "asset": asset,
        "slug": slug,
        "exit_reason": row_exit_reason,
        "settled": row_settled,
        "pnl": realized_pnl,
        "roi": realized_return,
        "shares": shares,
        "stake": stake,
        "entry_time": _iso8601_from_unix_nanos(getattr(position, "ts_opened", 0)),
        "exit_time": _iso8601_from_unix_nanos(getattr(position, "ts_closed", 0)),
        "entry_side": entry_side,
    }


def extract_strategy_results(
    *,
    cache: Any,
    presets: tuple[Any, ...] | list[Any],
    instrument_id: str,
    token_side: str = "up",
    asset: str,
    slug: str,
    strategy_ids_by_preset: dict[str, StrategyId] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    normalized_side = _normalize_token_side(token_side)
    cache_instrument_id = InstrumentId.from_str(str(instrument_id))
    for preset in presets:
        strategy_key = _strategy_key_for_preset(preset, normalized_side)
        strategy_id = (
            strategy_ids_by_preset.get(strategy_key) or strategy_ids_by_preset.get(preset.name)
            if strategy_ids_by_preset is not None
            else None
        ) or _strategy_id_for_preset(preset, normalized_side)
        closed_positions = list(
            cache.positions_closed(
                instrument_id=cache_instrument_id,
                strategy_id=strategy_id,
            ),
        )
        if closed_positions:
            rows.append(
                _position_row(
                    position=closed_positions[-1],
                    preset=preset,
                    instrument_id=instrument_id,
                    token_side=normalized_side,
                    asset=asset,
                    slug=slug,
                    exit_reason="position_closed",
                    settled=True,
                ),
            )
            continue

        open_positions = list(
            cache.positions_open(
                instrument_id=cache_instrument_id,
                strategy_id=strategy_id,
            ),
        )
        if open_positions:
            rows.append(
                _position_row(
                    position=open_positions[-1],
                    preset=preset,
                    instrument_id=instrument_id,
                    token_side=normalized_side,
                    asset=asset,
                    slug=slug,
                    exit_reason="position_open",
                    settled=False,
                ),
            )
            continue

        rows.append(
            {
                "event": "strategy_result",
                "strategy_name": _strategy_name_for_preset(preset, normalized_side),
                "preset_name": preset.name,
                "token_side": normalized_side,
                "strategy_mode": preset.mode,
                "rationale": preset.rationale,
                "runner": "polymarket_crypto_5m_paper_strategy",
                "entry_price": preset.entry_price,
                **_configured_entry_fields(preset),
                "entry_in_configured_band": None,
                "accounting_status": "no_position",
                "exit_price": preset.exit_price,
                "stop_loss_price": preset.stop_loss_price,
                "instrument_id": str(instrument_id),
                "asset": asset,
                "slug": slug,
                "exit_reason": "no_position",
                "settled": False,
                "pnl": None,
                "roi": None,
                "shares": None,
                "stake": None,
                "entry_time": None,
                "exit_time": None,
                "entry_side": None,
            },
        )
    return rows


async def _run_node_until_deadline(
    *,
    node: TradingNode,
    duration_seconds: float,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    run_task = asyncio.create_task(node.run_async())
    try:
        await sleeper(max(0.0, float(duration_seconds)))
        await node.stop_async()
        await run_task
    finally:
        if not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass


async def _default_run_round(
    *,
    session: Any,
    asset: str,
    preset_set: str,
    execution_cutoff_seconds: float,
) -> list[dict[str, Any]]:
    presets = _strategy_presets_for_set(preset_set)
    instrument_ids_by_side = {
        "up": session.instrument_ids["up"],
        "down": session.instrument_ids["down"],
    }
    config = build_daemon_node_config(
        instrument_ids=[str(instrument_id) for instrument_id in instrument_ids_by_side.values()],
        trader_id="PAPER-5M-DAEMON",
        cache_host=os.getenv("NAUTILUS_CACHE_HOST", DEFAULT_CACHE_HOST),
        cache_port=int(os.getenv("NAUTILUS_CACHE_PORT", str(DEFAULT_CACHE_PORT))),
    )
    node = TradingNode(config=config)
    strategy_ids_by_preset: dict[str, StrategyId] = {}
    for token_side, instrument_id in instrument_ids_by_side.items():
        for preset in presets:
            strategy = _build_paper_strategy(
                preset=preset,
                instrument_id=instrument_id,
                market_end_time=session.end_time,
                token_side=_normalize_token_side(token_side),
            )
            node.trader.add_strategy(strategy)
            strategy_ids_by_preset[_strategy_key_for_preset(preset, token_side)] = strategy.id
    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    node.add_exec_client_factory(POLYMARKET, SandboxLiveExecClientFactory)
    node.build()

    now = datetime.now(tz=UTC)
    runtime_seconds = max(
        1.0,
        (session.end_time - now).total_seconds() - max(0.0, float(execution_cutoff_seconds)),
    )
    try:
        await _run_node_until_deadline(node=node, duration_seconds=runtime_seconds)
        rows: list[dict[str, Any]] = []
        for token_side, instrument_id in instrument_ids_by_side.items():
            rows.extend(
                extract_strategy_results(
                    cache=node.cache,
                    presets=presets,
                    instrument_id=str(instrument_id),
                    token_side=token_side,
                    asset=asset,
                    slug=session.slug,
                    strategy_ids_by_preset=strategy_ids_by_preset,
                ),
            )
        return rows
    except Exception as exc:  # pragma: no cover - exercised via orchestration wrapper
        raise RecoverableDaemonError(str(exc)) from exc
    finally:
        node.kernel.dispose()
        if node.kernel.executor:
            node.kernel.executor.shutdown(wait=True, cancel_futures=True)


async def _sleep_until_next_round(*, session: Any) -> None:
    delay = max(0.0, (session.end_time - datetime.now(tz=UTC)).total_seconds() + 1.0)
    if delay > 0:
        await asyncio.sleep(delay)


async def _resolve_session(asset: str, gamma_host: str, timeout: float):
    http_client = HttpClient(timeout_secs=max(1, math.ceil(timeout)))
    return await resolve_crypto_5m_session(
        asset=asset,
        http_client=http_client,
        gamma_base_url=validate_http_base_url(gamma_host, name="gamma_base_url"),
        timeout=timeout,
    )


async def run_daemon(
    *,
    asset: str,
    preset_set: str,
    resolve_session: Callable[[], Awaitable[Any]],
    run_round: Callable[..., Awaitable[list[dict[str, Any]]]],
    sleep_until_next_round: Callable[..., Awaitable[None]],
    writer: JsonlRunWriter,
    now_fn: Callable[[], datetime],
    max_rounds: int = 0,
    run_id: str | None = None,
    backoff_sleep: Callable[[float], Awaitable[None]] | None = None,
    reconnect_delay: float = DEFAULT_RECONNECT_DELAY,
    execution_cutoff_seconds: float = DEFAULT_EXECUTION_CUTOFF_SECONDS,
) -> None:
    rounds_completed = 0
    daemon_run_id = run_id or uuid.uuid4().hex
    backoff = backoff_sleep or asyncio.sleep

    while max_rounds <= 0 or rounds_completed < max_rounds:
        session_id = None
        started_at = now_fn().astimezone(UTC)
        try:
            session = await resolve_session()
        except Exception as exc:
            error_reason = str(exc)
            writer.write(
                {
                    "run_id": daemon_run_id,
                    "event": "error",
                    "asset": asset,
                    "preset_set": preset_set,
                    "reason": error_reason,
                    "timestamp": started_at.isoformat(),
                },
            )
            writer.write(
                {
                    "run_id": daemon_run_id,
                    "event": "round_skipped",
                    "asset": asset,
                    "preset_set": preset_set,
                    "reason": error_reason,
                    "timestamp": started_at.isoformat(),
                },
            )
            rounds_completed += 1
            await backoff(_backoff_delay(reconnect_delay))
            continue

        session_id = f"{session.slug}:{rounds_completed + 1}"
        writer.write(
            {
                "run_id": daemon_run_id,
                "session_id": session_id,
                "event": "round_start",
                "asset": asset,
                "preset_set": preset_set,
                "slug": session.slug,
                "instrument_ids": {side: str(value) for side, value in session.instrument_ids.items()},
                "market_end_time": session.end_time.isoformat(),
                "timestamp": started_at.isoformat(),
            },
        )

        try:
            rows = await run_round(
                session=session,
                asset=asset,
                preset_set=preset_set,
                execution_cutoff_seconds=execution_cutoff_seconds,
            )
        except RecoverableDaemonError as exc:
            error_reason = str(exc)
            writer.write(
                {
                    "run_id": daemon_run_id,
                    "session_id": session_id,
                    "event": "error",
                    "asset": asset,
                    "preset_set": preset_set,
                    "slug": session.slug,
                    "reason": error_reason,
                    "timestamp": now_fn().astimezone(UTC).isoformat(),
                },
            )
            writer.write(
                {
                    "run_id": daemon_run_id,
                    "session_id": session_id,
                    "event": "round_skipped",
                    "asset": asset,
                    "preset_set": preset_set,
                    "slug": session.slug,
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
            payload.setdefault("session_id", session_id)
            payload.setdefault("asset", asset)
            payload.setdefault("slug", session.slug)
            payload.setdefault("timestamp", now_fn().astimezone(UTC).isoformat())
            writer.write(payload)

        writer.write(
            {
                "run_id": daemon_run_id,
                "session_id": session_id,
                "event": "round_end",
                "asset": asset,
                "preset_set": preset_set,
                "slug": session.slug,
                "market_end_time": session.end_time.isoformat(),
                "timestamp": now_fn().astimezone(UTC).isoformat(),
            },
        )
        rounds_completed += 1
        if max_rounds > 0 and rounds_completed >= max_rounds:
            break
        await sleep_until_next_round(session=session)


async def _run_main_loop(
    *,
    asset: str,
    preset_set: str,
    output_dir: str,
    gamma_host: str,
    timeout: float,
    max_rounds: int,
    reconnect_delay: float,
    execution_cutoff_seconds: float,
) -> None:
    output_path = build_output_path(
        output_dir=output_dir,
        preset_set=preset_set,
        now=datetime.now(tz=UTC),
    )
    writer = JsonlRunWriter(output_path)
    await run_daemon(
        asset=asset,
        preset_set=preset_set,
        resolve_session=lambda: _resolve_session(asset=asset, gamma_host=gamma_host, timeout=timeout),
        run_round=_default_run_round,
        sleep_until_next_round=_sleep_until_next_round,
        writer=writer,
        now_fn=lambda: datetime.now(tz=UTC),
        max_rounds=max_rounds,
        reconnect_delay=reconnect_delay,
        execution_cutoff_seconds=execution_cutoff_seconds,
    )


def main() -> int:
    args = _build_parser().parse_args()
    asyncio.run(
        _run_main_loop(
            asset=str(args.asset).upper(),
            preset_set=str(args.preset_set),
            output_dir=str(args.output_dir),
            gamma_host=str(args.gamma_host),
            timeout=float(args.timeout),
            max_rounds=int(args.max_rounds),
            reconnect_delay=float(args.reconnect_delay),
            execution_cutoff_seconds=float(args.execution_cutoff_seconds),
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
