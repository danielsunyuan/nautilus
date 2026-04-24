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
Settlement polling resolver for Polymarket weather daily temperature markets.

Pure orchestration — reads JSONL files and queries CLOB API midpoint data for
market resolution status. No Nautilus TradingNode, no Strategy classes.

When a market resolves, the winning token's mid price snaps to ~1.000 and the
losing token's mid price snaps to ~0.000. This is authoritative and directly
accessible from the CLOB API.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from weather_daily_temperature_report import (
        build_weather_temperature_summary,
        render_weather_temperature_markdown,
    )
except ModuleNotFoundError:
    try:
        from examples.live.polymarket.weather_daily_temperature_report import (
            build_weather_temperature_summary,
            render_weather_temperature_markdown,
        )
    except ModuleNotFoundError:
        _mod_path = Path(__file__).resolve().with_name("weather_daily_temperature_report.py")
        if _mod_path.exists():
            _spec = importlib.util.spec_from_file_location(
                "weather_daily_temperature_report", _mod_path,
            )
            assert _spec is not None and _spec.loader is not None
            _mod = importlib.util.module_from_spec(_spec)
            sys.modules["weather_daily_temperature_report"] = _mod
            _spec.loader.exec_module(_mod)
            build_weather_temperature_summary = _mod.build_weather_temperature_summary
            render_weather_temperature_markdown = _mod.render_weather_temperature_markdown
        else:
            build_weather_temperature_summary = None  # type: ignore[assignment]
            render_weather_temperature_markdown = None  # type: ignore[assignment]


log = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = "/workspace/outputs"
RESOLVED_THRESHOLD = 0.99

# ---------------------------------------------------------------------------
# Redemption constants (CTF / Proxy Factory — Polygon mainnet)
# ---------------------------------------------------------------------------

_PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
_CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_POLYGON_CHAIN_ID = 137


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UnresolvedEntry:
    """An unresolved strategy_result entry extracted from JSONL."""
    market_slug: str
    condition_id: str
    token_id: str
    strategy_name: str
    arena: str
    token_side: str  # "yes" or "no"
    entry_price: float
    shares: float
    stake: float
    city: str
    observation_date: str
    source_file: str  # which JSONL file it came from
    real_order: bool = False  # True only for confirmed-entry daemon (real CLOB fills)
    entry_time: str = ""  # ISO timestamp from entry row; used by make_entry_id for key parity


@dataclass(frozen=True, slots=True)
class MarketResolution:
    """Resolution data for a token from CLOB midpoint."""
    token_id: str
    resolved: bool
    settlement_price: float | None  # 1.0 = token wins, 0.0 = token loses, None = still live


# ---------------------------------------------------------------------------
# JSONL writer (same pattern as daemon)
# ---------------------------------------------------------------------------

class JsonlRunWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            # Single write call: crash between two separate write() calls would
            # produce a partial line with no newline — silently dropped by readers.
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _live_jsonl_files(jsonl_dir: Path) -> list[Path]:
    """Return all JSONL files that belong to the live weather ledger.

    Includes ``weather_temp_live_*.jsonl`` (entry records written by the daemon),
    ``weather_confirmed_live_*.jsonl`` (confirmed-entry records written by the
    confirmed-entry daemon), ``settlement_live.jsonl`` (settlement_update records
    written by this script), and ``take_profit.jsonl`` (settlement_update records
    written by the take-profit watcher when a position is exited early via
    take-profit or stop-loss). Paper trade files (``overnight_*``,
    ``weather_temp_all_*``, the old ``settlement.jsonl``, etc.) are excluded.
    """
    files = sorted(jsonl_dir.glob("weather_temp_live_*.jsonl"))
    files += sorted(jsonl_dir.glob("weather_confirmed_live_*.jsonl"))
    for extra_name in ("settlement_live.jsonl", "take_profit.jsonl"):
        extra_file = jsonl_dir / extra_name
        if extra_file.exists():
            files.append(extra_file)
    return files


def _read_all_jsonl_rows(jsonl_dir: Path) -> list[tuple[str, dict]]:
    """Read rows from live-trade JSONL files only. Returns (filename, row) tuples.

    Reads ``weather_temp_live_*.jsonl`` (entry records) and ``settlement.jsonl``
    (settlement_update records) so that settled P&L is visible to the resolver.
    Paper trade files are never mixed into the live settlement ledger.
    """
    results: list[tuple[str, dict]] = []
    if not jsonl_dir.exists():
        return results
    for jsonl_file in _live_jsonl_files(jsonl_dir):
        try:
            with jsonl_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        results.append((jsonl_file.name, row))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return results


def _collect_redeemed_condition_ids(all_rows: list[tuple[str, dict]]) -> set[str]:
    """Collect condition_ids that already have a redemption_completed event."""
    redeemed: set[str] = set()
    for _fname, row in all_rows:
        if row.get("event") == "redemption_completed":
            cid = row.get("condition_id")
            if cid:
                redeemed.add(cid)
    return redeemed


def _collect_settled_token_ids(all_rows: list[tuple[str, dict]]) -> set[str]:
    """Collect token_ids that already have a settlement_update event.

    Also checks condition_id for backward compatibility with old settlement events.
    """
    settled: set[str] = set()
    for _fname, row in all_rows:
        if row.get("event") == "settlement_update" and row.get("resolved") is True:
            # Prefer token_id (new format)
            token_id = row.get("token_id")
            if token_id:
                settled.add(token_id)
            # Fallback to condition_id (old format)
            else:
                cid = row.get("condition_id")
                if cid:
                    settled.add(cid)
    return settled


def scan_unresolved_entries(jsonl_dir: Path) -> list[UnresolvedEntry]:
    """Scan all JSONL files in directory for unresolved strategy_result rows."""
    all_rows = _read_all_jsonl_rows(jsonl_dir)
    settled = _collect_settled_token_ids(all_rows)

    entries: list[UnresolvedEntry] = []
    for fname, row in all_rows:
        if row.get("event") != "strategy_result":
            continue
        if row.get("resolved") is True:
            continue
        if row.get("accounting_status") != "open":
            continue

        # Extract condition_id from condition_id field or instrument_id
        condition_id = row.get("condition_id", "")
        if not condition_id:
            instrument_id = row.get("instrument_id", "")
            if instrument_id:
                condition_id = instrument_id.split(".POLYMARKET")[0].rsplit("-", 1)[0]
        if not condition_id:
            continue

        # Extract token_id from instrument_id
        token_id = ""
        instrument_id = row.get("instrument_id", "")
        if instrument_id:
            token_id = instrument_id.split(".POLYMARKET")[0].rsplit("-", 1)[1]

        if not token_id:
            continue

        # Skip if already settled
        if token_id in settled:
            continue

        # real_order=True only for confirmed-entry daemon entries.
        # weather_temp_live_* entries are NautilusTrader sandbox paper trades.
        is_real = bool(row.get("real_order", False)) or fname.startswith("weather_confirmed_live_")

        entries.append(
            UnresolvedEntry(
                market_slug=row.get("market_slug", ""),
                condition_id=condition_id,
                token_id=token_id,
                strategy_name=row.get("strategy_name", ""),
                arena=row.get("arena", ""),
                token_side=row.get("token_side", "yes"),
                entry_price=float(row.get("entry_price", 0.0)),
                shares=float(row.get("shares", 0.0)),
                stake=float(row.get("stake", 0.0)),
                city=row.get("city", ""),
                observation_date=row.get("observation_date", ""),
                source_file=fname,
                real_order=is_real,
                entry_time=row.get("entry_time", row.get("timestamp", "")),
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Settlement computation
# ---------------------------------------------------------------------------

def compute_settlement(
    entry: UnresolvedEntry,
    resolution: MarketResolution,
) -> dict | None:
    """Compute settlement_update event dict from entry + resolution.

    Returns None if market not yet resolved.
    """
    if not resolution.resolved:
        return None
    if resolution.settlement_price is None:
        return None

    # settlement_price is already the correct payout for our token
    settlement_price = resolution.settlement_price
    pnl = (settlement_price - entry.entry_price) * entry.shares

    if pnl > 0:
        resolved_outcome = "win"
    else:
        resolved_outcome = "loss"

    return {
        "run_id": f"settlement-{uuid.uuid4()}",
        "event": "settlement_update",
        "market_slug": entry.market_slug,
        "condition_id": entry.condition_id,
        "token_id": entry.token_id,
        "strategy_name": entry.strategy_name,
        "arena": entry.arena,
        "city": entry.city,
        "observation_date": entry.observation_date,
        "token_side": entry.token_side,
        "entry_price": entry.entry_price,
        "entry_time": entry.entry_time,
        "settlement_price": settlement_price,
        "shares": entry.shares,
        "stake": entry.stake,
        "pnl": pnl,
        "resolved": True,
        "resolved_outcome": resolved_outcome,
        "real_order": entry.real_order,
    }


# ---------------------------------------------------------------------------
# CLOB API fetch
# ---------------------------------------------------------------------------

def _resolution_from_price(token_id: str, price: float) -> MarketResolution:
    """Convert a scalar price to a MarketResolution."""
    if price >= RESOLVED_THRESHOLD:
        return MarketResolution(token_id=token_id, resolved=True, settlement_price=1.0)
    elif price <= (1.0 - RESOLVED_THRESHOLD):
        return MarketResolution(token_id=token_id, resolved=True, settlement_price=0.0)
    else:
        return MarketResolution(token_id=token_id, resolved=False, settlement_price=None)


async def fetch_token_resolution(
    *,
    token_id: str,
    http_client: Any,
    clob_base_url: str,
    timeout: float = 15.0,
) -> MarketResolution | None:
    """Query CLOB for a token's resolution status.

    Two-phase lookup:
    1. GET /midpoint  — works for active (open) markets. Winning token mid
       snaps to ~1.000 and losing token mid snaps to ~0.000 near resolution.
    2. GET /last-trade-price  — fallback for closed/settled markets whose
       orderbook has been removed (midpoint returns 404). The last trade
       price reflects the final settlement direction.
    """
    # Phase 1: midpoint (active markets)
    try:
        response = await http_client.get(
            f"{clob_base_url}/midpoint",
            params={"token_id": token_id},
            timeout=timeout,
        )
        if response.status_code == 200:
            mid_str = response.json().get("mid")
            if mid_str is not None:
                return _resolution_from_price(token_id, float(mid_str))
        elif response.status_code != 404:
            log.warning(
                "Unexpected midpoint status %d for token_id=...%s",
                response.status_code, token_id[-8:],
            )
            return None
        # 404 → market closed; fall through to last-trade-price
    except Exception:
        log.warning("Failed to fetch midpoint for token_id=...%s", token_id[-8:])
        return None

    # Phase 2: last-trade-price (closed/resolved markets)
    try:
        response = await http_client.get(
            f"{clob_base_url}/last-trade-price",
            params={"token_id": token_id},
            timeout=timeout,
        )
        if response.status_code == 200:
            price_str = response.json().get("price")
            if price_str is not None:
                return _resolution_from_price(token_id, float(price_str))
        log.warning(
            "last-trade-price status %d for token_id=...%s",
            response.status_code, token_id[-8:],
        )
        return None
    except Exception:
        log.warning("Failed to fetch last-trade-price for token_id=...%s", token_id[-8:])
        return None


# ---------------------------------------------------------------------------
# On-chain redemption (CTF redeemPositions via Polymarket proxy factory)
# ---------------------------------------------------------------------------

async def redeem_winning_position(
    *,
    condition_id: str,
    token_side: str,
    rpc_url: str,
    private_key: str,
    http_client: Any,
) -> tuple[bool, str]:
    """Submit a CTF redeemPositions call via the Polymarket proxy factory.

    The caller's EOA address is used to derive the proxy wallet via CREATE2.
    The PROXY_FACTORY routes the redeemPositions call through that proxy wallet,
    which holds the CTF tokens.

    Calldata format (confirmed working via eth_call simulation):
        proxy_factory.proxy([(typeCode=1, to=CTF, value=0, data=redeemPositions(...))])

    Returns:
        (True,  tx_hash)       on successful submission
        (False, error_message) on failure (e.g. insufficient MATIC for gas)
    """
    try:
        from eth_abi import encode as _abi_encode
        from eth_account import Account
        from eth_utils import keccak
    except ImportError as exc:
        return False, f"missing_dependency:{exc}"

    # YES token → outcomeIndex=0, indexSet=1; NO token → outcomeIndex=1, indexSet=2
    outcome_index = 0 if token_side.lower() == "yes" else 1
    index_set = 1 << outcome_index

    # condition_id (hex string) → bytes32
    cid_bytes = bytes.fromhex(condition_id.lstrip("0x").zfill(64))

    # Build redeemPositions(address,bytes32,bytes32,uint256[]) calldata
    redeem_sel = keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    redeem_calldata: bytes = redeem_sel + _abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [_USDC_CONTRACT, b"\x00" * 32, cid_bytes, [index_set]],
    )

    # Build proxy((uint8,address,uint256,bytes)[]) calldata (selector 0x34ee9791)
    proxy_calldata: bytes = bytes.fromhex("34ee9791") + _abi_encode(
        ["(uint8,address,uint256,bytes)[]"],
        [[(1, _CTF_CONTRACT, 0, redeem_calldata)]],
    )

    account = Account.from_key(private_key)
    eoa = account.address

    try:
        nonce_resp = await http_client.post(
            rpc_url,
            json={"jsonrpc": "2.0", "method": "eth_getTransactionCount",
                  "params": [eoa, "pending"], "id": 1},
            timeout=15.0,
        )
        nonce = int(nonce_resp.json()["result"], 16)

        gp_resp = await http_client.post(
            rpc_url,
            json={"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 2},
            timeout=15.0,
        )
        gas_price = int(int(gp_resp.json()["result"], 16) * 1.2)  # 20% buffer

        tx = {
            "to": _PROXY_FACTORY,
            "from": eoa,
            "nonce": nonce,
            "gas": 250_000,
            "gasPrice": gas_price,
            "data": "0x" + proxy_calldata.hex(),
            "value": 0,
            "chainId": _POLYGON_CHAIN_ID,
        }
        signed = account.sign_transaction(tx)

        send_resp = await http_client.post(
            rpc_url,
            json={"jsonrpc": "2.0", "method": "eth_sendRawTransaction",
                  "params": ["0x" + signed.raw_transaction.hex()], "id": 3},
            timeout=15.0,
        )
        resp = send_resp.json()
        if "error" in resp:
            err = resp["error"].get("message", str(resp["error"]))
            log.warning("Redemption rejected condition_id=%.16s: %s", condition_id, err)
            return False, err

        tx_hash: str = resp.get("result", "")
        log.info("Redemption submitted condition_id=%.16s tx=%s", condition_id, tx_hash)
        return True, tx_hash

    except Exception as exc:
        log.warning("Redemption error condition_id=%.16s: %s", condition_id, exc)
        return False, str(exc)


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

async def run_settlement_loop(
    *,
    jsonl_dir: Path,
    writer: JsonlRunWriter,
    fetch_resolution: Callable,  # injectable for testing
    redeem_fn: Callable | None = None,  # optional: async (condition_id, token_side) → (bool, str)
    poll_interval_seconds: float = 900.0,
    max_iterations: int = 0,  # 0 = run forever
    report_md_path: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> None:
    """Main polling loop."""
    _now = now_fn or (lambda: datetime.now(tz=UTC))
    iteration = 0

    while True:
        iteration += 1
        log.info("Settlement poll iteration %d", iteration)

        entries = scan_unresolved_entries(jsonl_dir)
        if not entries:
            log.info("No unresolved entries found. Exiting.")
            return

        # Deduplicate by token_id (take first entry per token_id)
        seen_tokens: dict[str, list[UnresolvedEntry]] = {}
        for entry in entries:
            seen_tokens.setdefault(entry.token_id, []).append(entry)

        settlements_written = 0

        for token_id, group in seen_tokens.items():
            resolution = await fetch_resolution(token_id=token_id)
            if resolution is None:
                continue

            for entry in group:
                event = compute_settlement(entry, resolution)
                if event is None:
                    continue
                event["timestamp"] = _now().isoformat()
                writer.write(event)
                settlements_written += 1
                log.info(
                    "Settled %s (%s): pnl=%.4f outcome=%s",
                    entry.market_slug,
                    entry.token_id[-8:],
                    event["pnl"],
                    event["resolved_outcome"],
                )

        # Refresh report if settlements were written and path is configured
        if settlements_written > 0 and report_md_path:
            _refresh_report(jsonl_dir, report_md_path)

        # Attempt on-chain redemption for any winning positions not yet redeemed
        if redeem_fn is not None:
            await _redeem_pending_wins(
                jsonl_dir=jsonl_dir,
                writer=writer,
                redeem_fn=redeem_fn,
                now_fn=_now,
            )

        if 0 < max_iterations <= iteration:
            log.info("Reached max_iterations=%d. Exiting.", max_iterations)
            return

        if poll_interval_seconds > 0:
            await asyncio.sleep(poll_interval_seconds)


async def _redeem_pending_wins(
    *,
    jsonl_dir: Path,
    writer: JsonlRunWriter,
    redeem_fn: Callable,
    now_fn: Callable[[], datetime],
) -> None:
    """Scan JSONL for WIN settlements without a completed redemption and attempt redemption.

    One redemption attempt per condition_id per poll cycle.  Already-completed
    redemptions (event=redemption_completed) are permanently skipped.
    """
    all_rows = _read_all_jsonl_rows(jsonl_dir)
    redeemed_cids = _collect_redeemed_condition_ids(all_rows)

    # Collect the LATEST settlement_update per condition_id.
    # Using latest-wins so that a correction record (resolved_outcome=loss)
    # appended after a spurious WIN will cancel the redemption attempt.
    latest_settlements: dict[str, dict] = {}
    for _fname, row in all_rows:
        if row.get("event") != "settlement_update":
            continue
        cid = row.get("condition_id", "")
        if not cid:
            continue
        latest_settlements[cid] = row  # later rows overwrite earlier ones

    pending: dict[str, dict] = {}
    for cid, row in latest_settlements.items():
        if row.get("resolved_outcome") != "win":
            continue
        if cid in redeemed_cids:
            continue
        # Only attempt redemption for confirmed (real CLOB) entries.
        # weather_temp_live_* entries are NautilusTrader sandbox paper trades —
        # they never exist on-chain, so redemption is impossible and pointless.
        if not row.get("real_order", False):
            continue
        pending[cid] = row

    if not pending:
        return

    log.info("Attempting redemption for %d winning condition(s)", len(pending))

    for cid, win_row in pending.items():
        token_side = win_row.get("token_side", "yes")
        success, result = await redeem_fn(condition_id=cid, token_side=token_side)

        redemption_event: dict[str, Any] = {
            "event": "redemption_completed" if success else "redemption_pending",
            "condition_id": cid,
            "market_slug": win_row.get("market_slug", ""),
            "token_id": win_row.get("token_id", ""),
            "token_side": token_side,
            "shares": win_row.get("shares", 0.0),
            "timestamp": now_fn().isoformat(),
        }
        if success:
            redemption_event["tx_hash"] = result
        else:
            redemption_event["error"] = result

        writer.write(redemption_event)

        if success:
            log.info(
                "Redemption completed condition_id=%.16s shares=%.4f tx=%s",
                cid, win_row.get("shares", 0.0), result,
            )
        else:
            log.warning(
                "Redemption pending condition_id=%.16s: %s (will retry next poll)",
                cid, result,
            )


def _refresh_report(jsonl_dir: Path, report_md_path: str) -> None:
    """Rebuild the markdown report from all JSONL data."""
    if build_weather_temperature_summary is None or render_weather_temperature_markdown is None:
        log.warning("Report module not available; skipping report refresh.")
        return

    all_rows: list[dict] = []
    for jsonl_file in _live_jsonl_files(jsonl_dir):
        try:
            with jsonl_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        all_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue

    summary = build_weather_temperature_summary(all_rows)
    md = render_weather_temperature_markdown(summary)

    md_path = Path(report_md_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md, encoding="utf-8")
    log.info("Report refreshed: %s", report_md_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll for weather market settlements")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="JSONL output directory")
    parser.add_argument("--poll-interval", type=float, default=900.0, help="seconds between polls (default 15 min)")
    parser.add_argument("--max-iterations", type=int, default=0, help="0 = poll forever")
    parser.add_argument("--report-md", default="", help="path to refresh markdown report after settlements")
    parser.add_argument("--clob-host", default="https://clob.polymarket.com", help="CLOB API base URL")
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    import os

    try:
        import httpx
    except ImportError:
        raise SystemExit("httpx is required: pip install httpx")

    jsonl_dir = Path(args.output_dir)
    writer = JsonlRunWriter(jsonl_dir / "settlement_live.jsonl")

    rpc_url = os.environ.get("POLYMARKET_RPC_URL", "")
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")

    async with httpx.AsyncClient() as client:
        async def _fetch(*, token_id: str) -> MarketResolution | None:
            return await fetch_token_resolution(
                token_id=token_id,
                http_client=client,
                clob_base_url=args.clob_host,
            )

        _redeem: Callable | None = None
        if rpc_url and private_key:
            async def _redeem(*, condition_id: str, token_side: str) -> tuple[bool, str]:
                return await redeem_winning_position(
                    condition_id=condition_id,
                    token_side=token_side,
                    rpc_url=rpc_url,
                    private_key=private_key,
                    http_client=client,
                )
            log.info("Auto-redemption enabled (POLYMARKET_RPC_URL + POLYMARKET_PRIVATE_KEY set)")
        else:
            log.warning(
                "Auto-redemption disabled: POLYMARKET_RPC_URL and/or POLYMARKET_PRIVATE_KEY not set"
            )

        await run_settlement_loop(
            jsonl_dir=jsonl_dir,
            writer=writer,
            fetch_resolution=_fetch,
            redeem_fn=_redeem,
            poll_interval_seconds=args.poll_interval,
            max_iterations=args.max_iterations,
            report_md_path=args.report_md or None,
        )


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
