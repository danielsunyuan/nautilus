#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
Demonstrates streaming public market data from Binance without API keys.

This example shows how to use the Binance data client for public market data without
requiring authentication. No API key or secret is needed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os

from nautilus_trader.adapters.binance import BINANCE
from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance import BinanceDataClientConfig
from nautilus_trader.adapters.binance import BinanceLiveDataClientFactory
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.strategies.tester_data import DataTester
from nautilus_trader.test_kit.strategies.tester_data import DataTesterConfig


@dataclass(frozen=True, slots=True)
class BinanceRuntime:
    account_type: BinanceAccountType
    instrument_id: InstrumentId
    subscribe_bars: bool
    subscribe_book_at_interval: bool
    bar_type: BarType | None


def build_runtime(*, account_type_name: str, symbol: str) -> BinanceRuntime:
    normalized = account_type_name.strip().lower()
    if normalized == "spot":
        account_type = BinanceAccountType.SPOT
        subscribe_bars = True
        subscribe_book_at_interval = False
    elif normalized in {"usdt_futures", "futures"}:
        account_type = BinanceAccountType.USDT_FUTURES
        subscribe_bars = False
        subscribe_book_at_interval = True
    else:
        raise ValueError(f"Unsupported Binance account type: {account_type_name}")

    instrument_id = InstrumentId.from_str(f"{symbol.strip().upper()}.{BINANCE}")
    bar_type = (
        BarType.from_str(f"{instrument_id.value}-1-MINUTE-LAST-EXTERNAL")
        if subscribe_bars
        else None
    )
    return BinanceRuntime(
        account_type=account_type,
        instrument_id=instrument_id,
        subscribe_bars=subscribe_bars,
        subscribe_book_at_interval=subscribe_book_at_interval,
        bar_type=bar_type,
    )


def build_node(runtime: BinanceRuntime) -> TradingNode:
    config_node = TradingNodeConfig(
        trader_id=TraderId("TESTER-001"),
        logging=LoggingConfig(log_level="INFO", use_pyo3=True),
        data_clients={
            BINANCE: BinanceDataClientConfig(
                environment=BinanceEnvironment.LIVE,
                account_type=runtime.account_type,
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset([runtime.instrument_id]),
                ),
            ),
        },
        timeout_connection=20.0,
        timeout_disconnection=10.0,
        timeout_post_stop=1.0,
    )

    node = TradingNode(config=config_node)
    node.trader.add_actor(
        DataTester(
            config=DataTesterConfig(
                instrument_ids=[runtime.instrument_id],
                bar_types=[runtime.bar_type] if runtime.bar_type is not None else [],
                subscribe_instrument=True,
                subscribe_book_at_interval=runtime.subscribe_book_at_interval,
                subscribe_quotes=True,
                subscribe_trades=True,
                subscribe_bars=runtime.subscribe_bars,
                book_interval_ms=100,
            ),
        ),
    )
    node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
    node.build()
    return node


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream public Binance market data")
    parser.add_argument(
        "--account-type",
        default=os.getenv("BINANCE_DATA_ACCOUNT_TYPE", "spot"),
        help="Binance account type: spot or usdt_futures",
    )
    parser.add_argument(
        "--symbol",
        default=os.getenv("BINANCE_DATA_SYMBOL", "BTCUSDT"),
        help="Binance symbol, default BTCUSDT",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runtime = build_runtime(account_type_name=args.account_type, symbol=args.symbol)
    node = build_node(runtime)
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
