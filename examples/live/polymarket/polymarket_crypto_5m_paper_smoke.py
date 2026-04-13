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
Live Polymarket **5-minute crypto Up/Down** round (e.g. ``btc-updown-5m-{epoch}``) with
sandbox execution only — same stack as ``polymarket_paper_tester.py``, but resolves the
active market from Gamma using the same slug convention as the EL ``quant`` runners.

Usage (from repo root, or inside the ``papertrade`` container with ``/workspace`` mounted):

.. code-block:: bash

    python examples/live/polymarket/polymarket_crypto_5m_paper_smoke.py --asset BTC

Docker (see ``.docker/README.md``):

.. code-block:: bash

    docker compose -f .docker/docker-compose.yml run --rm papertrade \\
      python /workspace/examples/live/polymarket/polymarket_crypto_5m_paper_smoke.py --asset BTC

"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket import PolymarketDataClientConfig
from nautilus_trader.adapters.polymarket import PolymarketLiveDataClientFactory
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
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
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.strategies.tester_exec import ExecTester
from nautilus_trader.test_kit.strategies.tester_exec import ExecTesterConfig

DEFAULT_GAMMA_HOST = "https://gamma-api.polymarket.com"
SUPPORTED_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")


def current_crypto_5m_market_slug(*, asset: str, now: datetime) -> str:
    symbol = asset.strip().upper()
    if symbol not in SUPPORTED_ASSETS:
        raise ValueError(f"unsupported asset {asset!r}; choose one of {SUPPORTED_ASSETS}")
    epoch = int(now.astimezone(timezone.utc).timestamp())
    round_start = epoch - (epoch % 300)
    return f"{symbol.lower()}-updown-5m-{round_start}"


def _gamma_market_slug_url(*, gamma_host: str, slug: str) -> str:
    return f"{gamma_host.rstrip('/')}/markets/slug/{quote(slug)}"


def _json_get(url: str, *, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; NautilusTrader/5m-paper-smoke)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _require_list(value: Any, *, name: str) -> list[Any]:
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    if isinstance(value, list):
        return value
    raise ValueError(f"unexpected {name} payload")


def _parse_gamma_market_for_side(
    payload: dict[str, Any],
    *,
    side: str,
) -> tuple[str, str]:
    """
    Return ``(condition_id, clob_token_id)`` for the requested side (``up`` / ``down``).
    """
    condition_id = str(payload.get("conditionId") or "").strip()
    if not condition_id:
        raise ValueError("Gamma market payload missing conditionId")

    outcomes = _require_list(payload.get("outcomes", []), name="outcomes")
    token_ids = _require_list(payload.get("clobTokenIds", []), name="clobTokenIds")
    if len(outcomes) != len(token_ids):
        raise ValueError("Gamma outcomes and clobTokenIds length mismatch")

    want = side.strip().lower()
    if want not in ("up", "down"):
        raise ValueError("side must be 'up' or 'down'")

    for idx, outcome in enumerate(outcomes):
        label = str(outcome).strip().lower()
        if label == want:
            return condition_id, str(token_ids[idx]).strip()

    raise ValueError(f"Gamma market has no outcome matching {want!r}")


def _validate_open(payload: dict[str, Any], *, slug: str) -> None:
    if payload.get("closed") is True:
        raise ValueError(f"market {slug!r} is closed")
    accepting = payload.get("acceptingOrders", payload.get("accepting_orders"))
    if accepting is False:
        raise ValueError(f"market {slug!r} is not accepting orders")


def resolve_instrument_for_5m_round(
    *,
    asset: str,
    gamma_host: str,
    timeout: float,
    side: str,
    now: datetime | None = None,
) -> tuple[str, InstrumentId]:
    """
    Resolve slug(s) for the current 5m window (and the previous window if the current
    slug is not yet available), fetch Gamma, return ``(slug_used, instrument_id)``.
    """
    now = now or datetime.now(timezone.utc)
    current_slug = current_crypto_5m_market_slug(asset=asset, now=now)
    epoch = int(now.astimezone(timezone.utc).timestamp())
    round_start = epoch - (epoch % 300)
    candidates = [
        current_slug,
        f"{asset.strip().lower()}-updown-5m-{round_start - 300}",
    ]

    last_err: Exception | None = None
    for slug in candidates:
        url = _gamma_market_slug_url(gamma_host=gamma_host, slug=slug)
        try:
            payload = _json_get(url, timeout=timeout)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                continue
            raise
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as e:
            last_err = e
            continue

        if not isinstance(payload, dict):
            continue

        try:
            _validate_open(payload, slug=slug)
            condition_id, token_id = _parse_gamma_market_for_side(payload, side=side)
        except ValueError:
            last_err = ValueError(f"market {slug!r} failed validation or parsing")
            continue

        inst = get_polymarket_instrument_id(condition_id, token_id)
        return slug, inst

    msg = f"could not resolve a live 5m market for {asset!r}; tried {candidates!r}"
    if last_err is not None:
        raise RuntimeError(msg) from last_err
    raise RuntimeError(msg)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--asset", default="BTC", help=f"one of {', '.join(SUPPORTED_ASSETS)}")
    p.add_argument("--gamma-host", default=DEFAULT_GAMMA_HOST, help="Gamma API base URL")
    p.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout seconds")
    p.add_argument(
        "--side",
        choices=("up", "down"),
        default="up",
        help="which outcome token to attach ExecTester to",
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()
    try:
        slug_used, instrument_id = resolve_instrument_for_5m_round(
            asset=str(args.asset),
            gamma_host=str(args.gamma_host),
            timeout=float(args.timeout),
            side=str(args.side),
        )
    except (RuntimeError, ValueError, urllib.error.URLError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Using Gamma slug={slug_used!r} instrument_id={instrument_id}")

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
