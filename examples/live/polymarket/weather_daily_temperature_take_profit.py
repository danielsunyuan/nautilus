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
Real-time take-profit / stop-loss monitor for live Polymarket weather positions.

Subscribes to the CLOB WebSocket market feed and reacts to every price event.
No polling interval — the CLOB pushes book updates sub-second.

Logic per position:
  mid >= take_profit_price  →  immediate market SELL (lock in the win)
  mid <= stop_loss_price    →  immediate market SELL (cut the loss)

Position discovery:
  - On startup: scans all JSONL run files for open strategy_result entries.
  - Every POSITION_REFRESH_SECS: rescans to pick up positions entered by the
    live daemon mid-session, dynamically subscribing to new token IDs.

After a successful sell the monitor writes a settlement_update event so the
settlement poller treats the position as resolved and skips it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import py_clob_client.http_helpers.helpers as _poly_helpers
import httpx as _httpx
_poly_helpers._http_client = _httpx.Client(http2=False)
del _poly_helpers, _httpx

log = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = "/workspace/nautilus/outputs"
DEFAULT_WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_CLOB_HOST = "https://clob.polymarket.com"

# How often to rescan JSONL for new positions entered by the live daemon.
POSITION_REFRESH_SECS = 30.0

# Sell buffer: Polymarket deducts taker fees from received tokens at fill time,
# so record share count is slightly above actual balance.
SELL_BUFFER = Decimal("0.03")

# Default thresholds used when a position has no explicit preset values.
DEFAULT_TAKE_PROFIT = 0.99
DEFAULT_STOP_LOSS: float | None = None  # no stop-loss unless explicitly set


# ---------------------------------------------------------------------------
# Position state
# ---------------------------------------------------------------------------

@dataclass
class PositionWatch:
    token_id: str
    market_slug: str
    instrument_id: str
    shares: float
    entry_price: float
    take_profit: float
    stop_loss: float | None
    city: str
    observation_date: str
    strategy_name: str
    # Live best-bid / best-ask — updated on each WS message
    best_bid: float | None = field(default=None, compare=False)
    best_ask: float | None = field(default=None, compare=False)
    # Guard: set True once a sell is submitted so we never double-exit
    exit_submitted: bool = field(default=False, compare=False)

    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None

    def should_take_profit(self) -> bool:
        m = self.mid
        return m is not None and m >= self.take_profit

    def should_stop_loss(self) -> bool:
        if self.stop_loss is None:
            return False
        m = self.mid
        return m is not None and m <= self.stop_loss


# ---------------------------------------------------------------------------
# JSONL helpers
# ---------------------------------------------------------------------------

class JsonlRunWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True))
            fh.write("\n")
            fh.flush()


def _token_id_from_instrument_id(instrument_id: str) -> str:
    return instrument_id.split(".POLYMARKET")[0].rsplit("-", 1)[1]


def load_open_positions(jsonl_dir: Path) -> dict[str, PositionWatch]:
    """
    Scan all JSONL run files and return a dict of token_id → PositionWatch
    for every open strategy_result entry that is not already resolved.

    A position is considered resolved if a settlement_update with resolved=True
    OR a take_profit_exit event exists for that token_id.
    """
    resolved_tokens: set[str] = set()
    all_rows: list[dict] = []

    for jsonl_file in sorted(jsonl_dir.glob("*.jsonl")):
        try:
            for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    all_rows.append(row)
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue

    # Collect all resolved token IDs
    for row in all_rows:
        ev = row.get("event", "")
        if ev in ("settlement_update", "take_profit_exit") and row.get("resolved") is True:
            tid = row.get("token_id")
            if tid:
                resolved_tokens.add(tid)
        if ev == "take_profit_exit":
            tid = row.get("token_id")
            if tid:
                resolved_tokens.add(tid)

    # Build watches for open positions
    watches: dict[str, PositionWatch] = {}
    for row in all_rows:
        if row.get("event") != "strategy_result":
            continue
        if row.get("accounting_status") != "open":
            continue

        instrument_id = row.get("instrument_id", "")
        if not instrument_id:
            continue

        token_id = _token_id_from_instrument_id(instrument_id)
        if not token_id or token_id in resolved_tokens:
            continue

        tp = row.get("take_profit_price")
        sl = row.get("stop_loss_price")

        watches[token_id] = PositionWatch(
            token_id=token_id,
            market_slug=row.get("market_slug", ""),
            instrument_id=instrument_id,
            shares=float(row.get("shares", 0.0)),
            entry_price=float(row.get("entry_price", 0.0)),
            take_profit=float(tp) if tp is not None else DEFAULT_TAKE_PROFIT,
            stop_loss=float(sl) if sl is not None else DEFAULT_STOP_LOSS,
            city=row.get("city", ""),
            observation_date=row.get("observation_date", ""),
            strategy_name=row.get("strategy_name", ""),
        )

    return watches


# ---------------------------------------------------------------------------
# CLOB client
# ---------------------------------------------------------------------------

def _build_clob_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    from py_clob_client.clob_types import ApiCreds

    host = os.environ.get("POLYMARKET_CLOB_HOST", DEFAULT_CLOB_HOST)
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))

    if not private_key or not funder:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS must be set")

    client = ClobClient(host, chain_id=POLYGON, key=private_key, funder=funder, signature_type=sig_type)

    api_key = os.environ.get("POLYMARKET_CLOB_API_KEY", "")
    api_secret = os.environ.get("POLYMARKET_CLOB_API_SECRET", "")
    passphrase = os.environ.get("POLYMARKET_CLOB_PASSPHRASE", "")

    if not (api_key and api_secret and passphrase):
        log.info("Deriving CLOB API credentials (one-time network call)...")
        creds = client.create_or_derive_api_creds()
        api_key = creds.api_key
        api_secret = creds.api_secret
        passphrase = creds.api_passphrase

    client.set_api_creds(ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=passphrase,
    ))
    return client


# ---------------------------------------------------------------------------
# Sell execution
# ---------------------------------------------------------------------------

def _submit_sell(position: PositionWatch, clob_client: Any, reason: str) -> dict:
    """Submit a FOK market SELL for the position. Returns the CLOB response."""
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL

    qty = Decimal(str(position.shares)) - SELL_BUFFER
    if qty <= Decimal("0"):
        raise ValueError(f"Position too small after sell buffer: shares={position.shares}")

    order_args = MarketOrderArgs(
        token_id=position.token_id,
        amount=float(qty),
        side=SELL,
    )
    signed_order = clob_client.create_market_order(order_args)
    resp = clob_client.post_order(signed_order, OrderType.FOK)

    log.info(
        "%s  slug=%s  mid=%.4f  qty=%s  resp=%s",
        reason.upper(), position.market_slug, position.mid or 0.0, qty, resp,
    )
    return resp


def _build_settlement_event(
    position: PositionWatch,
    sell_price: float,
    reason: str,
    resp: dict,
) -> dict:
    """
    Build a settlement_update event so the settlement poller skips this token.
    sell_price: the price we sold at (mid at time of trigger).
    """
    pnl = (sell_price - position.entry_price) * position.shares
    return {
        "run_id": f"tp-{uuid.uuid4()}",
        "event": "settlement_update",
        "market_slug": position.market_slug,
        "token_id": position.token_id,
        "instrument_id": position.instrument_id,
        "strategy_name": position.strategy_name,
        "city": position.city,
        "observation_date": position.observation_date,
        "entry_price": position.entry_price,
        "settlement_price": sell_price,
        "shares": position.shares,
        "stake": position.entry_price * position.shares,
        "pnl": pnl,
        "resolved": True,
        "resolved_outcome": "win" if pnl > 0 else "loss",
        "exit_method": reason,
        "clob_response": str(resp),
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# WebSocket monitor
# ---------------------------------------------------------------------------

async def _run_ws_loop(
    *,
    watches: dict[str, PositionWatch],
    watches_lock: asyncio.Lock,
    writer: JsonlRunWriter,
    clob_client: Any,
    wss_url: str,
    new_tokens_event: asyncio.Event,
) -> None:
    """
    Persistent WebSocket loop. Subscribes to all watched token IDs, processes
    every price event, and triggers exits when thresholds are crossed.

    Reconnects automatically on disconnect. When new tokens are added (detected
    via new_tokens_event), sends an incremental subscribe on the live connection.
    """
    import websockets

    reconnect_delay = 1.0

    while True:
        async with watches_lock:
            active_ids = [tid for tid, w in watches.items() if not w.exit_submitted]

        if not active_ids:
            log.info("No active positions to watch. Waiting for new positions...")
            await asyncio.sleep(10.0)
            continue

        subscribe_msg = {"type": "market", "assets_ids": active_ids}
        log.info("WS connecting — watching %d token(s)", len(active_ids))

        try:
            async with websockets.connect(
                wss_url, ping_interval=20, ping_timeout=20,
            ) as ws:
                await ws.send(json.dumps(subscribe_msg))
                new_tokens_event.clear()
                reconnect_delay = 1.0

                while True:
                    # Check for new positions to subscribe to
                    if new_tokens_event.is_set():
                        async with watches_lock:
                            all_ids = [tid for tid, w in watches.items() if not w.exit_submitted]
                        new_ids = [tid for tid in all_ids if tid not in active_ids]
                        if new_ids:
                            log.info("Subscribing to %d new token(s): %s",
                                     len(new_ids), [tid[-8:] for tid in new_ids])
                            await ws.send(json.dumps({"type": "market", "assets_ids": new_ids}))
                            active_ids.extend(new_ids)
                        new_tokens_event.clear()

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        break

                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    messages = payload if isinstance(payload, list) else [payload]
                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue
                        await _process_message(
                            msg=msg,
                            watches=watches,
                            watches_lock=watches_lock,
                            writer=writer,
                            clob_client=clob_client,
                        )

        except Exception as exc:
            log.warning("WS error: %s — reconnecting in %.1fs", exc, reconnect_delay)

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 30.0)


async def _process_message(
    *,
    msg: dict,
    watches: dict[str, PositionWatch],
    watches_lock: asyncio.Lock,
    writer: JsonlRunWriter,
    clob_client: Any,
) -> None:
    """Update position state from a WS message and trigger exit if threshold crossed."""
    token_id = str(msg.get("asset_id") or msg.get("asset") or "").strip()
    if not token_id:
        return

    async with watches_lock:
        pos = watches.get(token_id)
        if pos is None or pos.exit_submitted:
            return

        bid = msg.get("best_bid")
        ask = msg.get("best_ask")
        if bid not in (None, ""):
            try:
                pos.best_bid = float(bid)
            except (ValueError, TypeError):
                pass
        if ask not in (None, ""):
            try:
                pos.best_ask = float(ask)
            except (ValueError, TypeError):
                pass

        if pos.should_take_profit():
            reason = "take_profit"
        elif pos.should_stop_loss():
            reason = "stop_loss"
        else:
            return

        # Mark as submitted immediately under the lock to prevent concurrent triggers
        pos.exit_submitted = True
        mid_at_trigger = pos.mid

    # Submit outside the lock (network call)
    try:
        resp = _submit_sell(pos, clob_client, reason)
        sell_price = mid_at_trigger or pos.take_profit
        event = _build_settlement_event(pos, sell_price, reason, resp)
        writer.write(event)
        log.info(
            "EXIT %s  %s  mid=%.4f  pnl=%.4f",
            reason.upper(), pos.market_slug, sell_price, event["pnl"],
        )
    except Exception as exc:
        log.error("SELL FAILED for %s: %s — position remains open", pos.market_slug, exc)
        # Unmark so next price event can retry
        async with watches_lock:
            pos.exit_submitted = False


# ---------------------------------------------------------------------------
# Position refresh task
# ---------------------------------------------------------------------------

async def _position_refresh_task(
    *,
    jsonl_dir: Path,
    watches: dict[str, PositionWatch],
    watches_lock: asyncio.Lock,
    new_tokens_event: asyncio.Event,
) -> None:
    """
    Periodically rescans JSONL to pick up new positions entered by the live daemon.
    Adds newly discovered token IDs to watches and signals the WS loop to subscribe.
    """
    while True:
        await asyncio.sleep(POSITION_REFRESH_SECS)
        try:
            fresh = load_open_positions(jsonl_dir)
        except Exception as exc:
            log.warning("Position refresh failed: %s", exc)
            continue

        async with watches_lock:
            new_count = 0
            for tid, pos in fresh.items():
                if tid not in watches:
                    watches[tid] = pos
                    new_count += 1
                    log.info("New position discovered: %s (tp=%.2f)", pos.market_slug, pos.take_profit)
            if new_count:
                new_tokens_event.set()

            # Also drop positions that have been externally resolved
            for tid in list(watches.keys()):
                if tid not in fresh and not watches[tid].exit_submitted:
                    log.info("Position no longer open (externally resolved): %s", watches[tid].market_slug)
                    watches[tid].exit_submitted = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _async_main(args: argparse.Namespace) -> None:
    jsonl_dir = Path(args.output_dir) / "polymarket" / "runs"

    writer = JsonlRunWriter(jsonl_dir / "take_profit.jsonl")

    log.info("Loading open positions from %s ...", jsonl_dir)
    watches = load_open_positions(jsonl_dir)
    log.info("Found %d open position(s) to watch", len(watches))
    for pos in watches.values():
        log.info("  %s  tp=%.2f  sl=%s  shares=%.4f",
                 pos.market_slug, pos.take_profit,
                 f"{pos.stop_loss:.2f}" if pos.stop_loss else "none",
                 pos.shares)

    clob_client = _build_clob_client()

    watches_lock = asyncio.Lock()
    new_tokens_event = asyncio.Event()

    await asyncio.gather(
        _run_ws_loop(
            watches=watches,
            watches_lock=watches_lock,
            writer=writer,
            clob_client=clob_client,
            wss_url=args.wss_url,
            new_tokens_event=new_tokens_event,
        ),
        _position_refresh_task(
            jsonl_dir=jsonl_dir,
            watches=watches,
            watches_lock=watches_lock,
            new_tokens_event=new_tokens_event,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real-time take-profit / stop-loss monitor for live weather positions"
    )
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT_DIR,
        help="Root output directory (polymarket/runs/ will be scanned)"
    )
    parser.add_argument(
        "--wss-url", default=DEFAULT_WSS_URL,
        help="CLOB WebSocket URL"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
