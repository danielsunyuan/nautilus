#!/usr/bin/env python3
"""Display a Polymarket order book for a market slug or token id."""

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
    DEFAULT_CLOB_HOST,
    DEFAULT_GAMMA_BASE_URL,
    DisplayStyle,
    coerce_list,
    dump_json,
    fetch_market_by_slug,
    fetch_order_book,
    render_order_book,
    use_color,
)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print a Polymarket CLOB order book.")
    parser.add_argument("--slug", default="")
    parser.add_argument("--token-id", default="")
    parser.add_argument("--outcome-index", type=int, default=0)
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL)
    parser.add_argument("--clob-host", default=DEFAULT_CLOB_HOST)
    parser.add_argument("--depth", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    return parser


async def _resolve_token_id(args: argparse.Namespace) -> tuple[str, str]:
    if args.token_id.strip():
        return args.token_id.strip(), f"token {args.token_id.strip()}"

    if not args.slug.strip():
        raise ValueError("provide --slug or --token-id")

    http_client = HttpClient(timeout_secs=max(1, int(args.timeout)))
    market = await fetch_market_by_slug(
        slug=args.slug.strip(),
        http_client=http_client,
        gamma_host=args.gamma_host,
        timeout=args.timeout,
    )
    token_ids = coerce_list(market.get("clobTokenIds", []), name="clobTokenIds")
    outcomes = coerce_list(market.get("outcomes", []), name="outcomes")
    if not token_ids:
        raise ValueError(f"market {args.slug.strip()!r} has no clobTokenIds")
    if args.outcome_index < 0 or args.outcome_index >= len(token_ids):
        raise ValueError(f"--outcome-index must be between 0 and {len(token_ids) - 1}")
    outcome = str(outcomes[args.outcome_index]) if args.outcome_index < len(outcomes) else f"#{args.outcome_index}"
    question = str(market.get("question") or args.slug.strip())
    label = f"{question} [{outcome}]"
    return str(token_ids[args.outcome_index]), label


async def _run(args: argparse.Namespace) -> int:
    token_id, label = await _resolve_token_id(args)
    book = fetch_order_book(token_id=token_id, clob_host=args.clob_host)
    if args.json:
        dump_json({"label": label, "token_id": token_id, "book": book})
        return 0
    style = DisplayStyle(use_color(no_color_flag=bool(args.no_color)))
    print(render_order_book(label=label, book=book, depth=args.depth, style=style), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.depth < 1:
        parser.error("--depth must be >= 1")
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
