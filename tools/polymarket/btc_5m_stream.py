#!/usr/bin/env python3
"""Fast compact stream of BTC 5m Polymarket top-of-book (coloured)."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))

import argparse
import asyncio
import time
from datetime import UTC, datetime

from nautilus_trader.adapters.polymarket.common.crypto_5m import (
    current_crypto_5m_market_slug,
    resolve_crypto_5m_session,
)
from nautilus_trader.core.nautilus_pyo3 import HttpClient

from tools.polymarket.display_common import (
    DEFAULT_CLOB_HOST,
    DEFAULT_GAMMA_BASE_URL,
    DisplayStyle,
    clear_fast_stream,
    fetch_order_books_parallel,
    render_fast_books,
    use_color,
    write_fast_stream,
)

FAST_LINE_COUNT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stream BTC 5m top-of-book from the CLOB.")
    parser.add_argument("--asset", default="BTC")
    parser.add_argument("--market-slug", default="")
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL)
    parser.add_argument("--clob-host", default=DEFAULT_CLOB_HOST)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--no-color", action="store_true")
    return parser


def _needs_roll(*, session, asset: str, now: datetime, pinned_slug: bool) -> bool:
    if pinned_slug:
        return now >= session.end_time
    expected = current_crypto_5m_market_slug(asset=asset, now=now)
    return now >= session.end_time or session.slug != expected


async def _resolve_session(args: argparse.Namespace, http_client: HttpClient):
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


async def _wait_for_next_session(
    *,
    args: argparse.Namespace,
    http_client: HttpClient,
    previous_slug: str,
    style: DisplayStyle,
) -> object:
    pinned_slug = bool(args.market_slug.strip())
    deadline = time.monotonic() + 120.0

    while time.monotonic() < deadline:
        now = datetime.now(tz=UTC)
        try:
            session = await _resolve_session(args, http_client)
        except RuntimeError:
            session = None

        if session is not None and (
            pinned_slug
            or session.slug != previous_slug
            or session.end_time > now
        ):
            try:
                fetch_order_books_parallel(
                    token_ids=(session.token_ids["up"], session.token_ids["down"]),
                    clob_host=args.clob_host,
                    retries=2,
                    retry_delay=0.15,
                )
                return session
            except RuntimeError:
                pass

        waiting = (
            f"{style.dim}waiting for next {args.asset} 5m round"
            f" (after {previous_slug}){style.reset}"
        )
        write_fast_stream(waiting, line_count=1, first=True, reset=True)
        await asyncio.sleep(0.5)

    raise RuntimeError(f"timed out waiting for next market after {previous_slug}")


def _fetch_books_with_roll_hint(*, session, args: argparse.Namespace):
    try:
        return fetch_order_books_parallel(
            token_ids=(session.token_ids["up"], session.token_ids["down"]),
            clob_host=args.clob_host,
        )
    except RuntimeError as exc:
        message = str(exc).lower()
        if "no clob order book" in message or "request exception" in message:
            raise RuntimeError("books_unavailable") from exc
        raise


async def _run(args: argparse.Namespace) -> int:
    style = DisplayStyle(use_color(no_color_flag=bool(args.no_color)))
    http_client = HttpClient(timeout_secs=max(1, int(args.timeout)))
    pinned_slug = bool(args.market_slug.strip())
    session = await _resolve_session(args, http_client)
    first = True
    reset = False

    while True:
        now = datetime.now(tz=UTC)
        if _needs_roll(session=session, asset=args.asset, now=now, pinned_slug=pinned_slug):
            previous_slug = session.slug
            session = await _wait_for_next_session(
                args=args,
                http_client=http_client,
                previous_slug=previous_slug,
                style=style,
            )
            first = True
            reset = True

        try:
            up_book, down_book = _fetch_books_with_roll_hint(session=session, args=args)
        except RuntimeError as exc:
            if str(exc) == "books_unavailable" and _needs_roll(
                session=session,
                asset=args.asset,
                now=datetime.now(tz=UTC),
                pinned_slug=pinned_slug,
            ):
                session = await _wait_for_next_session(
                    args=args,
                    http_client=http_client,
                    previous_slug=session.slug,
                    style=style,
                )
                first = True
                reset = True
                continue
            if str(exc) == "books_unavailable":
                await asyncio.sleep(max(0.05, args.interval))
                continue
            raise

        remaining = max(0, int((session.end_time - now).total_seconds()))
        rendered = render_fast_books(
            slug=session.slug,
            remaining_seconds=remaining,
            up_book=up_book,
            down_book=down_book,
            style=style,
        )
        write_fast_stream(rendered, line_count=FAST_LINE_COUNT, first=first, reset=reset)
        first = False
        reset = False
        await asyncio.sleep(max(0.05, args.interval))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
