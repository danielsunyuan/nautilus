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
DEFAULT_OUT_DIR = "/data/nautilus_export/false_spikes"
PRICE_SCALE = 10**16  # fixed_size_binary[16] -> little-endian signed int128 scaled by 1e16


@dataclass(frozen=True)
class MarketRow:
    asset: str
    market_slug: str
    end_ns: int
    end_date: str
    resolved_outcome: str  # "Up" or "Down"
    up_token_id: str
    down_token_id: str
    yes_instrument_id: str
    no_instrument_id: str


def _instrument_id(*, asset: str, label: str, token_id: str) -> str:
    symbol = f"PM-{asset.upper()}-5M-{label.upper()}-{token_id}".replace(".", "_")
    return f"{symbol}.POLYMARKET"


def _parse_iso_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
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


def _decode_fixed16_to_float(b: bytes) -> float:
    n = int.from_bytes(b, byteorder="little", signed=True)
    return n / PRICE_SCALE


def _iter_window_mids_with_ts(*, instrument_dir: Path, start_ns: int, end_ns: int) -> Iterable[tuple[int, float]]:
    if not instrument_dir.exists():
        return []
    files = sorted(instrument_dir.glob("*.parquet"))
    out: list[tuple[int, float]] = []
    for f in files:
        table = pq.read_table(f, columns=["bid_price", "ask_price", "ts_event"])
        for r in table.to_pylist():
            ts = int(r["ts_event"])
            if ts < start_ns or ts > end_ns:
                continue
            bid_b = r["bid_price"]
            ask_b = r["ask_price"]
            if not isinstance(bid_b, (bytes, bytearray)) or not isinstance(ask_b, (bytes, bytearray)):
                continue
            bid = _decode_fixed16_to_float(bytes(bid_b))
            ask = _decode_fixed16_to_float(bytes(ask_b))
            out.append((ts, (bid + ask) / 2.0))
    out.sort(key=lambda x: x[0])
    return out


def _segments_above_threshold(
    *, series: list[tuple[int, float]], threshold: float, max_gap_ns: int = 1_500_000_000
) -> list[list[tuple[int, float]]]:
    segs: list[list[tuple[int, float]]] = []
    cur: list[tuple[int, float]] = []
    for ts, mid in series:
        if mid >= threshold:
            if not cur:
                cur = [(ts, mid)]
                continue
            if ts - cur[-1][0] <= max_gap_ns:
                cur.append((ts, mid))
                continue
            segs.append(cur)
            cur = [(ts, mid)]
        else:
            if cur:
                segs.append(cur)
                cur = []
    if cur:
        segs.append(cur)
    return segs


def _flip_time_yes_over_no(
    *, yes: list[tuple[int, float]], no: list[tuple[int, float]], max_pair_gap_ns: int = 250_000_000
) -> dict[str, Any] | None:
    """
    Return first timestamp where YES mid > NO mid (paired by nearest timestamp).
    """
    if not yes or not no:
        return None
    yes.sort()
    no.sort()
    j = 0
    for ts_y, mid_y in yes:
        # advance no pointer
        while j + 1 < len(no) and no[j + 1][0] <= ts_y:
            j += 1
        candidates = [no[j]]
        if j + 1 < len(no):
            candidates.append(no[j + 1])
        ts_n, mid_n = min(candidates, key=lambda x: abs(x[0] - ts_y))
        if abs(ts_n - ts_y) > max_pair_gap_ns:
            continue
        if mid_y > mid_n:
            return {
                "ts_ns": int(ts_y),
                "yes_mid": float(mid_y),
                "no_mid": float(mid_n),
                "pair_gap_ms": float(abs(ts_n - ts_y)) / 1e6,
            }
    return None


def _load_markets(*, path: Path, hours: float) -> list[MarketRow]:
    if not path.exists():
        raise SystemExit(f"missing resolutions file: {path}")
    cutoff = _now_utc() - timedelta(hours=max(0.0, float(hours)))

    rows: list[MarketRow] = []
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
            end_date_raw = row.get("end_date")
            end_dt = _parse_iso_dt(end_date_raw)

            if not slug or not asset or end_dt is None:
                continue
            if end_dt < cutoff:
                continue
            if resolved_outcome in (None, ""):
                continue

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
            rows.append(
                MarketRow(
                    asset=asset,
                    market_slug=slug,
                    end_ns=end_ns,
                    end_date=str(end_date_raw),
                    resolved_outcome=str(resolved_outcome).strip(),
                    up_token_id=up_token_id,
                    down_token_id=down_token_id,
                    yes_instrument_id=_instrument_id(asset=asset, label="YES", token_id=up_token_id),
                    no_instrument_id=_instrument_id(asset=asset, label="NO", token_id=down_token_id),
                )
            )

    # Keep latest row per slug (jsonl can have updates).
    latest: dict[str, MarketRow] = {}
    for r in rows:
        prev = latest.get(r.market_slug)
        if prev is None or r.end_ns >= prev.end_ns:
            latest[r.market_slug] = r
    return sorted(latest.values(), key=lambda r: r.end_ns, reverse=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Report NO-side false spikes near expiry.")
    p.add_argument("--catalog-path", default=os.environ.get("POLYMARKET_NAUTILUS_CATALOG", "/data/nautilus_catalog"))
    p.add_argument("--resolutions-path", default=os.environ.get("POLYMARKET_5M_RESOLUTIONS_PATH", DEFAULT_RESOLUTIONS_PATH))
    p.add_argument("--hours", type=float, default=6.0)
    p.add_argument("--limit-markets", type=int, default=500)
    p.add_argument("--window-seconds", type=int, default=60)
    p.add_argument("--threshold", type=float, default=0.90)
    p.add_argument("--max-results", type=int, default=50)
    p.add_argument("--out-dir", default=os.environ.get("POLYMARKET_FALSE_SPIKES_OUT_DIR", DEFAULT_OUT_DIR))
    p.add_argument("--dump-series", action="store_true", help="Write per-market JSONL of (ts_ns, mid) for YES/NO.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    markets = _load_markets(path=Path(str(args.resolutions_path)), hours=float(args.hours))
    if int(args.limit_markets) > 0:
        markets = markets[: int(args.limit_markets)]

    catalog_root = Path(str(args.catalog_path))
    quote_root = catalog_root / "data" / "quote_tick"
    window_ns = int(max(1, int(args.window_seconds)) * 1_000_000_000)
    threshold = float(args.threshold)

    flagged: list[dict[str, Any]] = []
    by_end_date: dict[str, int] = {}
    out_dir = Path(str(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    for m in markets:
        # NO-side false spike means: NO mid >= threshold near end, but market resolved "Up" (so NO lost).
        if m.resolved_outcome.strip().lower() != "up":
            continue

        start_ns = m.end_ns - window_ns
        no_dir = quote_root / m.no_instrument_id
        no_series = list(_iter_window_mids_with_ts(instrument_dir=no_dir, start_ns=start_ns, end_ns=m.end_ns))
        if not no_series:
            continue
        yes_dir = quote_root / m.yes_instrument_id
        yes_series = list(_iter_window_mids_with_ts(instrument_dir=yes_dir, start_ns=start_ns, end_ns=m.end_ns))

        hit_points = [(ts, mid) for (ts, mid) in no_series if mid >= threshold]
        if not hit_points:
            continue

        first_hit_ts, first_hit_mid = hit_points[0]
        max_mid = max(mid for _, mid in no_series)
        segs = _segments_above_threshold(series=no_series, threshold=threshold)
        longest = max(segs, key=lambda s: (s[-1][0] - s[0][0], len(s))) if segs else []
        longest_dur_s = float(longest[-1][0] - longest[0][0]) / 1e9 if longest else 0.0
        flip = _flip_time_yes_over_no(yes=yes_series, no=no_series)
        ms_to_end_at_flip = (float(m.end_ns - int(flip["ts_ns"])) / 1e6) if flip else None

        row = {
            "market_slug": m.market_slug,
            "asset": m.asset,
            "end_date": m.end_date,
            "resolved_outcome": m.resolved_outcome,
            "window_seconds": int(args.window_seconds),
            "threshold": threshold,
            "no_first_hit_ts_ns": int(first_hit_ts),
            "no_first_hit_mid": float(first_hit_mid),
            "no_max_mid_in_window": float(max_mid),
            "no_longest_above_threshold_s": longest_dur_s,
            "no_segments_above_threshold": len(segs),
            "flip_yes_over_no": flip,
            "ms_to_end_at_flip": ms_to_end_at_flip,
            "yes_instrument_id": m.yes_instrument_id,
            "no_instrument_id": m.no_instrument_id,
        }
        flagged.append(row)
        by_end_date[str(m.end_date)] = by_end_date.get(str(m.end_date), 0) + 1

        if args.dump_series:
            dump_path = out_dir / f"{m.market_slug}__w{int(args.window_seconds)}__t{threshold:.2f}.jsonl"
            with dump_path.open("w", encoding="utf-8") as f:
                for ts, mid in yes_series:
                    f.write(json.dumps({"side": "YES", "ts_ns": int(ts), "mid": float(mid)}, sort_keys=True))
                    f.write("\n")
                for ts, mid in no_series:
                    f.write(json.dumps({"side": "NO", "ts_ns": int(ts), "mid": float(mid)}, sort_keys=True))
                    f.write("\n")

        if int(args.max_results) and len(flagged) >= int(args.max_results):
            break

    print(
        json.dumps(
            {
                "event": "polymarket_5m_false_spike_report",
                "hours": float(args.hours),
                "limit_markets": int(args.limit_markets),
                "window_seconds": int(args.window_seconds),
                "threshold": float(args.threshold),
                "flagged": len(flagged),
                "flagged_by_end_date": dict(sorted(by_end_date.items(), key=lambda kv: (-kv[1], kv[0]))),
                "rows": flagged,
                "out_dir": str(out_dir),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

