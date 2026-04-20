from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


DEFAULT_RESOLUTIONS_PATH = "/data/nautilus_catalog/metadata/polymarket_5m_resolutions.jsonl"
PRICE_SCALE = 10**16  # fixed_size_binary[16] -> little-endian signed int128 scaled by 1e16


@dataclass(frozen=True)
class Market:
    asset: str
    market_slug: str
    end_ns: int
    end_date: str
    resolved_outcome: str  # "Up" or "Down"
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


def _read_mid_series(*, instrument_dir: Path, start_ns: int, end_ns: int) -> list[tuple[int, float]]:
    if not instrument_dir.exists():
        return []
    series: list[tuple[int, float]] = []
    for f in sorted(instrument_dir.glob("*.parquet")):
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
            series.append((ts, (bid + ask) / 2.0))
    series.sort(key=lambda x: x[0])
    return series


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
) -> int | None:
    if not yes or not no:
        return None
    yes = sorted(yes)
    no = sorted(no)
    j = 0
    for ts_y, mid_y in yes:
        while j + 1 < len(no) and no[j + 1][0] <= ts_y:
            j += 1
        candidates = [no[j]]
        if j + 1 < len(no):
            candidates.append(no[j + 1])
        ts_n, mid_n = min(candidates, key=lambda x: abs(x[0] - ts_y))
        if abs(ts_n - ts_y) > max_pair_gap_ns:
            continue
        if mid_y > mid_n:
            return ts_y
    return None


def _load_markets(*, path: Path, hours: float) -> list[Market]:
    if not path.exists():
        raise SystemExit(f"missing resolutions file: {path}")
    cutoff = _now_utc() - timedelta(hours=max(0.0, float(hours)))

    rows: list[Market] = []
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
                Market(
                    asset=asset,
                    market_slug=slug,
                    end_ns=end_ns,
                    end_date=str(end_date_raw),
                    resolved_outcome=str(resolved_outcome).strip(),
                    yes_instrument_id=_instrument_id(asset=asset, label="YES", token_id=up_token_id),
                    no_instrument_id=_instrument_id(asset=asset, label="NO", token_id=down_token_id),
                )
            )

    latest: dict[str, Market] = {}
    for r in rows:
        prev = latest.get(r.market_slug)
        if prev is None or r.end_ns >= prev.end_ns:
            latest[r.market_slug] = r
    return sorted(latest.values(), key=lambda r: r.end_ns, reverse=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sweep NO-side false spikes across thresholds/windows.")
    p.add_argument("--catalog-path", default=os.environ.get("POLYMARKET_NAUTILUS_CATALOG", "/data/nautilus_catalog"))
    p.add_argument("--resolutions-path", default=os.environ.get("POLYMARKET_5M_RESOLUTIONS_PATH", DEFAULT_RESOLUTIONS_PATH))
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--limit-markets", type=int, default=1000)
    p.add_argument("--windows", default="60,30,10")
    p.add_argument("--thresholds", default="0.90,0.99")
    return p


def _parse_ints(value: str) -> list[int]:
    return [int(x.strip()) for x in str(value).split(",") if x.strip()]


def _parse_floats(value: str) -> list[float]:
    return [float(x.strip()) for x in str(value).split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    windows = sorted(set(_parse_ints(args.windows)), reverse=True)
    thresholds = sorted(set(_parse_floats(args.thresholds)))
    max_window = max(windows) if windows else 60

    markets = _load_markets(path=Path(str(args.resolutions_path)), hours=float(args.hours))
    if int(args.limit_markets) > 0:
        markets = markets[: int(args.limit_markets)]

    catalog_root = Path(str(args.catalog_path))
    quote_root = catalog_root / "data" / "quote_tick"

    # summary[window][threshold]
    summary: dict[int, dict[float, dict[str, Any]]] = {
        w: {t: {"markets": 0, "false_spikes": 0, "by_end_date": {}, "flip_ms_buckets": {}} for t in thresholds}
        for w in windows
    }
    buckets_ms = [250, 500, 1000, 1500, 2000, 3000]

    for m in markets:
        # NO-side false spike: NO hits threshold but NO loses => market resolved Up.
        if m.resolved_outcome.strip().lower() != "up":
            continue

        start_ns_full = m.end_ns - int(max_window * 1_000_000_000)
        yes = _read_mid_series(instrument_dir=quote_root / m.yes_instrument_id, start_ns=start_ns_full, end_ns=m.end_ns)
        no = _read_mid_series(instrument_dir=quote_root / m.no_instrument_id, start_ns=start_ns_full, end_ns=m.end_ns)
        if not yes or not no:
            continue

        for w in windows:
            start_ns = m.end_ns - int(w * 1_000_000_000)
            yes_w = [(ts, mid) for ts, mid in yes if ts >= start_ns]
            no_w = [(ts, mid) for ts, mid in no if ts >= start_ns]
            for t in thresholds:
                cell = summary[w][t]
                cell["markets"] += 1
                if not any(mid >= t for _, mid in no_w):
                    continue
                # false spike confirmed
                cell["false_spikes"] += 1
                cell["by_end_date"][m.end_date] = cell["by_end_date"].get(m.end_date, 0) + 1

                flip_ts = _flip_time_yes_over_no(yes=yes_w, no=no_w)
                if flip_ts is None:
                    bucket = "no_flip"
                else:
                    ms_to_end = (m.end_ns - flip_ts) / 1e6
                    bucket = None
                    for b in buckets_ms:
                        if ms_to_end <= b:
                            bucket = f"<= {b}ms"
                            break
                    if bucket is None:
                        bucket = f"> {buckets_ms[-1]}ms"
                cell["flip_ms_buckets"][bucket] = cell["flip_ms_buckets"].get(bucket, 0) + 1

    # sort by_end_date maps
    for w in windows:
        for t in thresholds:
            cell = summary[w][t]
            cell["by_end_date"] = dict(sorted(cell["by_end_date"].items(), key=lambda kv: (-kv[1], kv[0]))[:10])
            cell["flip_ms_buckets"] = dict(sorted(cell["flip_ms_buckets"].items(), key=lambda kv: kv[0]))

    print(
        json.dumps(
            {
                "event": "polymarket_5m_false_spike_sweep",
                "hours": float(args.hours),
                "markets_considered": len(markets),
                "windows": windows,
                "thresholds": thresholds,
                "summary": summary,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

