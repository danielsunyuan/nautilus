from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from websockets.asyncio.client import connect

from scripts.polymarket_btc_hourly_watch import DEFAULT_GAMMA_HOST, gamma_market_snapshot


SUPPORTED_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
DEFAULT_WSS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class _Style:
    def __init__(self, enabled: bool) -> None:
        self.reset = "\033[0m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.green = "\033[92m" if enabled else ""
        self.red = "\033[91m" if enabled else ""
        self.cyan = "\033[96m" if enabled else ""


def _use_color(*, no_color_flag: bool) -> bool:
    if no_color_flag or os.environ.get("NO_COLOR", "").strip():
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def current_crypto_5m_market_slug(*, asset: str, now: datetime) -> str:
    symbol = asset.strip().upper()
    if symbol not in SUPPORTED_ASSETS:
        raise ValueError(f"unsupported asset {asset!r}")
    epoch = int(now.astimezone(timezone.utc).timestamp())
    round_start = epoch - (epoch % 300)
    return f"{symbol.lower()}-updown-5m-{round_start}"


def market_end_from_slug(slug: str) -> datetime:
    start_epoch = int(str(slug).rsplit("-", 1)[1])
    return datetime.fromtimestamp(start_epoch, tz=timezone.utc) + timedelta(minutes=5)


def apply_market_message(*, state: dict[str, dict[str, Any]], message: dict[str, Any]) -> None:
    asset_id = str(message.get("asset_id") or message.get("asset") or "")
    if asset_id not in state:
        return
    row = state[asset_id]
    if message.get("event_type") == "book":
        bids = message.get("bids") or []
        asks = message.get("asks") or []
        bid_prices = [float(level["price"]) for level in bids if level.get("price") not in (None, "")]
        ask_prices = [float(level["price"]) for level in asks if level.get("price") not in (None, "")]
        row["best_bid"] = max(bid_prices) if bid_prices else row.get("best_bid")
        row["best_ask"] = min(ask_prices) if ask_prices else row.get("best_ask")
        return
    best_bid = message.get("best_bid")
    best_ask = message.get("best_ask")
    if best_bid not in (None, ""):
        row["best_bid"] = float(best_bid)
    if best_ask not in (None, ""):
        row["best_ask"] = float(best_ask)


def render_state(
    *,
    asset: str,
    market_slug: str,
    state: dict[str, dict[str, Any]],
    now: datetime,
    use_color: bool,
) -> str:
    style = _Style(use_color)
    remaining = max(0, int((market_end_from_slug(market_slug) - now.astimezone(timezone.utc)).total_seconds()))
    minutes, seconds = divmod(remaining, 60)
    rows = sorted(state.values(), key=lambda row: 0 if row["label"] == "YES" else 1)
    yes, no = rows
    return "\n".join(
        [
            f"{style.bold}{asset} 5m{style.reset}  {style.dim}{market_slug}{style.reset}",
            f"{style.cyan}remaining {minutes:02d}:{seconds:02d}{style.reset}",
            f"{style.green}YES{style.reset}  {style.dim}bid {yes.get('best_bid')} ask {yes.get('best_ask')}{style.reset}",
            f"{style.red}NO{style.reset}   {style.dim}bid {no.get('best_bid')} ask {no.get('best_ask')}{style.reset}",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Watch the current 5-minute Polymarket market over the official CLOB WebSocket."
    )
    parser.add_argument("--asset", choices=SUPPORTED_ASSETS, default="BTC")
    parser.add_argument("--market-slug", default="")
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_HOST)
    parser.add_argument("--wss-url", default=DEFAULT_WSS_MARKET)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--no-color", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    asset = str(args.asset).upper()
    market_slug = args.market_slug.strip() or current_crypto_5m_market_slug(asset=asset, now=datetime.now(timezone.utc))
    market = gamma_market_snapshot(gamma_host=args.gamma_host, slug=market_slug, timeout=args.timeout)
    state = {
        str(market["up"]["token_id"]): {"label": "YES", "best_bid": None, "best_ask": None},
        str(market["down"]["token_id"]): {"label": "NO", "best_bid": None, "best_ask": None},
    }
    use_color = _use_color(no_color_flag=bool(args.no_color))
    subscribe = {
        "type": "market",
        "assets_ids": list(state.keys()),
        "custom_feature_enabled": True,
    }
    async with connect(args.wss_url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps(subscribe))
        print(render_state(asset=asset, market_slug=market_slug, state=state, now=datetime.now(timezone.utc), use_color=use_color), flush=True)
        async for raw in ws:
            now = datetime.now(timezone.utc)
            if now >= market_end_from_slug(market_slug):
                print(f"{market_slug} finished", flush=True)
                return 0
            payload = json.loads(raw)
            messages = payload if isinstance(payload, list) else [payload]
            changed = False
            for message in messages:
                if not isinstance(message, dict):
                    continue
                before = json.dumps(state, sort_keys=True)
                apply_market_message(state=state, message=message)
                if json.dumps(state, sort_keys=True) != before:
                    changed = True
            if changed:
                print(render_state(asset=asset, market_slug=market_slug, state=state, now=now, use_color=use_color), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
