#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_REPORT_ROOT = "/workspace/outputs"
DEFAULT_REPORT_MD = "/workspace/WEATHER_ENSEMBLE_RESULTS.md"


def _entry_id(row: dict[str, Any]) -> str:
    slug = str(row.get("market_slug") or "")
    entry_time = str(row.get("entry_time") or row.get("timestamp") or "")
    token_side = str(row.get("token_side") or row.get("selected_side") or "")
    strategy_name = str(row.get("strategy_name") or row.get("preset_name") or "")
    return f"{slug}|{entry_time}|{token_side}|{strategy_name}"


def merge_entries_with_settlements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settlements: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("event") != "settlement_update":
            continue
        entry_id = row.get("entry_id")
        key = str(entry_id) if entry_id else _entry_id(row)
        settlements[key] = row

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row.get("event") != "strategy_result":
            continue
        key = str(row.get("entry_id") or _entry_id(row))
        if key in seen:
            continue
        seen.add(key)
        settlement = settlements.get(key)
        merged_row = dict(row)
        if settlement is not None:
            merged_row["resolved"] = settlement.get("resolved", True)
            merged_row["settlement_price"] = settlement.get("settlement_price")
            merged_row["settlement_source"] = settlement.get("settlement_source")
            merged_row["resolved_outcome"] = settlement.get("resolved_outcome")
            merged_row["pnl"] = settlement.get("pnl")
        merged.append(merged_row)
    return merged


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _empty_bucket() -> dict[str, Any]:
    return {
        "resolved_wins": 0,
        "resolved_losses": 0,
        "resolved_trades": 0,
        "unresolved": 0,
        "net_pnl": 0.0,
        "edges": [],
        "entry_prices": [],
    }


def _finalize_bucket(bucket: dict[str, Any]) -> None:
    resolved = bucket["resolved_wins"] + bucket["resolved_losses"]
    bucket["resolved_trades"] = resolved
    bucket["resolved_win_rate"] = (bucket["resolved_wins"] / resolved) if resolved else None
    edges = bucket.pop("edges")
    entry_prices = bucket.pop("entry_prices")
    bucket["avg_edge"] = (sum(edges) / len(edges)) if edges else None
    bucket["avg_entry_price"] = (sum(entry_prices) / len(entry_prices)) if entry_prices else None
    bucket["net_pnl"] = round(bucket["net_pnl"], 6)


def _bucket_calibration_key(probability: float | None) -> str | None:
    if probability is None:
        return None
    clipped = max(0.0, min(probability, 0.999999))
    lower = int(clipped * 10) * 10
    upper = lower + 10
    return f"{lower:02d}-{upper:02d}%"


def build_weather_ensemble_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    merged = merge_entries_with_settlements(rows)
    city_buckets: dict[str, dict[str, Any]] = defaultdict(_empty_bucket)
    threshold_buckets: dict[str, dict[str, Any]] = defaultdict(_empty_bucket)
    calibration_buckets: dict[str, dict[str, Any]] = defaultdict(_empty_bucket)
    unresolved_positions: list[dict[str, Any]] = []
    forecast_sources: set[str] = set()
    settlement_sources: set[str] = set()

    strategy_rows = [
        row for row in merged
        if row.get("event") == "strategy_result"
        and row.get("market_slug")
        and row.get("city")
        and row.get("threshold") is not None
        and (row.get("token_side") or row.get("selected_side"))
        and row.get("entry_price") is not None
    ]
    scanned_markets = len(
        {
            str(row.get("market_slug") or "")
            for row in rows
            if row.get("event") in {"market_snapshot", "strategy_result"} and row.get("market_slug")
        },
    )
    accepted_candidates = len(strategy_rows)

    resolved_trades = 0
    unresolved_trades = 0
    net_pnl = 0.0
    edge_values: list[float] = []
    entry_values: list[float] = []

    for row in strategy_rows:
        city = str(row.get("city") or "unknown")
        threshold = str(row.get("threshold") or "unknown")
        band_type = str(row.get("band_type") or "unknown")
        threshold_key = f"{threshold}|{band_type}"
        resolved = bool(row.get("resolved"))
        pnl = _safe_float(row.get("pnl")) or 0.0
        edge = _safe_float(row.get("edge"))
        entry_price = _safe_float(row.get("entry_price"))
        model_yes_probability = _safe_float(row.get("model_yes_probability"))
        calibration_key = _bucket_calibration_key(model_yes_probability)
        forecast_source = str(row.get("forecast_source") or "")
        settlement_source = str(row.get("settlement_source") or "")

        if forecast_source:
            forecast_sources.add(forecast_source)
        if settlement_source:
            settlement_sources.add(settlement_source)

        for bucket in (city_buckets[city], threshold_buckets[threshold_key]):
            if resolved:
                if pnl > 0:
                    bucket["resolved_wins"] += 1
                else:
                    bucket["resolved_losses"] += 1
                bucket["net_pnl"] += pnl
                if edge is not None:
                    bucket["edges"].append(edge)
                if entry_price is not None:
                    bucket["entry_prices"].append(entry_price)
            else:
                bucket["unresolved"] += 1

        if calibration_key is not None:
            bucket = calibration_buckets[calibration_key]
            if resolved:
                if pnl > 0:
                    bucket["resolved_wins"] += 1
                else:
                    bucket["resolved_losses"] += 1
                bucket["net_pnl"] += pnl
            else:
                bucket["unresolved"] += 1

        if resolved:
            resolved_trades += 1
            net_pnl += pnl
            if edge is not None:
                edge_values.append(edge)
            if entry_price is not None:
                entry_values.append(entry_price)
        else:
            unresolved_trades += 1
            unresolved_positions.append(
                {
                    "city": city,
                    "threshold": threshold,
                    "band_type": band_type,
                    "token_side": row.get("token_side") or row.get("selected_side"),
                    "entry_price": entry_price,
                    "edge": edge,
                    "forecast_source": forecast_source or "-",
                    "market_slug": row.get("market_slug"),
                },
            )

    def _finalize_buckets(buckets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for key, bucket in buckets.items():
            _finalize_bucket(bucket)
            items.append({"_key": key, **bucket})
        items.sort(key=lambda row: (-row["net_pnl"], row["_key"]))
        return items

    calibration_items = _finalize_buckets(calibration_buckets)
    city_items = _finalize_buckets(city_buckets)
    threshold_items = _finalize_buckets(threshold_buckets)

    totals = {
        "scanned_markets": scanned_markets,
        "accepted_candidates": accepted_candidates,
        "entered_positions": accepted_candidates,
        "resolved_trades": resolved_trades,
        "unresolved_positions": unresolved_trades,
        "win_rate": (
            sum(1 for row in strategy_rows if bool(row.get("resolved")) and (_safe_float(row.get("pnl")) or 0.0) > 0) / resolved_trades
            if resolved_trades
            else None
        ),
        "net_pnl": round(net_pnl, 6),
        "avg_edge": (sum(edge_values) / len(edge_values)) if edge_values else None,
        "avg_entry_price": (sum(entry_values) / len(entry_values)) if entry_values else None,
    }

    return {
        "generated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "totals": totals,
        "city_breakdown": city_items,
        "threshold_breakdown": threshold_items,
        "calibration_breakdown": calibration_items,
        "unresolved_positions": sorted(unresolved_positions, key=lambda row: (str(row.get("city") or ""), str(row.get("threshold") or ""))),
        "forecast_sources": sorted(forecast_sources),
        "settlement_sources": sorted(settlement_sources),
    }


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _money(value: float) -> str:
    return f"${value:+.4f}"


def _float_text(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render_weather_ensemble_markdown(summary: dict[str, Any]) -> str:
    totals = summary["totals"]
    lines = [
        "# Polymarket Weather Ensemble Report",
        "",
        f"> Last updated: {summary['generated_at']}",
        "",
        "## Portfolio Summary",
        "",
        f"**Scanned Markets:** {totals['scanned_markets']}",
        f"**Accepted Candidates:** {totals['accepted_candidates']}",
        f"**Entered Positions:** {totals['entered_positions']}",
        f"**Resolved Trades:** {totals['resolved_trades']}",
        f"**Unresolved Positions:** {totals['unresolved_positions']}",
        f"**Win Rate:** {_pct(totals['win_rate'])}",
        f"**Net P/L:** {_money(float(totals['net_pnl']))}",
        f"**Average Edge:** {_pct(totals['avg_edge'])}",
        f"**Average Entry Price:** {_float_text(totals['avg_entry_price'])}",
        "",
        f"**Forecast Sources:** {', '.join(summary['forecast_sources']) if summary['forecast_sources'] else '-'}",
        f"**Settlement Sources:** {', '.join(summary['settlement_sources']) if summary['settlement_sources'] else '-'}",
        "",
        "## By City",
        "",
        "| City | Resolved | W | L | Unresolved | Win Rate | Avg Edge | Avg Entry | Net P/L |",
        "|------|---------:|--:|--:|-----------:|---------:|---------:|----------:|--------:|",
    ]

    for row in summary["city_breakdown"]:
        lines.append(
            f"| {row['_key']} | {row['resolved_trades']} | {row['resolved_wins']} | {row['resolved_losses']} | {row['unresolved']} | {_pct(row['resolved_win_rate'])} | {_pct(row['avg_edge'])} | {_float_text(row['avg_entry_price'])} | {_money(float(row['net_pnl']))} |"
        )

    lines.extend(
        [
            "",
            "## By Threshold",
            "",
            "| Threshold | Resolved | W | L | Unresolved | Win Rate | Avg Edge | Avg Entry | Net P/L |",
            "|-----------|---------:|--:|--:|-----------:|---------:|---------:|----------:|--------:|",
        ],
    )
    for row in summary["threshold_breakdown"]:
        lines.append(
            f"| {row['_key']} | {row['resolved_trades']} | {row['resolved_wins']} | {row['resolved_losses']} | {row['unresolved']} | {_pct(row['resolved_win_rate'])} | {_pct(row['avg_edge'])} | {_float_text(row['avg_entry_price'])} | {_money(float(row['net_pnl']))} |"
        )

    lines.extend(
        [
            "",
            "## Calibration Buckets",
            "",
            "| Bucket | Resolved | W | L | Unresolved | Win Rate | Net P/L |",
            "|--------|---------:|--:|--:|-----------:|---------:|--------:|",
        ],
    )
    for row in summary["calibration_breakdown"]:
        lines.append(
            f"| {row['_key']} | {row['resolved_trades']} | {row['resolved_wins']} | {row['resolved_losses']} | {row['unresolved']} | {_pct(row['resolved_win_rate'])} | {_money(float(row['net_pnl']))} |"
        )

    lines.extend(["", "## Open Positions", ""])
    if summary["unresolved_positions"]:
        lines.extend(
            [
                "| City | Threshold | Band | Side | Entry | Edge | Forecast Source | Slug |",
                "|------|-----------|------|------|------:|-----:|-----------------|------|",
            ],
        )
        for row in summary["unresolved_positions"]:
            lines.append(
                f"| {row.get('city','-')} | {row.get('threshold','-')} | {row.get('band_type','-')} | {row.get('token_side','-')} | {_float_text(row.get('entry_price'))} | {_pct(row.get('edge'))} | {row.get('forecast_source','-')} | {row.get('market_slug','-')} |"
            )
    else:
        lines.append("No open weather ensemble positions.")

    lines.append("")
    return "\n".join(lines)


def _read_jsonl_dir(jsonl_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not jsonl_dir.exists():
        return rows
    for path in sorted(jsonl_dir.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return rows


def run_report(*, report_root: str, report_md: str) -> None:
    jsonl_dir = Path(report_root) / "polymarket" / "weather_ensemble"
    rows = _read_jsonl_dir(jsonl_dir)
    if not rows:
        print(f"No JSONL data found in {jsonl_dir}")
        return
    summary = build_weather_ensemble_summary(rows)
    markdown = render_weather_ensemble_markdown(summary)
    output_path = Path(report_md)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    print(f"Wrote {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate WEATHER_ENSEMBLE_RESULTS.md from weather ensemble JSONL data")
    parser.add_argument("--report-root", default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--report-md", default=DEFAULT_REPORT_MD)
    args = parser.parse_args()
    run_report(report_root=args.report_root, report_md=args.report_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
