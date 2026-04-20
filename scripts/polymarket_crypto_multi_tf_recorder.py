"""Lightweight poll-based recorder for Polymarket 15m and hourly crypto markets.

Polls Gamma API at regular intervals to capture top-of-book snapshots.
Writes to JSONL — no Nautilus/Parquet overhead, minimal storage footprint.

Designed to run alongside the full-granularity 5m WebSocket recorder.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

EASTERN_TZ = ZoneInfo("America/New_York")

SUPPORTED_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")

DEFAULT_GAMMA_HOST = "https://gamma-api.polymarket.com"
DEFAULT_WSS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

HOURLY_SLUG_PREFIX = {
    "BTC": "bitcoin-up-or-down",
    "ETH": "ethereum-up-or-down",
    "SOL": "solana-up-or-down",
    "XRP": "xrp-up-or-down",
    "DOGE": "dogecoin-up-or-down",
    "BNB": "bnb-up-or-down",
    "HYPE": "hype-up-or-down",
}


@dataclass(frozen=True)
class Timeframe:
    label: str
    duration_seconds: int
    poll_interval_seconds: float


TF_15M = Timeframe(label="15m", duration_seconds=900, poll_interval_seconds=10.0)
TF_1H = Timeframe(label="1h", duration_seconds=3600, poll_interval_seconds=30.0)


def _json_get(url: str, *, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def current_15m_slug(*, asset: str, now: datetime) -> str:
    epoch = int(now.astimezone(timezone.utc).timestamp())
    round_start = epoch - (epoch % 900)
    return f"{asset.lower()}-updown-15m-{round_start}"


def current_hourly_slug(*, asset: str, now: datetime) -> str:
    prefix = HOURLY_SLUG_PREFIX[asset.upper()]
    eastern = now.astimezone(EASTERN_TZ)
    hour = eastern.strftime("%-I%p").lower()
    month = eastern.strftime("%B").lower()
    day = str(eastern.day)
    year = str(eastern.year)
    return f"{prefix}-{month}-{day}-{year}-{hour}-et"


def market_end_15m(slug: str) -> datetime:
    start_epoch = int(slug.rsplit("-", 1)[1])
    return datetime.fromtimestamp(start_epoch, tz=timezone.utc) + timedelta(seconds=900)


def market_end_hourly(now: datetime) -> datetime:
    eastern = now.astimezone(EASTERN_TZ)
    hour_start = eastern.replace(minute=0, second=0, microsecond=0)
    return (hour_start + timedelta(hours=1)).astimezone(timezone.utc)


def gamma_snapshot(*, gamma_host: str, slug: str, timeout: float = 10.0) -> dict[str, Any] | None:
    url = f"{gamma_host.rstrip('/')}/markets/slug/{slug}"
    try:
        payload = _json_get(url, timeout=timeout)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    outcomes = payload.get("outcomes", [])
    prices = payload.get("outcomePrices", [])
    token_ids = payload.get("clobTokenIds", [])
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(prices, str):
        prices = json.loads(prices)
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)

    if len(outcomes) != len(prices) or len(outcomes) != len(token_ids):
        return None

    result: dict[str, Any] = {
        "slug": payload.get("slug"),
        "question": payload.get("question"),
        "active": payload.get("active"),
        "closed": payload.get("closed"),
    }
    for idx, outcome in enumerate(outcomes):
        label = str(outcome).strip().lower()
        result[f"{label}_price"] = float(prices[idx])
        result[f"{label}_token_id"] = str(token_ids[idx])

    return result


def clob_book_summary(
    *, token_id: str, wss_url: str = DEFAULT_WSS_MARKET, timeout: float = 5.0
) -> dict[str, float | None]:
    """Placeholder — use Gamma prices only. CLOB orderbook would need WebSocket."""
    return {"best_bid": None, "best_ask": None, "best_bid_size": None, "best_ask_size": None}


def _output_dir() -> Path:
    return Path(os.environ.get("POLYMARKET_MULTI_TF_OUTPUT", "/data/nautilus_export/multi_tf"))


def _output_path(*, timeframe: str, date_str: str) -> Path:
    d = _output_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"polymarket_{timeframe}_{date_str}.jsonl"


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True))
        f.write("\n")


def _log(event: str, **kwargs: Any) -> None:
    print(json.dumps({"event": event, **kwargs}, sort_keys=True), flush=True)


async def _poll_market_cycle(
    *,
    asset: str,
    timeframe: Timeframe,
    slug: str,
    market_end: datetime,
    gamma_host: str,
    timeout: float,
) -> list[dict[str, Any]]:
    """Poll one market from now until it ends. Returns list of snapshot records."""
    records: list[dict[str, Any]] = []
    poll_count = 0

    while True:
        now = datetime.now(timezone.utc)
        if now >= market_end:
            break

        snap = gamma_snapshot(gamma_host=gamma_host, slug=slug, timeout=timeout)
        if snap is not None:
            remaining_s = max(0, int((market_end - now).total_seconds()))
            record = {
                "ts": now.isoformat(),
                "asset": asset,
                "timeframe": timeframe.label,
                "slug": slug,
                "remaining_s": remaining_s,
                "up_price": snap.get("up_price"),
                "down_price": snap.get("down_price"),
                "active": snap.get("active"),
                "closed": snap.get("closed"),
            }
            records.append(record)

            date_str = now.strftime("%Y%m%d")
            _append_jsonl(_output_path(timeframe=timeframe.label, date_str=date_str), record)
            poll_count += 1

        await asyncio.sleep(timeframe.poll_interval_seconds)

    snap_final = gamma_snapshot(gamma_host=gamma_host, slug=slug, timeout=timeout)
    now = datetime.now(timezone.utc)
    resolution = {
        "ts": now.isoformat(),
        "asset": asset,
        "timeframe": timeframe.label,
        "slug": slug,
        "remaining_s": 0,
        "up_price": snap_final.get("up_price") if snap_final else None,
        "down_price": snap_final.get("down_price") if snap_final else None,
        "active": snap_final.get("active") if snap_final else None,
        "closed": snap_final.get("closed") if snap_final else None,
        "is_resolution": True,
    }
    records.append(resolution)
    date_str = now.strftime("%Y%m%d")
    _append_jsonl(_output_path(timeframe=timeframe.label, date_str=date_str), resolution)

    _log(
        "market_cycle_complete",
        asset=asset,
        timeframe=timeframe.label,
        slug=slug,
        snapshots=poll_count + 1,
    )
    return records


async def _run_asset_tf_loop(
    *,
    asset: str,
    timeframe: Timeframe,
    gamma_host: str,
    timeout: float,
) -> None:
    """Infinite loop: discover current market, poll it, move to next."""
    while True:
        now = datetime.now(timezone.utc)

        if timeframe.label == "15m":
            slug = current_15m_slug(asset=asset, now=now)
            end = market_end_15m(slug)
        elif timeframe.label == "1h":
            slug = current_hourly_slug(asset=asset, now=now)
            end = market_end_hourly(now)
        else:
            raise ValueError(f"unsupported timeframe: {timeframe.label}")

        if datetime.now(timezone.utc) >= end:
            await asyncio.sleep(1.0)
            continue

        _log("market_cycle_start", asset=asset, timeframe=timeframe.label, slug=slug)

        try:
            await _poll_market_cycle(
                asset=asset,
                timeframe=timeframe,
                slug=slug,
                market_end=end,
                gamma_host=gamma_host,
                timeout=timeout,
            )
        except Exception as exc:
            _log(
                "market_cycle_error",
                asset=asset,
                timeframe=timeframe.label,
                slug=slug,
                error=f"{type(exc).__name__}: {exc}",
            )
            await asyncio.sleep(5.0)
            continue

        await asyncio.sleep(2.0)


async def _run(
    *,
    assets: tuple[str, ...],
    timeframes: tuple[Timeframe, ...],
    gamma_host: str,
    timeout: float,
) -> None:
    tasks = [
        asyncio.create_task(
            _run_asset_tf_loop(
                asset=asset,
                timeframe=tf,
                gamma_host=gamma_host,
                timeout=timeout,
            )
        )
        for asset in assets
        for tf in timeframes
    ]
    _log(
        "recorder_started",
        assets=list(assets),
        timeframes=[tf.label for tf in timeframes],
        task_count=len(tasks),
    )
    await asyncio.gather(*tasks)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Lightweight poll-based recorder for Polymarket 15m and hourly crypto markets."
    )
    p.add_argument(
        "--assets",
        default=os.environ.get("POLYMARKET_MULTI_TF_ASSETS", "BTC,ETH,SOL"),
        help="Comma-separated asset list (default: BTC,ETH,SOL)",
    )
    p.add_argument(
        "--timeframes",
        default=os.environ.get("POLYMARKET_MULTI_TF_TIMEFRAMES", "15m,1h"),
        help="Comma-separated timeframes to record (default: 15m,1h)",
    )
    p.add_argument(
        "--gamma-host",
        default=os.environ.get("POLYMARKET_5M_GAMMA_HOST", DEFAULT_GAMMA_HOST),
    )
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument(
        "--poll-15m",
        type=float,
        default=float(os.environ.get("POLYMARKET_MULTI_TF_POLL_15M", "10")),
        help="Poll interval in seconds for 15m markets (default: 10)",
    )
    p.add_argument(
        "--poll-1h",
        type=float,
        default=float(os.environ.get("POLYMARKET_MULTI_TF_POLL_1H", "30")),
        help="Poll interval in seconds for hourly markets (default: 30)",
    )

    args = p.parse_args(argv)
    assets = tuple(a.strip().upper() for a in str(args.assets).split(",") if a.strip())
    for a in assets:
        if a not in SUPPORTED_ASSETS:
            raise SystemExit(f"unsupported asset {a!r}")
        if a not in HOURLY_SLUG_PREFIX:
            raise SystemExit(f"no hourly slug prefix for {a!r}")

    tf_map = {
        "15m": Timeframe(label="15m", duration_seconds=900, poll_interval_seconds=args.poll_15m),
        "1h": Timeframe(label="1h", duration_seconds=3600, poll_interval_seconds=args.poll_1h),
    }
    requested_tfs = tuple(
        tf_map[t.strip().lower()]
        for t in str(args.timeframes).split(",")
        if t.strip().lower() in tf_map
    )
    if not requested_tfs:
        raise SystemExit("no valid timeframes specified")

    asyncio.run(_run(assets=assets, timeframes=requested_tfs, gamma_host=args.gamma_host, timeout=args.timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
