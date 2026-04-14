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
Live Polymarket 5-minute crypto Up/Down round with sandbox execution only.

Resolves the current 5-minute market from Gamma with previous-window fallback,
then runs a bounded Nautilus paper-trading round on the selected instrument.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC
from datetime import datetime
import importlib.util
import json
import math
from pathlib import Path
import sys
from decimal import Decimal

try:
    from examples.live.polymarket._crypto_5m_support import DEFAULT_GAMMA_BASE_URL
    from examples.live.polymarket._crypto_5m_support import SUPPORTED_ASSETS
    from examples.live.polymarket._crypto_5m_support import resolve_crypto_5m_session
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
from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import PolymarketLiveDataClientFactory
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.core.nautilus_pyo3 import HttpClient
from nautilus_trader.live.node import TradingNode

try:
    from examples.live.polymarket.polymarket_crypto_5m_paper_daemon import _build_paper_strategy
    from examples.live.polymarket.polymarket_crypto_5m_paper_daemon import _run_node_for_duration
    from examples.live.polymarket.polymarket_crypto_5m_paper_daemon import _strategy_presets_for_set
    from examples.live.polymarket.polymarket_crypto_5m_paper_daemon import build_daemon_node_config
    from examples.live.polymarket.polymarket_crypto_5m_paper_daemon import extract_strategy_results
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.polymarket_crypto_5m_paper_daemon"
    module_path = Path(__file__).resolve().with_name("polymarket_crypto_5m_paper_daemon.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _build_paper_strategy = module._build_paper_strategy
    _run_node_for_duration = module._run_node_for_duration
    _strategy_presets_for_set = module._strategy_presets_for_set
    build_daemon_node_config = module.build_daemon_node_config
    extract_strategy_results = module.extract_strategy_results

DEFAULT_EXECUTION_CUTOFF_SECONDS = 15.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="BTC", help=f"one of {', '.join(SUPPORTED_ASSETS)}")
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL, help="Gamma API base URL")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout seconds")
    parser.add_argument("--side", choices=("up", "down"), default="up", help="outcome side")
    parser.add_argument("--preset-set", default="quant", help="strategy preset set to run")
    parser.add_argument("--order-qty", type=Decimal, default=Decimal(10), help="paper order quantity per strategy")
    parser.add_argument(
        "--execution-cutoff-seconds",
        type=float,
        default=DEFAULT_EXECUTION_CUTOFF_SECONDS,
        help="stop this many seconds before market end",
    )
    return parser


async def _resolve_session(asset: str, gamma_host: str, timeout: float):
    http_client = HttpClient(timeout_secs=max(1, math.ceil(timeout)))
    return await resolve_crypto_5m_session(
        asset=asset,
        http_client=http_client,
        gamma_base_url=gamma_host,
        timeout=timeout,
    )


async def run_single_round(
    *,
    session,
    asset: str,
    preset_set: str,
    side: str,
    order_qty: Decimal | int | float | str,
    execution_cutoff_seconds: float,
    now_fn=None,
) -> list[dict[str, object]]:
    normalized_side = str(side).strip().lower()
    if normalized_side not in {"up", "down"}:
        raise ValueError(f"side must be 'up' or 'down', got {side!r}")

    presets = _strategy_presets_for_set(preset_set)
    instrument_id = session.instrument_ids[normalized_side]
    config = build_daemon_node_config(
        instrument_ids=[str(instrument_id)],
        trader_id="PAPER-5M-001",
        cache_host="redis",
        cache_port=6379,
    )
    node = TradingNode(config=config)
    for preset in presets:
        node.trader.add_strategy(
            _build_paper_strategy(
                preset=preset,
                instrument_id=instrument_id,
                market_end_time=session.end_time,
                order_qty=Decimal(str(order_qty)),
                token_side=normalized_side,
            ),
        )
    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    node.add_exec_client_factory(POLYMARKET, SandboxLiveExecClientFactory)
    node.build()

    current_time = now_fn or (lambda: datetime.now(UTC))
    runtime_seconds = max(
        1.0,
        (session.end_time - current_time()).total_seconds() - max(0.0, float(execution_cutoff_seconds)),
    )
    await asyncio.to_thread(_run_node_for_duration, node=node, duration_seconds=runtime_seconds)
    return extract_strategy_results(
        cache=node.cache,
        presets=presets,
        instrument_id=str(instrument_id),
        asset=asset,
        slug=session.slug,
    )


def main() -> int:
    args = _build_parser().parse_args()

    try:
        session = asyncio.run(
            _resolve_session(
                asset=str(args.asset),
                gamma_host=str(args.gamma_host),
                timeout=float(args.timeout),
            ),
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    side = str(args.side)
    instrument_id = session.instrument_ids[side]
    print(f"Using Gamma slug={session.slug!r} instrument_id={instrument_id}")
    try:
        rows = asyncio.run(
            run_single_round(
                session=session,
                asset=str(args.asset),
                preset_set=str(args.preset_set),
                side=side,
                order_qty=args.order_qty,
                execution_cutoff_seconds=float(args.execution_cutoff_seconds),
            ),
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
