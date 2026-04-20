from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

from scripts.polymarket_crypto_5m_nautilus_record import _extract_resolution, _gamma_market_raw, write_market_resolution
from scripts.polymarket_crypto_5m_probs_ws import SUPPORTED_ASSETS, current_crypto_5m_market_slug


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Backfill/update Polymarket 5m market resolution metadata by re-querying Gamma for recent slugs."
    )
    p.add_argument("--assets", default=os.environ.get("POLYMARKET_5M_ASSETS", "BTC,ETH,SOL,XRP,DOGE,BNB,HYPE"))
    p.add_argument("--hours", type=float, default=float(os.environ.get("POLYMARKET_5M_BACKFILL_HOURS", "24")))
    p.add_argument("--gamma-host", default=os.environ.get("POLYMARKET_5M_GAMMA_HOST", "https://gamma-api.polymarket.com"))
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--step-seconds", type=int, default=300, help="Market cadence (default 300 seconds).")
    return p


def _parse_assets(value: str) -> tuple[str, ...]:
    assets = tuple(a.strip().upper() for a in str(value).split(",") if a.strip())
    for a in assets:
        if a not in SUPPORTED_ASSETS:
            raise SystemExit(f"unsupported asset {a!r} (supported: {', '.join(SUPPORTED_ASSETS)})")
    return assets


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    assets = _parse_assets(str(args.assets))
    now = datetime.now(timezone.utc)
    hours = max(0.0, float(args.hours))
    step = max(60, int(args.step_seconds))
    start = now - timedelta(hours=hours)

    wrote = 0
    checked = 0

    t = start
    while t <= now:
        for asset in assets:
            slug = current_crypto_5m_market_slug(asset=asset, now=t)
            checked += 1
            try:
                raw = _gamma_market_raw(gamma_host=str(args.gamma_host), slug=slug, timeout=float(args.timeout))
            except Exception:
                continue
            extracted = _extract_resolution(raw)
            if extracted.get("slug") != slug:
                continue
            if extracted.get("resolved_outcome") is None and extracted.get("closed") is not True:
                continue
            if write_market_resolution(
                asset=asset,
                market_slug=slug,
                gamma_host=str(args.gamma_host),
                timeout=float(args.timeout),
                up_token_id="unknown",
                down_token_id="unknown",
            ):
                wrote += 1
        t += timedelta(seconds=step)

    print({"event": "polymarket_5m_backfill_done", "checked": checked, "wrote": wrote})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

