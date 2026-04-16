#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------
"""
Machine-readable reporting for Polymarket 5-minute paper-trading daemon outputs.
"""

from __future__ import annotations

import argparse
from datetime import UTC
from datetime import datetime
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1"


def _resolve_report_root(report_root: str | Path) -> Path:
    root = Path(report_root)
    if ".." in root.parts:
        raise ValueError("report_root must not contain '..'")
    return root.resolve(strict=False)


def loop_label_from_stem(stem: str) -> str:
    if stem.startswith("overnight_"):
        rest = stem[len("overnight_") :]
        return rest.split("_", 1)[0].upper()
    return stem.upper().replace("-", "_")


def discover_run_paths(report_root: str | Path) -> list[Path]:
    run_dir = _resolve_report_root(report_root) / "polymarket" / "runs"
    return sorted(run_dir.glob("overnight_*.jsonl"), key=lambda path: path.name)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle.read().splitlines() if line.strip()]


def _accounting_status(row: dict[str, Any]) -> str:
    status = row.get("accounting_status")
    if status and str(status) != "settled":
        return str(status)
    exit_reason = row.get("exit_reason")
    if exit_reason == "position_open":
        return "open"
    if exit_reason == "accounting_invalid" or row.get("entry_side") == "sell":
        return "invalid_entry_side"
    if row.get("entry_in_configured_band") is False:
        return "invalid_entry_band"
    if status:
        return str(status)
    if exit_reason == "no_position":
        return "no_position"
    return "settled"


def _is_trade(row: dict[str, Any]) -> bool:
    status = _accounting_status(row)
    if status in {"open", "no_position"} or status.startswith("invalid_"):
        return False
    if row.get("entered") is not None:
        return bool(row.get("entered"))
    return (
        row.get("entry_time") not in (None, "")
        and row.get("pnl") is not None
        and row.get("stake") is not None
    )


def _rounds_in_rows(rows: list[dict[str, Any]]) -> int:
    round_start_count = sum(1 for row in rows if row.get("event") == "round_start")
    if round_start_count > 0:
        return round_start_count
    skipped_count = sum(1 for row in rows if row.get("event") == "round_skipped")
    if skipped_count > 0:
        return skipped_count
    session_ids = {
        str(row["session_id"])
        for row in rows
        if row.get("session_id")
    }
    return len(session_ids)


def _default_strategy_row(loop: str, name: str) -> dict[str, Any]:
    return {
        "loop": loop,
        "strategy_name": name,
        "rounds": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "no_trade": 0,
        "open_positions": 0,
        "invalid_results": 0,
        "target_exits": 0,
        "stop_losses": 0,
        "settled_wins": 0,
        "settled_losses": 0,
        "net_pnl": 0.0,
        "total_stake": 0.0,
        "avg_entry_samples": [],
        "provisional": False,
    }


def build_summary(*, report_root: str | Path, now: datetime | None = None) -> dict[str, Any]:
    root = _resolve_report_root(report_root)
    generated_at = (now or datetime.now(tz=UTC)).astimezone(UTC)
    run_paths = discover_run_paths(root)
    sessions: list[dict[str, Any]] = []
    strategies: dict[tuple[str, str], dict[str, Any]] = {}
    rounds_by_loop: dict[str, int] = {}
    rounds_skipped = 0
    invalid_accounting_strategies: set[str] = set()

    for path in run_paths:
        rows = _load_jsonl(path)
        loop = loop_label_from_stem(path.stem)
        rounds = _rounds_in_rows(rows)
        rounds_by_loop[loop] = rounds_by_loop.get(loop, 0) + rounds
        sessions.append({"loop": loop, "file": path.name, "rounds": rounds})

        for row in rows:
            event = row.get("event")
            if event == "round_skipped":
                rounds_skipped += 1
                continue
            if event != "strategy_result":
                continue

            name = str(row["strategy_name"])
            key = (loop, name)
            aggregate = strategies.setdefault(key, _default_strategy_row(loop, name))
            accounting_status = _accounting_status(row)
            trade = _is_trade(row)
            pnl = row.get("pnl")
            stake = row.get("stake")
            exit_reason = row.get("exit_reason")
            entry_price = row.get("entry_price")

            provisional = (
                accounting_status in {"open", "no_position"}
                or accounting_status.startswith("invalid_")
                or (
                    not trade
                    and (pnl is None or stake is None)
                    and exit_reason not in ("settled_win", "settled_loss")
                )
            )
            aggregate["provisional"] = aggregate["provisional"] or provisional

            if accounting_status == "open":
                aggregate["open_positions"] += 1
                continue

            if accounting_status.startswith("invalid_"):
                aggregate["invalid_results"] += 1
                invalid_accounting_strategies.add(name)
                continue

            if not trade:
                aggregate["no_trade"] += 1
                continue

            aggregate["trades"] += 1
            pnl_value = 0.0 if pnl is None else float(pnl)
            stake_value = 0.0 if stake is None else float(stake)
            aggregate["net_pnl"] += pnl_value
            aggregate["total_stake"] += stake_value

            if pnl_value > 0:
                aggregate["wins"] += 1
            elif pnl_value < 0:
                aggregate["losses"] += 1

            if exit_reason == "target_exit":
                aggregate["target_exits"] += 1
            elif exit_reason == "stop_loss_exit":
                aggregate["stop_losses"] += 1
            elif exit_reason == "settled_win":
                aggregate["settled_wins"] += 1
            elif exit_reason == "settled_loss":
                aggregate["settled_losses"] += 1

            if entry_price not in (None, ""):
                aggregate["avg_entry_samples"].append(float(entry_price))

    leaderboard: list[dict[str, Any]] = []
    provisional_strategies: list[str] = []
    for strategy in strategies.values():
        strategy["rounds"] = rounds_by_loop.get(strategy["loop"], 0)
        avg_samples = strategy.pop("avg_entry_samples")
        total_stake = round(float(strategy["total_stake"]), 4)
        strategy["total_stake"] = total_stake
        net_pnl = round(float(strategy["net_pnl"]), 4)
        strategy["net_pnl"] = net_pnl
        strategy["win_rate"] = round(strategy["wins"] / strategy["trades"], 6) if strategy["trades"] else 0.0
        strategy["avg_entry_price"] = round(sum(avg_samples) / len(avg_samples), 3) if avg_samples else None
        strategy["roi"] = round(net_pnl / total_stake, 6) if total_stake > 0 else None
        if strategy["provisional"]:
            provisional_strategies.append(strategy["strategy_name"])
        leaderboard.append(strategy)

    leaderboard.sort(
        key=lambda row: (
            -float(row["net_pnl"]),
            -float(row["roi"] or 0.0),
            str(row["strategy_name"]),
        ),
    )
    for index, row in enumerate(leaderboard, start=1):
        row["rank"] = index

    totals = {
        "sessions": len(sessions),
        "rounds": sum(session["rounds"] for session in sessions),
        "rounds_skipped": rounds_skipped,
        "trades": sum(row["trades"] for row in leaderboard),
        "wins": sum(row["wins"] for row in leaderboard),
        "losses": sum(row["losses"] for row in leaderboard),
        "no_trade": sum(row["no_trade"] for row in leaderboard),
        "open_positions": sum(row["open_positions"] for row in leaderboard),
        "invalid_results": sum(row["invalid_results"] for row in leaderboard),
        "target_exits": sum(row["target_exits"] for row in leaderboard),
        "stop_losses": sum(row["stop_losses"] for row in leaderboard),
        "settled_wins": sum(row["settled_wins"] for row in leaderboard),
        "settled_losses": sum(row["settled_losses"] for row in leaderboard),
        "net_pnl": round(sum(float(row["net_pnl"]) for row in leaderboard), 4),
    }
    totals["win_rate"] = round(totals["wins"] / totals["trades"], 6) if totals["trades"] else 0.0
    total_stake = sum(float(row.get("total_stake") or 0.0) for row in leaderboard)
    totals["total_stake"] = round(total_stake, 4)
    totals["roi"] = round(totals["net_pnl"] / total_stake, 6) if total_stake > 0 else 0.0

    notes: list[str] = []
    if provisional_strategies:
        notes.append("Metrics may be provisional until daemon trade accounting is expanded.")
    if invalid_accounting_strategies:
        notes.append("Invalid accounting rows are excluded from realized P/L.")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "report_info": {
            "source_dir": str(root / "polymarket" / "runs"),
            "source_files": [path.name for path in run_paths],
        },
        "sessions": sessions,
        "leaderboard": leaderboard,
        "totals": totals,
        "notes": notes,
        "data_quality": {
            "provisional_metrics_present": bool(provisional_strategies),
            "provisional_strategies": provisional_strategies,
            "invalid_accounting_present": bool(invalid_accounting_strategies),
            "invalid_accounting_strategies": sorted(invalid_accounting_strategies),
        },
    }


def _fmt_win_rate(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.0f}%"


def _fmt_roi(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def _fmt_avg_entry(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _fmt_money(value: float) -> str:
    return f"${value:+.4f}"


def render_results_markdown(summary: dict[str, Any]) -> str:
    md: list[str] = []
    md.append("# Polymarket 5m Crypto Paper Trading — Nautilus Results")
    md.append("")
    md.append(f"> Last updated: {summary['generated_at']}")
    md.append("")
    md.append("## Sessions Included")
    md.append("")
    md.append("| Loop | Session File | Rounds |")
    md.append("|------|-------------|-------:|")
    for session in summary["sessions"]:
        md.append(f"| {session['loop']} | `{session['file']}` | {session['rounds']} |")
    md.append(f"| **ALL** | | **{summary['totals']['rounds']}** |")
    md.append("")
    md.append("## Strategy Leaderboard (ranked by net PnL)")
    md.append("")
    md.append(
        "| # | Loop | Strategy | Rounds | Trades | W | L | No Trade | Open | Invalid | "
        "Tgt Exit | Stop Loss | Stl Win | Stl Loss | Win % | Avg Entry | Net PnL | ROI |"
    )
    md.append(
        "|--:|------|----------|-------:|-------:|--:|--:|---------:|-----:|--------:|"
        "---------:|----------:|--------:|---------:|------:|----------:|--------:|----:|"
    )
    for row in summary["leaderboard"]:
        md.append(
            f"| {row['rank']} | {row['loop']} | {row['strategy_name']} | {row['rounds']} | {row['trades']} "
            f"| {row['wins']} | {row['losses']} | {row['no_trade']} | {row.get('open_positions', 0)} "
            f"| {row.get('invalid_results', 0)} | {row['target_exits']} "
            f"| {row['stop_losses']} | {row['settled_wins']} | {row['settled_losses']} "
            f"| {_fmt_win_rate(row['win_rate'])} | {_fmt_avg_entry(row['avg_entry_price'])} "
            f"| {_fmt_money(float(row['net_pnl']))} | {_fmt_roi(row['roi'])} |"
        )
    totals = summary["totals"]
    md.append(
        f"| | **ALL** | **TOTALS** | **{totals['rounds']}** | **{totals['trades']}** | **{totals['wins']}** "
        f"| **{totals['losses']}** | **{totals['no_trade']}** | **{totals.get('open_positions', 0)}** "
        f"| **{totals.get('invalid_results', 0)}** | **{totals['target_exits']}** "
        f"| **{totals['stop_losses']}** | **{totals['settled_wins']}** | **{totals['settled_losses']}** "
        f"| **{_fmt_win_rate(totals['win_rate'])}** | | **{_fmt_money(float(totals['net_pnl']))}** "
        f"| **{_fmt_roi(totals['roi'])}** |"
    )
    if summary.get("notes"):
        md.append("")
        md.append("## Notes")
        md.append("")
        for note in summary["notes"]:
            md.append(f"- {note}")
    md.append("")
    return "\n".join(md)


def write_report_outputs(*, report_root: str | Path, summary: dict[str, Any]) -> dict[str, Path]:
    root = _resolve_report_root(report_root) / "polymarket" / "reports"
    root.mkdir(parents=True, exist_ok=True)
    stamp = str(summary["generated_at"]).replace("-", "").replace(":", "").replace("T", "T").replace("Z", "Z")
    stamp = stamp[:15] + "Z" if len(stamp) >= 15 else stamp
    summary_latest = root / "summary_latest.json"
    summary_timestamped = root / f"summary_{stamp}.json"
    results_markdown = root / "RESULTS.md"

    summary_text = json.dumps(summary, indent=2, sort_keys=True)
    summary_latest.write_text(summary_text, encoding="utf-8")
    summary_timestamped.write_text(summary_text, encoding="utf-8")
    results_markdown.write_text(render_results_markdown(summary), encoding="utf-8")
    return {
        "summary_latest": summary_latest,
        "summary_timestamped": summary_timestamped,
        "results_markdown": results_markdown,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-root", default="/workspace/outputs")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    summary = build_summary(report_root=args.report_root)
    paths = write_report_outputs(report_root=args.report_root, summary=summary)
    print(json.dumps({key: str(value) for key, value in paths.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
