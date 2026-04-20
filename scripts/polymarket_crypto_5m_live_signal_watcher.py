from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from scripts.polymarket_btc_hourly_watch import DEFAULT_GAMMA_HOST, gamma_market_snapshot
from scripts.polymarket_crypto_5m_probs_ws import (
    DEFAULT_WSS_MARKET,
    SUPPORTED_ASSETS,
    current_crypto_5m_market_slug,
    market_end_from_slug,
)


DEFAULT_SIGNAL_PATH = "/data/nautilus_export/live_signals/polymarket_5m_live_signals.jsonl"


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


def _pm_instrument_id_str(*, asset: str, token_id: str, label: str) -> str:
    symbol = f"PM-{asset.upper()}-5M-{label.upper()}-{token_id}".replace(".", "_")
    return f"{symbol}.POLYMARKET"


def _parse_best_bid_ask(message: dict[str, Any]) -> tuple[float | None, float | None]:
    def as_float(v: Any) -> float | None:
        if v in (None, ""):
            return None
        try:
            return float(v)
        except Exception:
            return None

    return (as_float(message.get("best_bid")), as_float(message.get("best_ask")))


@dataclass
class _RollingStats:
    window_seconds: float
    events: deque[tuple[int, float, float, int]]  # (ts_ns, bid, ask, changed01)
    last: tuple[float, float] | None = None

    def __init__(self, *, window_seconds: float) -> None:
        self.window_seconds = float(window_seconds)
        self.events = deque()
        self.last = None

    def _trim(self, *, now_ns: int) -> None:
        cutoff = now_ns - int(self.window_seconds * 1_000_000_000)
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def add(self, *, ts_ns: int, bid: float, ask: float) -> None:
        changed = 1 if (self.last is None or self.last != (bid, ask)) else 0
        self.last = (bid, ask)
        self.events.append((int(ts_ns), float(bid), float(ask), int(changed)))
        self._trim(now_ns=int(ts_ns))

    def snapshot(self, *, now_ns: int) -> dict[str, float]:
        self._trim(now_ns=int(now_ns))
        if not self.events:
            return {"mid_max": 0.0, "spread_max": 0.0, "updates_per_s": 0.0, "n": 0.0}

        mids = []
        spreads = []
        changes = 0
        for _, bid, ask, changed in self.events:
            mids.append((bid + ask) / 2.0)
            spreads.append(max(0.0, ask - bid))
            changes += int(changed)

        dur_s = max(1e-9, (self.events[-1][0] - self.events[0][0]) / 1e9)
        updates_per_s = float(changes) / float(self.window_seconds) if self.window_seconds > 0 else 0.0
        return {
            "mid_max": float(max(mids)),
            "spread_max": float(max(spreads)),
            "updates_per_s": float(updates_per_s),
            "n": float(len(self.events)),
            "dur_s": float(dur_s),
        }


def _append_jsonl(*, path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True))
        f.write("\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Watch live Polymarket 5m crypto markets and emit manipulation-like signals.")
    p.add_argument("--assets", default=os.environ.get("POLYMARKET_5M_ASSETS", "BTC,ETH,SOL,XRP,DOGE,BNB,HYPE"))
    p.add_argument("--gamma-host", default=os.environ.get("POLYMARKET_5M_GAMMA_HOST", DEFAULT_GAMMA_HOST))
    p.add_argument("--wss-url", default=os.environ.get("POLYMARKET_5M_WSS_URL", DEFAULT_WSS_MARKET))
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--reconnect-delay", type=float, default=float(os.environ.get("POLYMARKET_5M_RECONNECT_DELAY", "1.0")))
    p.add_argument("--window-seconds", type=float, default=float(os.environ.get("PM_LIVE_SIGNAL_WINDOW_SECONDS", "10")))
    p.add_argument("--mid-threshold", type=float, default=float(os.environ.get("PM_LIVE_SIGNAL_MID_THRESHOLD", "0.99")))
    p.add_argument("--spread-threshold", type=float, default=float(os.environ.get("PM_LIVE_SIGNAL_SPREAD_THRESHOLD", "0.50")))
    p.add_argument("--updates-per-s-threshold", type=float, default=float(os.environ.get("PM_LIVE_SIGNAL_UPDATES_PER_S", "10")))
    p.add_argument("--signal-path", default=os.environ.get("PM_LIVE_SIGNAL_PATH", DEFAULT_SIGNAL_PATH))
    p.add_argument("--emit-cooldown-seconds", type=float, default=float(os.environ.get("PM_LIVE_SIGNAL_COOLDOWN_SECONDS", "2")))
    p.add_argument("--max-seconds", type=float, default=0.0, help="For smoke tests: stop after N seconds (0 = run forever).")
    return p


def _parse_assets(value: str) -> tuple[str, ...]:
    assets = tuple(a.strip().upper() for a in str(value).split(",") if a.strip())
    for a in assets:
        if a not in SUPPORTED_ASSETS:
            raise SystemExit(f"unsupported asset {a!r} (supported: {', '.join(SUPPORTED_ASSETS)})")
    return assets


async def _watch_asset(
    *,
    asset: str,
    gamma_host: str,
    wss_url: str,
    timeout: float,
    reconnect_delay: float,
    window_seconds: float,
    mid_threshold: float,
    spread_threshold: float,
    updates_per_s_threshold: float,
    signal_path: Path,
    emit_cooldown_seconds: float,
    max_seconds: float,
) -> int:
    stats_by_token: dict[str, _RollingStats] = {}
    last_emit_at: dict[tuple[str, str], float] = {}
    started = time.monotonic()

    while True:
        if max_seconds and (time.monotonic() - started) >= float(max_seconds):
            return 0

        market_slug = current_crypto_5m_market_slug(asset=asset, now=datetime.now(timezone.utc))
        market_end = market_end_from_slug(market_slug)
        try:
            market = gamma_market_snapshot(gamma_host=gamma_host, slug=market_slug, timeout=timeout)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "pm_5m_live_gamma_error",
                        "asset": asset,
                        "market_slug": market_slug,
                        "gamma_host": gamma_host,
                        "reason": str(exc),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            await asyncio.sleep(max(0.25, float(reconnect_delay)))
            continue
        up_token = str(market["up"]["token_id"])
        down_token = str(market["down"]["token_id"])
        token_label = {up_token: "YES", down_token: "NO"}
        token_ids = [up_token, down_token]
        for tid in token_ids:
            stats_by_token.setdefault(tid, _RollingStats(window_seconds=window_seconds))

        subscribe = {"type": "market", "assets_ids": token_ids, "custom_feature_enabled": True}
        poll_timeout = min(1.0, float(window_seconds)) if float(window_seconds) > 0 else 1.0

        try:
            async with connect(wss_url, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(subscribe))
                while True:
                    now = datetime.now(timezone.utc)
                    if now >= market_end:
                        break
                    if max_seconds and (time.monotonic() - started) >= float(max_seconds):
                        return 0
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=poll_timeout)
                    except TimeoutError:
                        continue
                    except StopAsyncIteration:
                        break

                    payload = json.loads(raw)
                    messages = payload if isinstance(payload, list) else [payload]
                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        token_id = str(message.get("asset_id") or message.get("asset") or "").strip()
                        if token_id not in stats_by_token:
                            continue
                        bid, ask = _parse_best_bid_ask(message)
                        if bid is None or ask is None:
                            continue
                        ts_ns = _now_ns()
                        stats_by_token[token_id].add(ts_ns=ts_ns, bid=bid, ask=ask)

                        snap = stats_by_token[token_id].snapshot(now_ns=ts_ns)
                        if snap["n"] <= 0:
                            continue
                        remaining_ms = max(0.0, (market_end - now).total_seconds() * 1000.0)
                        is_risk = (
                            snap["mid_max"] >= float(mid_threshold)
                            and snap["spread_max"] >= float(spread_threshold)
                            and snap["updates_per_s"] >= float(updates_per_s_threshold)
                        )
                        if not is_risk:
                            continue
                        key = (market_slug, token_id)
                        last = last_emit_at.get(key, 0.0)
                        current = time.monotonic()
                        if current - last < float(emit_cooldown_seconds):
                            continue
                        last_emit_at[key] = current

                        event = {
                            "event": "pm_5m_live_risk_signal",
                            "asset": asset,
                            "market_slug": market_slug,
                            "side": token_label.get(token_id, "?"),
                            "token_id": token_id,
                            "instrument_id": _pm_instrument_id_str(asset=asset, token_id=token_id, label=token_label.get(token_id, "?")),
                            "window_seconds": float(window_seconds),
                            "mid_threshold": float(mid_threshold),
                            "spread_threshold": float(spread_threshold),
                            "updates_per_s_threshold": float(updates_per_s_threshold),
                            "mid_max": snap["mid_max"],
                            "spread_max": snap["spread_max"],
                            "updates_per_s": snap["updates_per_s"],
                            "remaining_ms": float(remaining_ms),
                            "ts_ns": int(ts_ns),
                        }
                        _append_jsonl(path=signal_path, payload=event)
                        print(json.dumps(event, sort_keys=True), flush=True)

        except ConnectionClosed as exc:
            print(
                json.dumps(
                    {
                        "event": "pm_5m_live_reconnect",
                        "asset": asset,
                        "market_slug": market_slug,
                        "reason": str(exc),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            await asyncio.sleep(float(reconnect_delay))
            continue

        await asyncio.sleep(0.25)


async def _run(args: argparse.Namespace) -> int:
    assets = _parse_assets(str(args.assets))
    signal_path = Path(str(args.signal_path))
    tasks = [
        asyncio.create_task(
            _watch_asset(
                asset=asset,
                gamma_host=str(args.gamma_host),
                wss_url=str(args.wss_url),
                timeout=float(args.timeout),
                reconnect_delay=float(args.reconnect_delay),
                window_seconds=float(args.window_seconds),
                mid_threshold=float(args.mid_threshold),
                spread_threshold=float(args.spread_threshold),
                updates_per_s_threshold=float(args.updates_per_s_threshold),
                signal_path=signal_path,
                emit_cooldown_seconds=float(args.emit_cooldown_seconds),
                max_seconds=float(args.max_seconds),
            )
        )
        for asset in assets
    ]
    await asyncio.gather(*tasks)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

