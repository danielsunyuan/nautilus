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
Settlement polling resolver for Polymarket sports markets.

Pure orchestration — reads JSONL files and queries the CLOB API for market
resolution status. No Nautilus TradingNode, no Strategy classes.
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

log = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = "/workspace/outputs"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UnresolvedEntry:
    """An unresolved strategy_result entry extracted from JSONL."""
    market_slug: str
    condition_id: str
    preset_name: str
    arena: str
    entry_price: float
    shares: float
    stake: float
    sport: str
    match_title: str
    outcome_name: str
    game_time: str
    source_file: str  # which JSONL file it came from
    entry_fee: float | None = None
    fee_status: str = "missing"
    token_id: str = ""


@dataclass(frozen=True, slots=True)
class MarketResolution:
    """Resolution data for a market from the CLOB API."""
    condition_id: str
    slug: str
    resolved: bool
    winning_outcome: str | None  # outcome name or None if not resolved
    resolution_price: float | None  # 1.0 or 0.0
    winning_token_id: str | None = None
    resolution_source: str | None = None
    observed_at: str | None = None


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------

class JsonlRunWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _read_all_jsonl_rows(jsonl_dir: Path) -> list[tuple[str, dict]]:
    """Read all rows from all JSONL files in directory. Returns (filename, row) tuples."""
    results: list[tuple[str, dict]] = []
    if not jsonl_dir.exists():
        return results
    for jsonl_file in sorted(jsonl_dir.glob("*.jsonl")):
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


def _collect_settled_keys(all_rows: list[tuple[str, dict]]) -> set[tuple[str, str, str]]:
    """Collect (condition_id, preset_name, outcome_name) triples that already have a settlement_update."""
    settled: set[tuple[str, str, str]] = set()
    for _fname, row in all_rows:
        if row.get("event") == "settlement_update" and row.get("resolved") is True:
            cid = row.get("condition_id", "")
            preset = row.get("preset_name", "")
            outcome = row.get("outcome_name", "")
            if cid:
                settled.add((cid, preset, outcome))
    return settled


def scan_unresolved_entries(jsonl_dir: Path) -> list[UnresolvedEntry]:
    """Scan all JSONL files in directory for unresolved strategy_result rows.

    Deduplicates by (condition_id, preset_name, outcome_name) — the same market
    can be entered repeatedly across daemon rounds, but should only be settled once
    per unique (market, preset, outcome) combination.
    """
    all_rows = _read_all_jsonl_rows(jsonl_dir)
    settled = _collect_settled_keys(all_rows)

    # Deduplicate: keep the last-seen entry for each (condition_id, preset_name, outcome_name).
    # Using last-seen means we get the most recent entry_price / shares if they drift,
    # and naturally skip rounds that produced no fill (no_position rows are excluded below).
    seen: dict[tuple[str, str, str], UnresolvedEntry] = {}

    for fname, row in all_rows:
        if row.get("event") != "strategy_result":
            continue
        if row.get("resolved") is True:
            continue
        # Skip no_position rows — nothing to settle
        if row.get("accounting_status") == "no_position" or row.get("entry_price") is None:
            continue
        condition_id = row.get("condition_id", "")
        preset_name = row.get("preset_name", "")
        outcome_name = row.get("outcome_name", "")
        key = (condition_id, preset_name, outcome_name)
        if key in settled:
            continue
        seen[key] = UnresolvedEntry(
            market_slug=row.get("market_slug", ""),
            condition_id=condition_id,
            preset_name=preset_name,
            arena=row.get("arena", ""),
            entry_price=float(row.get("entry_price")),
            shares=float(row.get("shares")),
            stake=float(row.get("stake")),
            sport=row.get("sport", ""),
            match_title=row.get("match_title", ""),
            outcome_name=outcome_name,
            game_time=row.get("game_time", ""),
            source_file=fname,
            entry_fee=(
                float(row["entry_fee"])
                if row.get("fee_status") == "known" and row.get("entry_fee") is not None
                else None
            ),
            fee_status=(
                "known"
                if row.get("fee_status") == "known" and row.get("entry_fee") is not None
                else "missing"
            ),
            token_id=(
                row["token_id"]
                if isinstance(row.get("token_id"), str)
                else ""
            ),
        )

    return list(seen.values())


# ---------------------------------------------------------------------------
# Settlement computation
# ---------------------------------------------------------------------------

def _infer_market_type(slug: str) -> str:
    """Infer market type from slug string."""
    s = slug.lower()
    if "moneyline" in s or "-ml-" in s:
        return "moneyline"
    if "spread" in s:
        return "spread"
    if "total" in s or "-over-" in s or "-under-" in s:
        return "total"
    return "other"


def compute_settlement(
    entry: UnresolvedEntry,
    resolution: MarketResolution,
) -> dict | None:
    """
    Compute settlement_update event dict from entry + resolution.

    For sports markets, we hold the specific outcome token. If that outcome won,
    the token settles at 1.0. If it lost, 0.0.

    Returns None if market not yet resolved.
    """
    if not resolution.resolved or resolution.condition_id != entry.condition_id:
        return None
    if (
        not entry.token_id
        or not resolution.winning_token_id
        or resolution.winning_outcome is None
        or resolution.resolution_price is None
    ):
        return None

    if entry.token_id == resolution.winning_token_id:
        settlement_price = 1.0
    else:
        settlement_price = 0.0

    gross_pnl = (settlement_price - entry.entry_price) * entry.shares
    fee_known = entry.fee_status == "known" and entry.entry_fee is not None
    entry_fee = entry.entry_fee if fee_known else None
    net_pnl = gross_pnl - entry_fee if entry_fee is not None else None

    outcome_pnl = net_pnl if net_pnl is not None else gross_pnl
    if outcome_pnl > 0:
        resolved_outcome = "win"
    else:
        resolved_outcome = "loss"

    return {
        "run_id": f"settlement-{uuid.uuid4()}",
        "event": "settlement_update",
        "market_slug": entry.market_slug,
        "market_type": _infer_market_type(entry.market_slug),
        "condition_id": entry.condition_id,
        "preset_name": entry.preset_name,
        "arena": entry.arena,
        "sport": entry.sport,
        "match_title": entry.match_title,
        "outcome_name": entry.outcome_name,
        "game_time": entry.game_time,
        "entry_price": entry.entry_price,
        "settlement_price": settlement_price,
        "shares": entry.shares,
        "stake": entry.stake,
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "net_pnl": net_pnl,
        "fee_status": "known" if fee_known else "missing",
        "pnl": net_pnl,
        "resolved": True,
        "resolved_outcome": resolved_outcome,
        "winning_token_id": resolution.winning_token_id,
        "winning_outcome": resolution.winning_outcome,
        "resolution_source": resolution.resolution_source,
        "resolution_observed_at": resolution.observed_at,
    }


# ---------------------------------------------------------------------------
# CLOB API fetch
# ---------------------------------------------------------------------------

async def fetch_market_resolution(
    *,
    condition_id: str,
    http_client: Any,
    clob_base_url: str,
    timeout: float = 15.0,
    now_fn: Callable[[], datetime] | None = None,
) -> MarketResolution | None:
    """Query the CLOB market endpoint and return exact winning-token evidence."""
    endpoint = f"{clob_base_url.rstrip('/')}/markets/{condition_id}"
    _now = now_fn or (lambda: datetime.now(tz=UTC))

    try:
        response = await http_client.get(endpoint, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception:
        log.warning("Failed to fetch CLOB resolution for condition_id=%s", condition_id)
        return None

    observed_at = _now().isoformat()
    if not isinstance(data, dict) or data.get("condition_id") != condition_id:
        log.warning("Rejected mismatched or malformed CLOB market for %s", condition_id)
        return None

    slug = str(data.get("market_slug") or data.get("slug") or "")
    closed = data.get("closed")
    if closed is False:
        return MarketResolution(
            condition_id=condition_id,
            slug=slug,
            resolved=False,
            winning_outcome=None,
            resolution_price=None,
            resolution_source=endpoint,
            observed_at=observed_at,
        )
    if closed is not True:
        log.warning("Rejected CLOB market with invalid closed state for %s", condition_id)
        return None

    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        log.warning("Rejected CLOB market without token data for %s", condition_id)
        return None

    winners = [
        token
        for token in tokens
        if isinstance(token, dict) and token.get("winner") is True
    ]
    if len(winners) != 1:
        log.warning(
            "Rejected CLOB market with %d winning tokens for %s",
            len(winners),
            condition_id,
        )
        return None

    winner = winners[0]
    winning_token_id = winner.get("token_id")
    winning_outcome = winner.get("outcome")
    if (
        not isinstance(winning_token_id, str)
        or not winning_token_id
        or not isinstance(winning_outcome, str)
        or not winning_outcome
    ):
        log.warning("Rejected malformed winning token for %s", condition_id)
        return None

    return MarketResolution(
        condition_id=condition_id,
        slug=slug,
        resolved=True,
        winning_outcome=winning_outcome,
        resolution_price=1.0,
        winning_token_id=winning_token_id,
        resolution_source=endpoint,
        observed_at=observed_at,
    )


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

async def run_settlement_loop(
    *,
    jsonl_dir: Path,
    writer: JsonlRunWriter,
    fetch_resolution: Callable,  # injectable for testing
    poll_interval_seconds: float = 900.0,
    max_iterations: int = 0,  # 0 = run forever
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
            log.info("No unresolved entries found. Sleeping %gs.", poll_interval_seconds)
            if poll_interval_seconds > 0:
                await asyncio.sleep(poll_interval_seconds)
            if 0 < max_iterations <= iteration:
                log.info("Reached max_iterations=%d. Exiting.", max_iterations)
                return
            continue

        # Deduplicate by condition_id (take first entry per condition_id)
        seen_cids: dict[str, list[UnresolvedEntry]] = {}
        for entry in entries:
            seen_cids.setdefault(entry.condition_id, []).append(entry)

        settlements_written = 0

        for condition_id, group in seen_cids.items():
            # All entries in a group have the same condition_id but may differ in outcome_name.
            # Query resolution once (by slug — more reliable than condition_id on Gamma),
            # then apply to all outcomes in the group.
            slug = group[0].market_slug if group else ""
            resolution = await fetch_resolution(condition_id=condition_id, market_slug=slug)
            if resolution is None:
                continue

            for entry in group:
                event = compute_settlement(entry, resolution)
                if event is None:
                    continue
                event["timestamp"] = _now().isoformat()
                writer.write(event)
                settlements_written += 1
                net_pnl = event["net_pnl"]
                net_pnl_text = f"{net_pnl:.4f}" if net_pnl is not None else "unknown"
                log.info(
                    "Settled %s (%s): gross_pnl=%.4f net_pnl=%s outcome=%s",
                    entry.market_slug,
                    entry.condition_id,
                    event["gross_pnl"],
                    net_pnl_text,
                    event["resolved_outcome"],
                )

        if 0 < max_iterations <= iteration:
            log.info("Reached max_iterations=%d. Exiting.", max_iterations)
            return

        if poll_interval_seconds > 0:
            await asyncio.sleep(poll_interval_seconds)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll for sports market settlements")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="JSONL output directory")
    parser.add_argument("--poll-interval", type=float, default=900.0, help="seconds between polls (default 15 min)")
    parser.add_argument("--max-iterations", type=int, default=0, help="0 = poll forever")
    parser.add_argument("--clob-host", default="https://clob.polymarket.com")
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    try:
        import httpx
    except ImportError:
        raise SystemExit("httpx is required: pip install httpx")

    jsonl_dir = Path(args.output_dir) / "polymarket" / "sports"
    writer = JsonlRunWriter(jsonl_dir / "settlement.jsonl")

    async with httpx.AsyncClient() as client:
        async def _fetch(*, condition_id: str, market_slug: str = "") -> MarketResolution | None:
            return await fetch_market_resolution(
                condition_id=condition_id,
                http_client=client,
                clob_base_url=args.clob_host,
            )

        await run_settlement_loop(
            jsonl_dir=jsonl_dir,
            writer=writer,
            fetch_resolution=_fetch,
            poll_interval_seconds=args.poll_interval,
            max_iterations=args.max_iterations,
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
