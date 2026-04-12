#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------

from decimal import Decimal

from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket import PolymarketLiveDataClientFactory
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.strategies.tester_exec import ExecTester
from nautilus_trader.test_kit.strategies.tester_exec import ExecTesterConfig


# Live data from a known active Polymarket event with sandbox execution only.
condition_id = "0xcccb7e7613a087c132b69cbf3a02bece3fdcb824c1da54ae79acc8d4a562d902"
token_id = "8441400852834915183759801017793514978104486628517653995211751018945988243154"
instrument_id = get_polymarket_instrument_id(condition_id, token_id)

instrument_provider_config = PolymarketInstrumentProviderConfig(
    load_ids=frozenset([str(instrument_id)]),
)

config_node = TradingNodeConfig(
    trader_id=TraderId("PAPER-001"),
    logging=LoggingConfig(log_level="INFO", use_pyo3=True),
    exec_engine=LiveExecEngineConfig(
        reconciliation=False,
        open_check_interval_secs=5.0,
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


if __name__ == "__main__":
    try:
        node.run()
    finally:
        node.dispose()
