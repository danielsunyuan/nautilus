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
Polymarket CLOB L2 order book depth collector for Nautilus TradingNode.

Polls CLOB /book endpoint for all configured token IDs, records full order book
snapshots to JSONL for later analysis (slippage modeling, liquidity patterns).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

import httpx
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.core.data import Data
from nautilus_trader.data.messages import SubscribeData
from nautilus_trader.data.messages import UnsubscribeData
from nautilus_trader.live.data_client import LiveDataClient
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue


class L2DepthSnapshot(Data):
    """
    Custom data event for CLOB L2 order book snapshots.

    Records full depth at each price level for both bid and ask sides,
    along with derived metrics (mid_price, spread, depth in USD).
    """

    def __init__(
        self,
        token_id: str,
        market_slug: str,
        sport: str,
        bids: tuple[tuple[float, float], ...],
        asks: tuple[tuple[float, float], ...],
        mid_price: float,
        spread: float,
        bid_depth_usd: float,
        ask_depth_usd: float,
        timestamp_iso: str,
        ts_event: int,
        ts_init: int,
    ) -> None:
        self.token_id = token_id
        self.market_slug = market_slug
        self.sport = sport
        self.bids = bids  # tuple of (price, size) pairs, ascending
        self.asks = asks  # tuple of (price, size) pairs, ascending
        self.mid_price = mid_price
        self.spread = spread
        self.bid_depth_usd = bid_depth_usd
        self.ask_depth_usd = ask_depth_usd
        self.timestamp_iso = timestamp_iso
        self._ts_event = ts_event
        self._ts_init = ts_init

    @property
    def ts_event(self) -> int:
        """UNIX timestamp (ns) of the order book snapshot."""
        return self._ts_event

    @property
    def ts_init(self) -> int:
        """UNIX timestamp (ns) when this snapshot was created."""
        return self._ts_init


class L2DepthCollectorConfig(NautilusConfig, frozen=True):
    """Configuration for L2 depth collector."""

    poll_interval_secs: int = 60
    clob_base_url: str = "https://clob.polymarket.com"
    timeout_seconds: float = 10.0
    output_dir: str = "/workspace/outputs/polymarket/l2_depth"
    max_levels: int = 20  # Price levels per side to record
    token_ids: tuple[str, ...] = ()  # Empty = no static subscriptions


class L2DepthCollector(LiveDataClient):
    """
    Live data client for Polymarket CLOB L2 order book depth.

    Polls CLOB /book endpoint at configured intervals, snapshots full order
    book depth for each token_id, and writes to JSONL for analysis.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        venue: Venue | None,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        config: L2DepthCollectorConfig | None = None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=venue,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config or L2DepthCollectorConfig(),
        )
        self._config = config or L2DepthCollectorConfig()
        self._poll_task: asyncio.Task | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._output_file: Path | None = None
        self._tracked_tokens: dict[str, dict] = {}  # token_id -> {slug, sport}

    async def _connect(self) -> None:
        """Initialize HTTP client, output directory, and start polling loop."""
        self._http_client = httpx.AsyncClient(timeout=self._config.timeout_seconds)

        output_path = Path(self._config.output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Generate JSONL filename with date
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        self._output_file = output_path / f"l2_depth_{date_str}.jsonl"

        # Register static token_ids if configured
        for token_id in self._config.token_ids:
            self._tracked_tokens[token_id] = {"slug": "", "sport": ""}

        self._log.info(f"L2 depth output: {self._output_file}")

        self._poll_task = self.create_task(
            self._poll_loop(),
            log_msg="L2 depth polling loop",
        )

    async def _disconnect(self) -> None:
        """Stop polling loop and close HTTP client."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _subscribe(self, command: SubscribeData) -> None:
        """Register a token_id for continuous depth collection."""
        # Extract token_id from instrument_id or custom field
        # For now, no-op — we poll statically configured tokens

    async def _unsubscribe(self, command: UnsubscribeData) -> None:
        """Unregister a token_id."""

    async def _poll_loop(self) -> None:
        """
        Poll CLOB /book for each tracked token_id at poll_interval_secs.

        Snapshot structure from CLOB:
        {
            "bids": [
                {"price": "0.50", "size": "150"},
                {"price": "0.51", "size": "200"},
            ],
            "asks": [
                {"price": "0.55", "size": "100"},
                {"price": "0.56", "size": "50"},
            ]
        }

        Note: bids are ascending (bids[0] worst, bids[-1] best).
              asks are ascending (asks[0] best, asks[-1] worst).
        """
        try:
            while True:
                for token_id in list(self._tracked_tokens.keys()):
                    try:
                        snapshot = await self._fetch_book(token_id)
                        if snapshot:
                            self._write_jsonl(snapshot)
                            self._handle_data(snapshot)
                    except Exception as e:
                        self._log.error(f"Failed to fetch book for {token_id}: {e!r}")
                        continue

                # Sleep between full sweeps
                await asyncio.sleep(self._config.poll_interval_secs)

        except asyncio.CancelledError:
            self._log.debug("Polling loop cancelled")
            raise

    async def _fetch_book(self, token_id: str) -> L2DepthSnapshot | None:
        """Fetch order book from CLOB /book endpoint."""
        if self._http_client is None:
            return None

        url = f"{self._config.clob_base_url}/book?token_id={token_id}"

        try:
            response = await self._http_client.get(url)
            response.raise_for_status()
            data = response.json()

            return self._parse_book_snapshot(token_id, data)

        except httpx.HTTPError as e:
            self._log.error(f"HTTP error fetching {token_id}: {e}")
            return None
        except Exception as e:
            self._log.error(f"Error parsing book for {token_id}: {e}")
            return None

    def _parse_book_snapshot(self, token_id: str, data: dict) -> L2DepthSnapshot | None:
        """
        Parse CLOB /book response into L2DepthSnapshot.

        Computes mid_price, spread, and depth metrics.
        """
        now_ns = self._clock.timestamp_ns()

        bids_raw = data.get("bids", [])
        asks_raw = data.get("asks", [])

        if not bids_raw or not asks_raw:
            return None

        # Parse and limit to max_levels
        bids = tuple(
            (float(level["price"]), float(level["size"]))
            for level in bids_raw[: self._config.max_levels]
        )
        asks = tuple(
            (float(level["price"]), float(level["size"]))
            for level in asks_raw[: self._config.max_levels]
        )

        # Best bid is last in bids (ascending), best ask is first in asks (ascending)
        best_bid = bids[-1][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 1.0

        mid_price = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid

        # Compute depth in USD (sum of price * size for each side)
        bid_depth_usd = sum(price * size for price, size in bids)
        ask_depth_usd = sum(price * size for price, size in asks)

        # Resolve market metadata
        market_info = self._tracked_tokens.get(token_id, {})
        market_slug = market_info.get("slug", "")
        sport = market_info.get("sport", "")

        timestamp_iso = datetime.utcfromtimestamp(now_ns / 1e9).isoformat() + "Z"

        return L2DepthSnapshot(
            token_id=token_id,
            market_slug=market_slug,
            sport=sport,
            bids=bids,
            asks=asks,
            mid_price=mid_price,
            spread=spread,
            bid_depth_usd=bid_depth_usd,
            ask_depth_usd=ask_depth_usd,
            timestamp_iso=timestamp_iso,
            ts_event=now_ns,
            ts_init=now_ns,
        )

    def _write_jsonl(self, snapshot: L2DepthSnapshot) -> None:
        """Append snapshot as JSON line to output file."""
        if self._output_file is None:
            return

        record = {
            "token_id": snapshot.token_id,
            "market_slug": snapshot.market_slug,
            "sport": snapshot.sport,
            "mid_price": snapshot.mid_price,
            "spread": snapshot.spread,
            "bid_depth_usd": snapshot.bid_depth_usd,
            "ask_depth_usd": snapshot.ask_depth_usd,
            "bids": [[p, s] for p, s in snapshot.bids],
            "asks": [[p, s] for p, s in snapshot.asks],
            "timestamp": snapshot.timestamp_iso,
            "ts_event": snapshot.ts_event,
            "ts_init": snapshot.ts_init,
        }

        try:
            with open(self._output_file, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            self._log.error(f"Failed to write JSONL: {e}")


class L2DepthCollectorFactory(LiveDataClientFactory):
    """Factory for creating L2 depth collector clients."""

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: L2DepthCollectorConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> L2DepthCollector:
        """Create a new L2 depth collector."""
        return L2DepthCollector(
            loop=loop,
            client_id=ClientId("L2_DEPTH"),
            venue=None,  # Multi-venue
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
