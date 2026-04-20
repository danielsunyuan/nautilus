from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq


DEFAULT_RESOLUTIONS_PATH = "/data/nautilus_catalog/metadata/polymarket_5m_resolutions.jsonl"
PRICE_SCALE = 10**16  # Nautilus fixed 16-byte encoding observed in parquet (e.g. 0.67 -> 6700000000000000)


@dataclass(frozen=True)
class ResolvedMarket:
    asset: str
    market_slug: str
    end_ns: int
    resolved_outcome: str
    up_token_id: str
    yes_instrument_id: str
    down_token_id: str
    no_instrument_id: str


def _parse_iso_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        # Gamma uses "...Z"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _instrument_id(*, asset: str, label: str, token_id: str) -> str:
    symbol = f"PM-{asset.upper()}-5M-{label.upper()}-{token_id}".replace(".", "_")
    return f"{symbol}.POLYMARKET"


def _load_resolved_markets(*, path: Path, hours: float) -> list[ResolvedMarket]:
    if not path.exists():
        raise SystemExit(f"missing resolutions file: {path}")
    cutoff = _now_utc() - timedelta(hours=max(0.0, float(hours)))

    out: list[ResolvedMarket] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue

            slug = str(row.get("market_slug") or row.get("slug") or "").strip()
            asset = str(row.get("asset") or "").strip().upper()
            resolved_outcome = row.get("resolved_outcome")
            end_dt = _parse_iso_dt(row.get("end_date"))

            if not slug or not asset or end_dt is None:
                continue
            if end_dt < cutoff:
                continue
            if resolved_outcome in (None, ""):
                # Not resolved yet.
                continue

            # Find token ids for the "Up" and "Down" outcomes.
            # NOTE: JSONL may store arrays as JSON strings.
            outcomes_raw = row.get("outcomes")
            token_ids_raw = row.get("clob_token_ids")
            try:
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
            except Exception:
                continue
            if not isinstance(outcomes, list) or not isinstance(token_ids, list):
                continue
            if len(outcomes) != len(token_ids):
                continue

            up_token_id: str | None = None
            down_token_id: str | None = None
            for idx, outcome in enumerate(outcomes):
                label = str(outcome).strip().lower()
                if label == "up":
                    up_token_id = str(token_ids[idx]).strip()
                elif label == "down":
                    down_token_id = str(token_ids[idx]).strip()
            if not up_token_id or not down_token_id:
                continue

            end_ns = int(end_dt.timestamp() * 1_000_000_000)
            out.append(
                ResolvedMarket(
                    asset=asset,
                    market_slug=slug,
                    end_ns=end_ns,
                    resolved_outcome=str(resolved_outcome).strip(),
                    up_token_id=up_token_id,
                    yes_instrument_id=_instrument_id(asset=asset, label="YES", token_id=up_token_id),
                    down_token_id=down_token_id,
                    no_instrument_id=_instrument_id(asset=asset, label="NO", token_id=down_token_id),
                )
            )

    # Keep the most recent record per slug (JSONL can contain updates).
    latest_by_slug: dict[str, ResolvedMarket] = {}
    for m in out:
        prev = latest_by_slug.get(m.market_slug)
        if prev is None or m.end_ns >= prev.end_ns:
            latest_by_slug[m.market_slug] = m
    return sorted(latest_by_slug.values(), key=lambda m: m.end_ns, reverse=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compute empirical P(YES) after hitting price thresholds near market end (calibration)."
    )
    p.add_argument("--catalog-path", default=os.environ.get("POLYMARKET_NAUTILUS_CATALOG", "/data/nautilus_catalog"))
    p.add_argument("--resolutions-path", default=os.environ.get("POLYMARKET_5M_RESOLUTIONS_PATH", DEFAULT_RESOLUTIONS_PATH))
    p.add_argument("--hours", type=float, default=72.0)
    p.add_argument("--limit-markets", type=int, default=500)
    p.add_argument(
        "--windows",
        default="60,30,10",
        help="Comma-separated window sizes in seconds before end_date.",
    )
    p.add_argument(
        "--thresholds",
        default="0.90,0.99",
        help="Comma-separated midprice thresholds (dollars). 0.90 = 90c.",
    )
    return p


def _parse_floats(value: str) -> list[float]:
    out: list[float] = []
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


def _parse_ints(value: str) -> list[int]:
    out: list[int] = []
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


def _decode_fixed16_to_float(b: bytes) -> float:
    # Nautilus stores these as fixed_size_binary[16]. In our dataset, the numeric value is the
    # little-endian signed int128 with scale 1e16, and the high 8 bytes are often zero.
    n = int.from_bytes(b, byteorder="little", signed=True)
    return n / PRICE_SCALE


def _iter_window_mids(*, instrument_dir: Path, start_ns: int, end_ns: int) -> Iterable[float]:
    if not instrument_dir.exists():
        return []
    files = sorted(instrument_dir.glob("*.parquet"))
    mids: list[float] = []
    for f in files:
        table = pq.read_table(f, columns=["bid_price", "ask_price", "ts_event"])
        rows = table.to_pylist()
        for r in rows:
            ts = int(r["ts_event"])
            if ts < start_ns or ts > end_ns:
                continue
            bid_b = r["bid_price"]
            ask_b = r["ask_price"]
            if not isinstance(bid_b, (bytes, bytearray)) or not isinstance(ask_b, (bytes, bytearray)):
                continue
            bid = _decode_fixed16_to_float(bytes(bid_b))
            ask = _decode_fixed16_to_float(bytes(ask_b))
            mids.append((bid + ask) / 2.0)
    return mids


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    windows = sorted(set(_parse_ints(args.windows)), reverse=True)
    thresholds = sorted(set(_parse_floats(args.thresholds)))

    markets = _load_resolved_markets(path=Path(str(args.resolutions_path)), hours=float(args.hours))
    if int(args.limit_markets) > 0:
        markets = markets[: int(args.limit_markets)]
    catalog_root = Path(str(args.catalog_path))
    quote_root = catalog_root / "data" / "quote_tick"

    # stats[side][window][threshold]
    # - markets: total markets considered
    # - hit: number of markets where that side hit threshold within window
    # - win_if_hit: number of markets where that side hit threshold and that side ultimately won
    # - false_spike: hit threshold but that side ultimately lost
    stats: dict[str, dict[int, dict[float, dict[str, float]]]] = {
        side: {w: {t: {"markets": 0.0, "hit": 0.0, "win_if_hit": 0.0, "false_spike": 0.0} for t in thresholds} for w in windows}
        for side in ("YES", "NO")
    }

    for m in markets:
        outcome = m.resolved_outcome.strip().lower()
        yes_won = outcome == "up"
        no_won = outcome == "down"
        instrument_dir_by_side = {
            "YES": quote_root / m.yes_instrument_id,
            "NO": quote_root / m.no_instrument_id,
        }

        for side in ("YES", "NO"):
            instrument_dir = instrument_dir_by_side[side]
            side_won = yes_won if side == "YES" else no_won
            for w in windows:
                start_ns = m.end_ns - int(w * 1_000_000_000)
                window_mids = list(_iter_window_mids(instrument_dir=instrument_dir, start_ns=start_ns, end_ns=m.end_ns))
                for t in thresholds:
                    cell = stats[side][w][t]
                    cell["markets"] += 1.0
                    hit = any(mid >= t for mid in window_mids) if window_mids else False
                    if not hit:
                        continue
                    cell["hit"] += 1.0
                    if side_won:
                        cell["win_if_hit"] += 1.0
                    else:
                        cell["false_spike"] += 1.0

    rows: list[dict[str, Any]] = []
    for side in ("YES", "NO"):
        for w in windows:
            for t in thresholds:
                cell = stats[side][w][t]
                markets_n = int(cell["markets"])
                hit_n = int(cell["hit"])
                win_hit_n = int(cell["win_if_hit"])
                false_n = int(cell["false_spike"])
                p_hit = (hit_n / markets_n) if markets_n else None
                p_win_given_hit = (win_hit_n / hit_n) if hit_n else None
                p_false_given_hit = (false_n / hit_n) if hit_n else None
                rows.append(
                    {
                        "side": side,
                        "window_seconds": w,
                        "threshold": t,
                        "markets": markets_n,
                        "hit": hit_n,
                        "p_hit": p_hit,
                        "p_win_given_hit": p_win_given_hit,
                        "false_spike": false_n,
                        "p_false_given_hit": p_false_given_hit,
                    }
                )

    print(
        json.dumps(
            {
                "event": "polymarket_5m_threshold_calibration",
                "catalog_path": str(args.catalog_path),
                "resolutions_path": str(args.resolutions_path),
                "hours": float(args.hours),
                "markets_considered": len(markets),
                "rows": rows,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

