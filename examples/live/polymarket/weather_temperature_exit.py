#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
Manual exit tool for open Polymarket weather temperature positions.

Submits a market SELL directly via py_clob_client — no Nautilus TradingNode needed.
Must be run from inside a VPN-connected container (CLOB API is geo-blocked on host).

CLI usage:
    python weather_temperature_exit.py --market-slug "highest-temperature-in-austin-on-april-20-2026-69forbelow"
    python weather_temperature_exit.py --market-slug "..." --dry-run

Via docker exec:
    docker exec polynautilus-polymarket-weather-live-vpn-1 \\
        python /workspace/nautilus/examples/live/polymarket/weather_temperature_exit.py \\
        --market-slug "highest-temperature-in-austin-on-april-20-2026-69forbelow"

Or via the HTTP server (if running):
    curl -s -X POST localhost:8080/exit -H "Content-Type: application/json" \\
         -d '{"market_slug": "highest-temperature-in-austin-on-april-20-2026-69forbelow"}'
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
from datetime import UTC, datetime
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# HTTP/1.1 monkeypatch — same as live daemon. The py_clob_client module-level
# httpx.Client(http2=True) singleton is not thread-safe; replace before any use.
import py_clob_client.http_helpers.helpers as _poly_helpers  # noqa: E402
import httpx as _httpx  # noqa: E402
_poly_helpers._http_client = _httpx.Client(http2=False)
del _poly_helpers, _httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("weather.exit")

DEFAULT_OUTPUT_DIR = Path("/workspace/nautilus/outputs")

# Buffer to avoid on-chain balance rejections. Polymarket deducts taker fees
# from received tokens at fill time, so the wallet balance can be slightly
# less than the JSONL-recorded share count. 0.03 is conservative enough to
# cover the fee haircut without leaving meaningful value on the table.
SELL_BUFFER_SHARES = Decimal("0.03")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_first(*names: str) -> str | None:
    for name in names:
        v = os.getenv(name)
        if v:
            return v
    return None


def _token_id_from_instrument_id(instrument_id: str) -> str:
    """Extract YES token ID from instrument_id format: 0xCOND-TOKEN.POLYMARKET"""
    return instrument_id.split(".POLYMARKET")[0].rsplit("-", 1)[1]


def find_open_positions(output_dir: Path) -> list[dict]:
    """Return all open strategy_result entries across all JSONL run files."""
    runs_dir = output_dir.resolve() / "polymarket" / "runs"
    if not runs_dir.exists():
        return []
    seen: dict[str, dict] = {}  # slug -> latest open row
    for jsonl_file in sorted(runs_dir.glob("*.jsonl")):
        try:
            lines = jsonl_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if (
                row.get("event") == "strategy_result"
                and row.get("accounting_status") == "open"
                and row.get("market_slug")
            ):
                seen[row["market_slug"]] = row
    return list(seen.values())


def find_position(output_dir: Path, market_slug: str) -> dict | None:
    """Find the most recent open position for the given market_slug."""
    for pos in find_open_positions(output_dir):
        if pos["market_slug"] == market_slug:
            return pos
    return None


def _get_clob_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

    host = _env_first("POLYMARKET_CLOB_HOST") or "https://clob.polymarket.com"
    private_key = _env_first("POLYMARKET_PRIVATE_KEY", "POLYMARKET_PK")
    funder = _env_first("POLYMARKET_FUNDER_ADDRESS", "POLYMARKET_FUNDER")
    sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))

    if not private_key or not funder:
        raise RuntimeError(
            "POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS must be set."
        )

    client = ClobClient(host, chain_id=POLYGON, key=private_key, funder=funder, signature_type=sig_type)

    api_key = _env_first("POLYMARKET_CLOB_API_KEY", "POLYMARKET_API_KEY")
    api_secret = _env_first("POLYMARKET_CLOB_API_SECRET", "POLYMARKET_API_SECRET")
    passphrase = _env_first("POLYMARKET_CLOB_PASSPHRASE", "POLYMARKET_PASSPHRASE")

    if not (api_key and api_secret and passphrase):
        log.info("Deriving CLOB API credentials from private key (one-time network call)...")
        creds = client.create_or_derive_api_creds()
        api_key = creds.api_key
        api_secret = creds.api_secret
        passphrase = creds.api_passphrase

    from py_clob_client.clob_types import ApiCreds
    client.set_api_creds(ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=passphrase,
    ))
    return client


# ---------------------------------------------------------------------------
# Core exit logic
# ---------------------------------------------------------------------------

def submit_exit(
    *,
    market_slug: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    dry_run: bool = False,
) -> dict:
    """
    Find the open position for *market_slug* and submit a market SELL order.

    Returns a result dict with keys:
        market_slug, token_id, shares, qty_sold, dry_run, response (if live), timestamp
    """
    position = find_position(output_dir, market_slug)
    if not position:
        raise ValueError(f"No open position found for market_slug={market_slug!r}")

    shares = Decimal(str(position["shares"]))
    instrument_id = position.get("instrument_id", "")
    if not instrument_id:
        raise ValueError(f"Position for {market_slug!r} has no instrument_id field")

    token_id = _token_id_from_instrument_id(instrument_id)
    entry_price = float(position.get("entry_price", 0))
    city = position.get("city", "")
    observation_date = position.get("observation_date", "")

    qty = shares - SELL_BUFFER_SHARES
    if qty <= Decimal("0"):
        raise ValueError(
            f"Position too small to exit after {SELL_BUFFER_SHARES} share buffer: shares={shares}"
        )

    log.info(
        "EXIT REQUEST  slug=%s  city=%s  date=%s  entry=%.3f  shares=%s  qty_to_sell=%s",
        market_slug, city, observation_date, entry_price, shares, qty,
    )

    if dry_run:
        log.info("[DRY RUN] Would submit SELL %s shares of token %s...%s", qty, token_id[:8], token_id[-4:])
        return {
            "dry_run": True,
            "market_slug": market_slug,
            "city": city,
            "observation_date": observation_date,
            "token_id": token_id,
            "shares": str(shares),
            "qty_to_sell": str(qty),
            "entry_price": entry_price,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL

    client = _get_clob_client()

    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=float(qty),
        side=SELL,
    )
    signed_order = client.create_market_order(order_args)
    resp = client.post_order(signed_order, OrderType.FOK)

    log.info("SELL submitted  slug=%s  qty=%s  response=%s", market_slug, qty, resp)

    return {
        "dry_run": False,
        "market_slug": market_slug,
        "city": city,
        "observation_date": observation_date,
        "token_id": token_id,
        "shares": str(shares),
        "qty_sold": str(qty),
        "entry_price": entry_price,
        "response": resp,
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manually exit an open Polymarket weather temperature position",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--market-slug", required=True,
        help='Market slug to exit, e.g. "highest-temperature-in-austin-on-april-20-2026-69forbelow"',
    )
    parser.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR),
        help=f"Output directory containing JSONL run files (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be submitted without sending the order",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    result = submit_exit(
        market_slug=args.market_slug,
        output_dir=Path(args.output_dir),
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
