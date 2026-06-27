#!/usr/bin/env python3
"""Display the current BTC Up/Down 5-minute Polymarket order books."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime

from nautilus_trader.adapters.polymarket.common.crypto_5m import resolve_crypto_5m_session
from nautilus_trader.core.nautilus_pyo3 import HttpClient

from tools.polymarket.display_common import (
    DEFAULT_CLOB_HOST,
    DEFAULT_GAMMA_BASE_URL,
    DisplayStyle,
    dump_json,
    fetch_order_book,
    render_order_book,
    use_color,
)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print Polymarket BTC 5-minute Up/Down order books from the CLOB.",
    )
    parser.add_argument("--asset", default="BTC")
    parser.add_argument("--market-slug", default="")
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL)
    parser.add_argument("--clob-host", default=DEFAULT_CLOB_HOST)
    parser.add_argument("--depth", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    return parser


async def _resolve_session(args: argparse.Namespace):
    http_client = HttpClient(timeout_secs=max(1, int(args.timeout)))
    if args.market_slug.strip():
        from nautilus_trader.adapters.polymarket.common.crypto_5m import (
            fetch_crypto_5m_market,
            parse_crypto_5m_market,
        )

        payload = await fetch_crypto_5m_market(
            slug=args.market_slug.strip(),
            http_client=http_client,
            gamma_base_url=args.gamma_host,
            timeout=args.timeout,
        )
        return parse_crypto_5m_market(payload, asset=args.asset)
    return await resolve_crypto_5m_session(
        asset=args.asset,
        http_client=http_client,
        gamma_base_url=args.gamma_host,
        timeout=args.timeout,
        validate_open=True,
    )


def _render_snapshot(*, session, args: argparse.Namespace, style: DisplayStyle) -> str:
    up_book = fetch_order_book(token_id=session.token_ids["up"], clob_host=args.clob_host)
    down_book = fetch_order_book(token_id=session.token_ids["down"], clob_host=args.clob_host)
    if args.json:
        dump_json(
            {
                "asset": session.asset,
                "slug": session.slug,
                "round_start": session.round_start.isoformat(),
                "end_time": session.end_time.isoformat(),
                "condition_id": session.condition_id,
                "question": session.question,
                "books": {"up": up_book, "down": down_book},
            },
        )
        return ""

    remaining = max(0, int((session.end_time - datetime.now(tz=UTC)).total_seconds()))
    minutes, seconds = divmod(remaining, 60)
    header = [
        f"{style.bold}{session.asset} Up/Down 5m{style.reset}",
        f"{style.dim}{session.slug}{style.reset}",
        f"{style.cyan}remaining {minutes:02d}:{seconds:02d}{style.reset}",
    ]
    if session.question:
        header.append(f"{style.dim}{session.question}{style.reset}")
    body = [
        render_order_book(label="UP / YES", book=up_book, depth=args.depth, style=style),
        "",
        render_order_book(label="DOWN / NO", book=down_book, depth=args.depth, style=style),
    ]
    return "\n".join(header + [""] + body)


async def _run(args: argparse.Namespace) -> int:
    style = DisplayStyle(use_color(no_color_flag=bool(args.no_color)))
    session = await _resolve_session(args)

    while True:
        rendered = _render_snapshot(session=session, args=args, style=style)
        if rendered:
            print(rendered, flush=True)
        if not args.watch:
            return 0
        if datetime.now(tz=UTC) >= session.end_time:
            try:
                session = await _resolve_session(args)
                continue
            except RuntimeError:
                await asyncio.sleep(max(0.1, args.interval))
                continue
        time.sleep(max(0.1, args.interval))


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
