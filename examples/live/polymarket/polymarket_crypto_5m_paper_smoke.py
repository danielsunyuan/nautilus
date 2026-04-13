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
then attaches the standard ``ExecTester`` to the selected Up/Down instrument.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
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
from nautilus_trader.core.nautilus_pyo3 import HttpClient
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.strategies.tester_exec import ExecTester
from nautilus_trader.test_kit.strategies.tester_exec import ExecTesterConfig


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="BTC", help=f"one of {', '.join(SUPPORTED_ASSETS)}")
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL, help="Gamma API base URL")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout seconds")
    parser.add_argument("--side", choices=("up", "down"), default="up", help="outcome side")
    return parser


async def _resolve_session(asset: str, gamma_host: str, timeout: float):
    http_client = HttpClient(timeout_secs=max(1, int(timeout)))
    return await resolve_crypto_5m_session(
        asset=asset,
        http_client=http_client,
        gamma_base_url=gamma_host,
        timeout=timeout,
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

    instrument_provider_config = PolymarketInstrumentProviderConfig(
        load_ids=frozenset([str(instrument_id)]),
    )

    config_node = TradingNodeConfig(
        trader_id=TraderId("PAPER-5M-001"),
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
            database=DatabaseConfig(host="redis", port=6379),
            timestamps_as_iso8601=True,
            persist_account_events=True,
            buffer_interval_ms=100,
            flush_on_start=False,
            use_instance_id=True,
        ),
        message_bus=MessageBusConfig(
            database=DatabaseConfig(host="redis", port=6379),
            timestamps_as_iso8601=True,
            buffer_interval_ms=100,
            streams_prefix="polymarket",
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
            ),
        },
        exec_clients={
            POLYMARKET: SandboxExecutionClientConfig(
                venue=POLYMARKET_VENUE,
                starting_balances=["1_000 USDC"],
            ),
        },
        timeout_connection=20.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )

    node = TradingNode(config=config_node)
    tester = ExecTester(
        config=ExecTesterConfig(
            instrument_id=instrument_id,
            external_order_claims=[instrument_id],
            subscribe_quotes=True,
            subscribe_trades=True,
            enable_limit_sells=False,
            tob_offset_ticks=10,
            order_qty=Decimal(10),
            use_post_only=False,
            reduce_only_on_stop=False,
            cancel_orders_on_stop=True,
            close_positions_on_stop=True,
            log_data=False,
            can_unsubscribe=False,
        ),
    )

    node.trader.add_strategy(tester)
    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    node.add_exec_client_factory(POLYMARKET, SandboxLiveExecClientFactory)
    node.build()

    try:
        node.run()
    finally:
        node.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
