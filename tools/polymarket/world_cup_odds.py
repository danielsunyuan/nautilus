#!/usr/bin/env python3
"""List Polymarket odds via Gamma public search."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))

import argparse
import asyncio
import sys

from nautilus_trader.core.nautilus_pyo3 import HttpClient

from tools.polymarket.display_common import (
    DEFAULT_GAMMA_BASE_URL,
    DisplayStyle,
    dump_json,
    fetch_public_search,
    iter_search_markets,
    render_market_odds_table,
    use_color,
)

DEFAULT_QUERY = "world cup"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search Polymarket and print outcome odds.")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL)
    parser.add_argument("--events-status", default="active", choices=("active", "closed", "all"))
    parser.add_argument("--limit-per-type", type=int, default=50)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--max-markets", type=int, default=0)
    parser.add_argument("--include-closed", action="store_true")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    http_client = HttpClient(timeout_secs=max(1, int(args.timeout)))
    payload = await fetch_public_search(
        query=args.query.strip(),
        http_client=http_client,
        gamma_host=args.gamma_host,
        events_status=None if args.include_closed else args.events_status,
        limit_per_type=args.limit_per_type,
        page=args.page,
        keep_closed_markets=bool(args.include_closed),
        timeout=args.timeout,
    )
    rows = iter_search_markets(payload)
    if args.json:
        dump_json({"query": args.query, "count": len(rows), "markets": rows, "raw": payload})
        return 0

    style = DisplayStyle(use_color(no_color_flag=bool(args.no_color)))
    if not rows:
        print(f"No markets found for query {args.query!r}", file=sys.stderr)
        return 1

    max_markets = None if args.max_markets <= 0 else args.max_markets
    print(render_market_odds_table(rows, style=style, max_markets=max_markets), flush=True)
    shown = len(rows) if max_markets is None else min(len(rows), max_markets)
    print(f"\nShowing {shown} of {len(rows)} markets for query {args.query!r}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
