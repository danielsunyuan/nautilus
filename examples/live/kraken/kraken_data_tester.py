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

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os

from nautilus_trader.adapters.kraken import KRAKEN
from nautilus_trader.adapters.kraken import KrakenDataClientConfig
from nautilus_trader.adapters.kraken import KrakenEnvironment
from nautilus_trader.adapters.kraken import KrakenLiveDataClientFactory
from nautilus_trader.adapters.kraken import KrakenProductType
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.strategies.tester_data import DataTester
from nautilus_trader.test_kit.strategies.tester_data import DataTesterConfig


@dataclass(frozen=True, slots=True)
class KrakenRuntime:
    product_type: KrakenProductType
    environment: KrakenEnvironment
    instrument_id: InstrumentId
    subscribe_bars: bool
    subscribe_mark_prices: bool
    subscribe_index_prices: bool
    bar_type: BarType | None


def build_runtime(*, product_type_name: str, symbol: str) -> KrakenRuntime:
    normalized = product_type_name.strip().lower()
    if normalized == "spot":
        product_type = KrakenProductType.SPOT
        environment = KrakenEnvironment.MAINNET
        subscribe_bars = True
        subscribe_mark_prices = False
        subscribe_index_prices = False
    elif normalized == "futures":
        product_type = KrakenProductType.FUTURES
        environment = KrakenEnvironment.MAINNET
        subscribe_bars = False
        subscribe_mark_prices = True
        subscribe_index_prices = True
    else:
        raise ValueError(f"Unsupported Kraken product type: {product_type_name}")

    instrument_id = InstrumentId.from_str(f"{symbol.strip().upper()}.{KRAKEN}")
    bar_type = (
        BarType.from_str(f"{instrument_id.value}-1-MINUTE-LAST-EXTERNAL")
        if subscribe_bars
        else None
    )
    return KrakenRuntime(
        product_type=product_type,
        environment=environment,
        instrument_id=instrument_id,
        subscribe_bars=subscribe_bars,
        subscribe_mark_prices=subscribe_mark_prices,
        subscribe_index_prices=subscribe_index_prices,
        bar_type=bar_type,
    )


def build_node(runtime: KrakenRuntime) -> TradingNode:
    config_node = TradingNodeConfig(
        trader_id=TraderId("TESTER-001"),
        logging=LoggingConfig(log_level="INFO", use_pyo3=True),
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        data_clients={
            KRAKEN: KrakenDataClientConfig(
                environment=runtime.environment,
                product_types=(runtime.product_type,),
                instrument_provider=InstrumentProviderConfig(load_all=True),
            ),
        },
        timeout_connection=30.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )

    node = TradingNode(config=config_node)
    node.trader.add_actor(
        DataTester(
            config=DataTesterConfig(
                instrument_ids=[runtime.instrument_id],
                bar_types=[runtime.bar_type] if runtime.bar_type is not None else [],
                subscribe_instrument=True,
                subscribe_quotes=True,
                subscribe_trades=True,
                subscribe_mark_prices=runtime.subscribe_mark_prices,
                subscribe_index_prices=runtime.subscribe_index_prices,
                subscribe_bars=runtime.subscribe_bars,
                book_interval_ms=10,
            ),
        ),
    )
    node.add_data_client_factory(KRAKEN, KrakenLiveDataClientFactory)
    node.build()
    return node


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream Kraken market data")
    parser.add_argument(
        "--product-type",
        default=os.getenv("KRAKEN_DATA_PRODUCT_TYPE", "spot"),
        help="Kraken product type: spot or futures",
    )
    parser.add_argument(
        "--symbol",
        default=os.getenv("KRAKEN_DATA_SYMBOL", "BTC/USD"),
        help="Kraken symbol, default BTC/USD",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runtime = build_runtime(product_type_name=args.product_type, symbol=args.symbol)
    node = build_node(runtime)
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
