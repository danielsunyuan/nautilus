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

Pure orchestration — reads JSONL files and queries Gamma API for market
resolution status.  No Nautilus TradingNode, no Strategy classes.
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


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UnresolvedEntry:
    """An unresolved strategy_result entry extracted from JSONL."""
    market_slug: str
    condition_id: str
    strategy_name: str
    arena: str
    token_side: str  # "yes" or "no"
    entry_price: float
    shares: float
    stake: float
    city: str
    observation_date: str
    source_file: str  # which JSONL file it came from


@dataclass(frozen=True, slots=True)
class MarketResolution:
    """Resolution data for a market from Gamma API."""
    condition_id: str
    slug: str
    resolved: bool
    winning_outcome: str | None  # "Yes" or "No" or None if not resolved
    resolution_price_yes: float | None  # 1.0 or 0.0
    resolution_price_no: float | None   # 0.0 or 1.0


# ---------------------------------------------------------------------------
# JSONL writer (same pattern as daemon)
# ---------------------------------------------------------------------------

class JsonlRunWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.write("\n")
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


def _collect_settled_condition_ids(all_rows: list[tuple[str, dict]]) -> set[str]:
    """Collect condition_ids that already have a settlement_update event."""
    settled: set[str] = set()
    for _fname, row in all_rows:
        if row.get("event") == "settlement_update" and row.get("resolved") is True:
            cid = row.get("condition_id")
            if cid:
                settled.add(cid)
    return settled


def scan_unresolved_entries(jsonl_dir: Path) -> list[UnresolvedEntry]:
    """Scan all JSONL files in directory for unresolved strategy_result rows."""
    all_rows = _read_all_jsonl_rows(jsonl_dir)
    settled = _collect_settled_condition_ids(all_rows)

    entries: list[UnresolvedEntry] = []
    for fname, row in all_rows:
        if row.get("event") != "strategy_result":
            continue
        if row.get("resolved") is True:
            continue
        condition_id = row.get("condition_id", "")
        if not condition_id:
            instrument_id = row.get("instrument_id", "")
            if instrument_id:
                condition_id = instrument_id.split(".POLYMARKET")[0].rsplit("-", 1)[0]
        if not condition_id:
            continue
        if condition_id in settled:
            continue
        entries.append(
            UnresolvedEntry(
                market_slug=row.get("market_slug", ""),
                condition_id=condition_id,
                strategy_name=row.get("strategy_name", ""),
                arena=row.get("arena", ""),
                token_side=row.get("token_side", "yes"),
                entry_price=float(row.get("entry_price", 0.0)),
                shares=float(row.get("shares", 0.0)),
                stake=float(row.get("stake", 0.0)),
                city=row.get("city", ""),
                observation_date=row.get("observation_date", ""),
                source_file=fname,
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
    if resolution.winning_outcome is None:
        return None

    # Determine settlement price for the token side we hold
    if entry.token_side == "yes":
        settlement_price = resolution.resolution_price_yes
    else:
        settlement_price = resolution.resolution_price_no

    if settlement_price is None:
        return None

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
        "strategy_name": entry.strategy_name,
        "arena": entry.arena,
        "city": entry.city,
        "observation_date": entry.observation_date,
        "token_side": entry.token_side,
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
    http_client: Any,
    gamma_base_url: str,
    timeout: float = 15.0,
) -> MarketResolution | None:
    """Query Gamma API for a market's resolution status."""
    try:
        response = await http_client.get(
            f"{gamma_base_url}/markets",
            params={"condition_id": condition_id},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        log.warning("Failed to fetch resolution for condition_id=%s", condition_id)
        return None

    # API returns a list; find our market
    markets = data if isinstance(data, list) else [data]
    if not markets:
        return None

    market = markets[0]

    slug = market.get("slug", "")
    closed = market.get("closed", False)
    resolved = market.get("resolved", False)

    if not closed or not resolved:
        return MarketResolution(
            condition_id=condition_id,
            slug=slug,
            resolved=False,
            winning_outcome=None,
            resolution_price_yes=None,
            resolution_price_no=None,
        )

    # Determine winning outcome from tokens or outcome_prices
    winning_outcome: str | None = None
    resolution_price_yes: float | None = None
    resolution_price_no: float | None = None

    # Try tokens array first
    tokens = market.get("tokens", [])
    for tok in tokens:
        outcome = (tok.get("outcome") or "").strip()
        winner = tok.get("winner", False)
        price = tok.get("price")
        if winner or (price is not None and str(price) == "1"):
            winning_outcome = outcome

    # Try outcome_prices field as fallback
    if winning_outcome is None:
        outcome_prices_str = market.get("outcome_prices", "")
        if outcome_prices_str:
            try:
                parts = [float(p.strip()) for p in outcome_prices_str.split(",")]
                if len(parts) == 2:
                    if parts[0] == 1.0 and parts[1] == 0.0:
                        winning_outcome = "Yes"
                    elif parts[0] == 0.0 and parts[1] == 1.0:
                        winning_outcome = "No"
            except (ValueError, AttributeError):
                pass

    if winning_outcome is None:
        # Cannot determine resolution clearly — be conservative
        return None

    if winning_outcome.lower() == "yes":
        resolution_price_yes = 1.0
        resolution_price_no = 0.0
    else:
        resolution_price_yes = 0.0
        resolution_price_no = 1.0

    return MarketResolution(
        condition_id=condition_id,
        slug=slug,
        resolved=True,
        winning_outcome=winning_outcome,
        resolution_price_yes=resolution_price_yes,
        resolution_price_no=resolution_price_no,
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

        # Deduplicate by condition_id (take first entry per condition_id)
        seen_cids: dict[str, list[UnresolvedEntry]] = {}
        for entry in entries:
            seen_cids.setdefault(entry.condition_id, []).append(entry)

        settlements_written = 0

        for condition_id, group in seen_cids.items():
            resolution = await fetch_resolution(condition_id=condition_id)
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

        # Refresh report if settlements were written and path is configured
        if settlements_written > 0 and report_md_path:
            _refresh_report(jsonl_dir, report_md_path)

        if 0 < max_iterations <= iteration:
            log.info("Reached max_iterations=%d. Exiting.", max_iterations)
            return

        if poll_interval_seconds > 0:
            await asyncio.sleep(poll_interval_seconds)


def _refresh_report(jsonl_dir: Path, report_md_path: str) -> None:
    """Rebuild the markdown report from all JSONL data."""
    if build_weather_temperature_summary is None or render_weather_temperature_markdown is None:
        log.warning("Report module not available; skipping report refresh.")
        return

    all_rows: list[dict] = []
    for jsonl_file in sorted(jsonl_dir.glob("*.jsonl")):
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
    parser.add_argument("--gamma-host", default="https://gamma-api.polymarket.com")
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    try:
        import httpx
    except ImportError:
        raise SystemExit("httpx is required: pip install httpx")

    jsonl_dir = Path(args.output_dir)
    writer = JsonlRunWriter(jsonl_dir / "settlement.jsonl")

    async with httpx.AsyncClient() as client:
        async def _fetch(*, condition_id: str) -> MarketResolution | None:
            return await fetch_market_resolution(
                condition_id=condition_id,
                http_client=client,
                gamma_base_url=args.gamma_host,
            )

        await run_settlement_loop(
            jsonl_dir=jsonl_dir,
            writer=writer,
            fetch_resolution=_fetch,
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
