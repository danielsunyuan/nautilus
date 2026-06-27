#!/usr/bin/env python3
"""Live Gaussian view of World Cup winner probabilities from CLOB mids."""

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
    clear_fast_stream,
    fetch_gamma_event_by_slug,
    fetch_order_books_many,
    load_winner_market_entries,
    render_winner_gaussian_stream,
    top_of_book_mid,
    use_color,
    write_fast_stream,
)

DEFAULT_EVENT_SLUG = "world-cup-winner"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stream World Cup winner probabilities as a live Gaussian.")
    parser.add_argument("--event-slug", default=DEFAULT_EVENT_SLUG)
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL)
    parser.add_argument("--clob-host", default=DEFAULT_CLOB_HOST)
    parser.add_argument("--list-limit", type=int, default=8)
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-color", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    style = DisplayStyle(use_color(no_color_flag=bool(args.no_color)))
    http_client = HttpClient(timeout_secs=max(1, int(args.timeout)))
    event = await fetch_gamma_event_by_slug(
        slug=args.event_slug.strip(),
        http_client=http_client,
        gamma_host=args.gamma_host,
        timeout=args.timeout,
    )
    entries = load_winner_market_entries(event)
    if not entries:
        raise RuntimeError(f"no active winner markets found for event {args.event_slug!r}")

    title = str(event.get("title") or "World Cup Winner")
    first = True
    reset = False
    line_count = 1

    while True:
        token_ids = [str(row["yes_token_id"]) for row in entries]
        books = fetch_order_books_many(
            token_ids=token_ids,
            clob_host=args.clob_host,
            max_workers=args.workers,
        )

        rows: list[tuple[str, dict]] = []
        for entry in entries:
            token_id = str(entry["yes_token_id"])
            book = books.get(token_id)
            if book is None:
                continue
            rows.append((str(entry["label"]), book))

        rows.sort(key=lambda row: top_of_book_mid(row[1]), reverse=True)

        if not rows:
            waiting = f"{style.dim}waiting for World Cup winner books...{style.reset}"
            write_fast_stream(waiting, line_count=1, first=True, reset=True)
            await asyncio.sleep(max(0.1, args.interval))
            continue

        rendered = render_winner_gaussian_stream(
            title=title,
            rows=rows,
            style=style,
            list_limit=args.list_limit,
        )
        line_count = rendered.count("\n") + 1
        write_fast_stream(rendered, line_count=line_count, first=first, reset=reset)
        first = False
        reset = False
        await asyncio.sleep(max(0.1, args.interval))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_limit < 0:
        parser.error("--list-limit must be >= 0")
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        clear_fast_stream()
        print(flush=True)
        return 130
    except Exception as exc:
        clear_fast_stream()
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
