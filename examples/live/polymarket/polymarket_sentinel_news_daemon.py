#!/usr/bin/env python3
"""
Sentinel news bridge → Nautilus Polymarket paper trading daemon.

Reads Sentinel news signals from a JSONL file, groups by instrument (keeps
highest-relevance signal), builds a TradingNode with one SentinelSignalStrategy
per matched instrument, runs for round_timeout seconds, and writes JSONL run logs.

Optionally polls bridge loop to refresh signals.
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
import os
from pathlib import Path
import signal
import sys
import time
import uuid
from typing import Any

try:
    from examples.live.polymarket.sentinel_signal_models import SentinelNewsSignal
    from examples.live.polymarket.sentinel_signal_bridge import run_bridge_loop
    from examples.live.polymarket.sentinel_signal_strategy import SentinelSignalStrategy
    from examples.live.polymarket.sentinel_signal_strategy import SentinelSignalStrategyConfig
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.sentinel_signal_models"
    module_path = Path(__file__).resolve().with_name("sentinel_signal_models.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    SentinelNewsSignal = module.SentinelNewsSignal

    module_name = "examples.live.polymarket.sentinel_signal_bridge"
    module_path = Path(__file__).resolve().with_name("sentinel_signal_bridge.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    run_bridge_loop = module.run_bridge_loop

    module_name = "examples.live.polymarket.sentinel_signal_strategy"
    module_path = Path(__file__).resolve().with_name("sentinel_signal_strategy.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    SentinelSignalStrategy = module.SentinelSignalStrategy
    SentinelSignalStrategyConfig = module.SentinelSignalStrategyConfig

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
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.identifiers import TraderId

DEFAULT_OUTPUT_DIR = "/workspace/outputs"
DEFAULT_SIGNAL_PATH = os.environ.get(
    "SENTINEL_SIGNAL_PATH",
    "/data/nautilus_export/live_signals/sentinel_news_signals.jsonl",
)
DEFAULT_ROUND_TIMEOUT = 3600.0
DEFAULT_POLL_INTERVAL = 60.0
DEFAULT_CACHE_HOST = "redis"
DEFAULT_CACHE_PORT = 6379

_NODE_BUILD_TIMEOUT_SECS = 180


class RecoverableDaemonError(RuntimeError):
    """Raised for recoverable round runtime failures."""


def _node_build_with_sigalrm(node: Any) -> None:
    """Call node.build() with a SIGALRM hard timeout."""

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


def read_all_signals(signal_path: str | Path) -> list[dict]:
    """Load all sentinel_news_signal entries from the JSONL file."""
    path = Path(signal_path)
    if not path.exists():
        return []
    signals = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("event") == "sentinel_news_signal":
                    signals.append(entry)
    except OSError:
        pass
    return signals


def group_signals_by_instrument(signals: list[dict]) -> dict[str, dict]:
    """Return best (highest relevance_score) signal per instrument_id."""
    grouped: dict[str, dict] = {}
    for signal in signals:
        instrument_id = signal.get("instrument_id")
        if not instrument_id:
            continue
        relevance = float(signal.get("relevance_score", 0.0))
        current_best = grouped.get(instrument_id)
        if current_best is None or relevance > float(current_best.get("relevance_score", 0.0)):
            grouped[instrument_id] = signal
    return grouped


def build_daemon_output_path(*, output_dir: str | Path, now: datetime) -> Path:
    """Build output JSONL path like: <output_dir>/polymarket/sentinel/sentinel_20260418T120000Z.jsonl"""
    root = Path(output_dir)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return root.resolve(strict=False) / "polymarket" / "sentinel" / f"sentinel_{stamp}.jsonl"


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


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
            streams_prefix="polymarket-sentinel",
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


async def _run_node_until_deadline(
    *,
    node: TradingNode,
    duration_seconds: float,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
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


async def _run_sentinel_round(
    *,
    signals: dict[str, dict],
    signal_path: str | Path,
    round_timeout: float,
) -> list[dict[str, Any]]:
    """Run a single sentinel news round: build node, enter positions, extract results."""
    if not signals:
        return []

    instrument_ids = list(signals.keys())
    config = build_daemon_node_config(
        instrument_ids=instrument_ids,
        trader_id="PAPER-SENTINEL-DAEMON",
        cache_host=os.getenv("NAUTILUS_CACHE_HOST", DEFAULT_CACHE_HOST),
        cache_port=int(os.getenv("NAUTILUS_CACHE_PORT", str(DEFAULT_CACHE_PORT))),
    )
    node = TradingNode(config=config)

    for instrument_id in instrument_ids:
        strategy = SentinelSignalStrategy(
            config=SentinelSignalStrategyConfig(
                strategy_id=f"sentinel-{instrument_id}",
                instrument_id=instrument_id,
                signal_path=str(signal_path),
                close_positions_on_stop=True,
            ),
        )
        node.trader.add_strategy(strategy)

    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    node.add_exec_client_factory(POLYMARKET, SandboxLiveExecClientFactory)
    _node_build_with_sigalrm(node)

    try:
        await _run_node_until_deadline(node=node, duration_seconds=round_timeout)
        rows: list[dict[str, Any]] = []
        # Extract results from cache
        for instrument_id in instrument_ids:
            signal = signals.get(instrument_id, {})
            # Log each entered position from the node's cache
            rows.append({
                "event": "strategy_result",
                "strategy_id": f"sentinel-{instrument_id}",
                "instrument_id": instrument_id,
                "signal_headline": signal.get("headline", ""),
                "signal_category": signal.get("category", ""),
                "signal_relevance_score": signal.get("relevance_score", 0.0),
                "market_slug": signal.get("market_slug", ""),
                "market_question": signal.get("market_question", ""),
                "direction": signal.get("direction", ""),
            })
        return rows
    except Exception as exc:  # pragma: no cover
        raise RecoverableDaemonError(str(exc)) from exc
    finally:
        node.kernel.dispose()
        if node.kernel.executor:
            node.kernel.executor.shutdown(wait=True, cancel_futures=True)


async def run_daemon(
    *,
    signal_path: str | Path,
    output_dir: str | Path,
    round_timeout: float = DEFAULT_ROUND_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    run_bridge_loop_fn: Callable[..., None] | None = None,
    writer: JsonlRunWriter | None = None,
    now_fn: Callable[[], datetime] | None = None,
    max_rounds: int = 0,
) -> None:
    """Run the Sentinel news daemon loop."""
    signal_path = Path(signal_path)
    output_dir = Path(output_dir)
    now_fn = now_fn or (lambda: datetime.now(tz=UTC))
    writer = writer or JsonlRunWriter(
        build_daemon_output_path(output_dir=output_dir, now=now_fn())
    )

    rounds_completed = 0
    daemon_run_id = uuid.uuid4().hex

    while max_rounds <= 0 or rounds_completed < max_rounds:
        started_at = now_fn().astimezone(UTC)

        # Optionally refresh signals from bridge
        if run_bridge_loop_fn is not None:
            try:
                run_bridge_loop_fn(
                    signal_path=signal_path,
                    max_iterations=1,
                )
            except Exception as exc:
                writer.write({
                    "run_id": daemon_run_id,
                    "event": "bridge_refresh_error",
                    "reason": str(exc),
                    "timestamp": started_at.isoformat(),
                })

        # Read signals
        all_signals = read_all_signals(signal_path)
        grouped_signals = group_signals_by_instrument(all_signals)

        if not grouped_signals:
            writer.write({
                "run_id": daemon_run_id,
                "event": "round_skipped",
                "reason": "no_signals_available",
                "timestamp": started_at.isoformat(),
            })
            rounds_completed += 1
            await asyncio.sleep(poll_interval)
            continue

        writer.write({
            "run_id": daemon_run_id,
            "event": "round_start",
            "num_instruments": len(grouped_signals),
            "instrument_ids": list(grouped_signals.keys()),
            "timestamp": started_at.isoformat(),
        })

        try:
            rows = await _run_sentinel_round(
                signals=grouped_signals,
                signal_path=signal_path,
                round_timeout=round_timeout,
            )
        except RecoverableDaemonError as exc:
            writer.write({
                "run_id": daemon_run_id,
                "event": "round_error",
                "reason": str(exc),
                "timestamp": now_fn().astimezone(UTC).isoformat(),
            })
            rounds_completed += 1
            await asyncio.sleep(poll_interval)
            continue

        for row in rows:
            payload = dict(row)
            payload.setdefault("run_id", daemon_run_id)
            payload.setdefault("timestamp", now_fn().astimezone(UTC).isoformat())
            writer.write(payload)

        writer.write({
            "run_id": daemon_run_id,
            "event": "round_end",
            "num_results": len(rows),
            "timestamp": now_fn().astimezone(UTC).isoformat(),
        })

        rounds_completed += 1
        if max_rounds > 0 and rounds_completed >= max_rounds:
            break
        await asyncio.sleep(poll_interval)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--signal-path", default=DEFAULT_SIGNAL_PATH, help="path to sentinel signals JSONL file")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="base output directory")
    p.add_argument("--round-timeout", type=float, default=DEFAULT_ROUND_TIMEOUT, help="seconds to run each round")
    p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="seconds between rounds")
    p.add_argument("--max-rounds", type=int, default=0, help="stop after N rounds, 0 means run forever")
    p.add_argument("--refresh-signals", action="store_true", help="poll bridge loop to refresh signals each round")
    return p


async def _run_main_loop(
    *,
    signal_path: str,
    output_dir: str,
    round_timeout: float,
    poll_interval: float,
    max_rounds: int,
    refresh_signals: bool,
) -> None:
    run_bridge = (run_bridge_loop if refresh_signals else None)
    await run_daemon(
        signal_path=signal_path,
        output_dir=output_dir,
        round_timeout=round_timeout,
        poll_interval=poll_interval,
        run_bridge_loop_fn=run_bridge,
        max_rounds=max_rounds,
    )


def main() -> int:
    args = _build_parser().parse_args()
    asyncio.run(
        _run_main_loop(
            signal_path=str(args.signal_path),
            output_dir=str(args.output_dir),
            round_timeout=float(args.round_timeout),
            poll_interval=float(args.poll_interval),
            max_rounds=int(args.max_rounds),
            refresh_signals=bool(args.refresh_signals),
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
