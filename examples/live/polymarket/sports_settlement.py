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

Pure orchestration — reads JSONL files and queries Gamma API for market
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


@dataclass(frozen=True, slots=True)
class MarketResolution:
    """Resolution data for a market from Gamma API."""
    condition_id: str
    slug: str
    resolved: bool
    winning_outcome: str | None  # outcome name or None if not resolved
    resolution_price: float | None  # 1.0 or 0.0


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
    if not resolution.resolved:
        return None
    if resolution.winning_outcome is None or resolution.resolution_price is None:
        return None

    # Determine per-entry settlement price by comparing this entry's outcome
    # against the market's winning outcome.  resolution_price=1.0 marks which
    # outcome won at the market level; each entry must be matched individually.
    if entry.outcome_name.strip().lower() == resolution.winning_outcome.strip().lower():
        settlement_price = 1.0
    else:
        settlement_price = 0.0

    pnl = (settlement_price - entry.entry_price) * entry.shares

    if pnl > 0:
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
        "pnl": pnl,
        "resolved": True,
        "resolved_outcome": resolved_outcome,
    }


# ---------------------------------------------------------------------------
# Gamma API fetch
# ---------------------------------------------------------------------------

async def fetch_market_resolution(
    *,
    condition_id: str,
    outcome_name: str,
    market_slug: str = "",
    http_client: Any,
    gamma_base_url: str,
    timeout: float = 15.0,
) -> MarketResolution | None:
    """Query Gamma API for a market's resolution status.

    Queries by slug (reliable) rather than condition_id (Gamma's condition_id
    param is broken and returns wrong markets).  Falls back to condition_id
    if no slug is available.
    """
    # Prefer slug — the condition_id query param is unreliable on Gamma.
    # Must include closed=true: Gamma hides closed markets from default results,
    # so without it a resolved market returns an empty list (appears unresolved).
    query_param = ("slug", market_slug) if market_slug else ("condition_id", condition_id)
    try:
        response = await http_client.get(
            f"{gamma_base_url}/markets",
            params={query_param[0]: query_param[1], "closed": "true"},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        log.warning("Failed to fetch resolution for %s=%s", query_param[0], query_param[1])
        return None

    # API returns a list; find our market
    markets = data if isinstance(data, list) else [data]
    if not markets:
        # Nothing found even with closed=true — market genuinely not settled yet
        return MarketResolution(
            condition_id=condition_id,
            slug=market_slug,
            resolved=False,
            winning_outcome=None,
            resolution_price=None,
        )

    market = markets[0]

    slug = market.get("slug", "")
    closed = market.get("closed", False)
    resolved = market.get("resolved", False)

    # Treat as resolved if closed=True and prices are definitively 0/1.
    # Polymarket closes markets before setting resolved=True (oracle delay),
    # so checking only resolved=True would miss all newly-closed markets.
    if not closed:
        return MarketResolution(
            condition_id=condition_id,
            slug=slug,
            resolved=False,
            winning_outcome=None,
            resolution_price=None,
        )

    # Determine winning outcome from outcomes + outcomePrices
    outcomes = market.get("outcomes")
    outcome_prices_raw = market.get("outcomePrices")

    # Parse outcomes
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (ValueError, TypeError):
            outcomes = None
    if not isinstance(outcomes, list):
        outcomes = None

    # Parse outcome prices
    if isinstance(outcome_prices_raw, str):
        try:
            outcome_prices_raw = json.loads(outcome_prices_raw)
        except (ValueError, TypeError):
            outcome_prices_raw = None
    if not isinstance(outcome_prices_raw, list):
        outcome_prices_raw = None

    # Find the winning outcome (the one whose price settled at 1.0).
    # Do NOT match against outcome_name here — the caller queries once per market
    # and applies the result across multiple entries with different outcome_names.
    # Per-entry win/loss is determined later in compute_settlement.
    if outcomes and outcome_prices_raw and len(outcomes) == len(outcome_prices_raw):
        for resolved_outcome, price in zip(outcomes, outcome_prices_raw):
            price_float = float(price) if isinstance(price, (int, float, str)) else None
            if price_float is None:
                continue
            if price_float == 1.0:
                return MarketResolution(
                    condition_id=condition_id,
                    slug=slug,
                    resolved=True,
                    winning_outcome=resolved_outcome,
                    resolution_price=1.0,
                )

        # No outcome found at price=1.0 — market closed but prices not yet final
        return MarketResolution(
            condition_id=condition_id,
            slug=slug,
            resolved=False,
            winning_outcome=None,
            resolution_price=None,  # not yet settled
        )

    # Cannot determine resolution clearly — be conservative
    return None


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
                log.info(
                    "Settled %s (%s): pnl=%.4f outcome=%s",
                    entry.market_slug,
                    entry.condition_id,
                    event["pnl"],
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
    parser.add_argument("--gamma-host", default="https://gamma-api.polymarket.com")
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
                market_slug=market_slug,
                outcome_name="",  # Not used in the resolution lookup; all outcomes handled per-group
                http_client=client,
                gamma_base_url=args.gamma_host,
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
