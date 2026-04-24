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
Markdown report generator for Polymarket Weather Daily Temperature trading.

Pure functions: no I/O. File reading/writing is done by the caller or CLI.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any


# All known arenas — includes live-daemon arenas (temp_90c_no) and
# confirmed-entry daemon arena (temp_confirmed) so they always render.
ALL_ARENAS = [
    "temp_50c",
    "temp_60c",
    "temp_70c",
    "temp_80c",
    "temp_90c",
    "temp_90c_no",
    "temp_confirmed",
]

# Graduation gates (report as warnings, don't filter)
MIN_RESOLVED_PER_ARENA = 100
MIN_RESOLVED_PER_CITY = 30
MIN_CALENDAR_DAYS = 7


def normalize_strategy_name(row: dict[str, Any]) -> str:
    """Return a human-readable strategy name from a ledger row.

    Compatibility bridge: older daemon rows may not have a ``strategy_name``
    field but will have ``preset_name`` or ``strategy_type``.  Fall back in
    order so every row is attributed to a named strategy.
    """
    name = row.get("strategy_name")
    if name:
        return str(name)
    name = row.get("preset_name")
    if name:
        return str(name)
    name = row.get("strategy_type")
    if name:
        return str(name)
    return "unknown"


def make_entry_id(row: dict[str, Any]) -> str:
    """Return a stable identifier for a strategy_result ledger row.

    If the row already carries an ``entry_id`` field, that value is returned
    unchanged.  Otherwise a synthetic key is built from
    ``(market_slug, entry_time, token_side)`` so that:

    - Two strategies entering the same market on different days get different
      IDs (different ``entry_time``).
    - Two strategies entering the same market at the same time still differ if
      they took different sides (``token_side``).
    - The key is stable across repeated reads of the same JSONL file.
    """
    if row.get("entry_id"):
        return str(row["entry_id"])
    slug = row.get("market_slug", "")
    entry_time = row.get("entry_time") or row.get("timestamp") or ""
    token_side = row.get("token_side", "yes")
    return f"{slug}|{entry_time}|{token_side}"


def _classify_row(row: dict[str, Any]) -> str:
    """Classify a strategy_result row as 'win', 'loss', 'unresolved', or 'no_trade'."""
    resolved_outcome = row.get("resolved_outcome")
    if resolved_outcome == "no_trade":
        return "no_trade"

    shares = row.get("shares")
    if shares is None or shares == 0:
        return "no_trade"

    resolved = row.get("resolved")
    if not resolved:
        return "unresolved"

    # Non-oracle exits: if the row has an explicit exit_method (e.g. take_profit,
    # stop_loss) OR if settlement_price is between 0.01 and 0.99 (not a terminal
    # oracle value), trust resolved_outcome and pnl directly.
    exit_method = row.get("exit_method")
    settlement_price = row.get("settlement_price")
    pnl = row.get("pnl")

    if exit_method is not None:
        # Manual / TP / SL exit: trust resolved_outcome field
        return str(resolved_outcome) if resolved_outcome in ("win", "loss") else "unresolved"

    if settlement_price is not None:
        try:
            sp = float(settlement_price)
        except (ValueError, TypeError):
            sp = None
        if sp is not None and 0.01 <= sp <= 0.99:
            # Mid-market exit price — trust the pnl sign rather than oracle threshold
            return "win" if pnl is not None and float(pnl) > 0 else "loss"

    # Oracle resolution: winning token settles at 1.0, losing at 0.0
    if settlement_price == 1.0 and pnl is not None and float(pnl) > 0:
        return "win"
    return "loss"


def _empty_bucket() -> dict[str, Any]:
    return {
        "resolved_wins": 0,
        "resolved_losses": 0,
        "unresolved": 0,
        "no_trade": 0,
        "net_pnl": 0.0,
        "entry_prices": [],
    }


def _finalize_bucket(bucket: dict[str, Any]) -> None:
    """Compute derived metrics in-place."""
    wins = bucket["resolved_wins"]
    losses = bucket["resolved_losses"]
    resolved = wins + losses
    bucket["resolved_trades"] = resolved
    bucket["resolved_win_rate"] = wins / resolved if resolved > 0 else 0.0

    entry_prices = bucket.pop("entry_prices")
    avg_entry = sum(entry_prices) / len(entry_prices) if entry_prices else None
    bucket["breakeven_win_rate"] = avg_entry if avg_entry is not None else None
    bucket["edge"] = (
        (bucket["resolved_win_rate"] - avg_entry) if avg_entry is not None else None
    )
    bucket["net_pnl"] = round(bucket["net_pnl"], 6)


def _accumulate(bucket: dict[str, Any], row: dict[str, Any], classification: str) -> None:
    """Accumulate one row into a bucket."""
    if classification == "win":
        bucket["resolved_wins"] += 1
        bucket["net_pnl"] += float(row.get("pnl") or 0.0)
        if row.get("entry_price") is not None:
            bucket["entry_prices"].append(float(row["entry_price"]))
    elif classification == "loss":
        bucket["resolved_losses"] += 1
        bucket["net_pnl"] += float(row.get("pnl") or 0.0)
        if row.get("entry_price") is not None:
            bucket["entry_prices"].append(float(row["entry_price"]))
    elif classification == "unresolved":
        bucket["unresolved"] += 1
    elif classification == "no_trade":
        bucket["no_trade"] += 1


def merge_entries_with_settlements(rows: list[dict]) -> list[dict]:
    """Merge strategy_result entries with their settlement_update events.

    For each strategy_result row, if a matching settlement_update exists
    (matched by stable entry_id), override the entry's resolution fields
    with the settlement data.

    The stable entry_id is synthesised from (market_slug, entry_time,
    token_side) when an explicit ``entry_id`` field is absent.  This prevents
    two strategies entering the same market at different times from silently
    overwriting each other — the old (market_slug, strategy_name) key was
    non-unique in that scenario.

    Returns a list of merged rows ready for aggregation. Non-strategy_result,
    non-settlement_update rows pass through unchanged. Settlement_update rows
    with no matching strategy_result are dropped (orphaned settlements).
    """
    # Index settlement_update events by entry_id.
    # When multiple settlements share an entry_id, the last one wins (latest
    # correction record takes precedence).
    settlements: dict[str, dict] = {}
    for row in rows:
        if row.get("event") == "settlement_update":
            key = make_entry_id(row)
            settlements[key] = row

    result: list[dict] = []
    for row in rows:
        event = row.get("event")
        if event == "settlement_update":
            # Settlement rows are consumed via the index; don't include them directly
            continue
        if event == "strategy_result":
            row = dict(row)
            # Normalize strategy name for attribution
            if "strategy_name" not in row or not row["strategy_name"]:
                row["strategy_name"] = normalize_strategy_name(row)
            key = make_entry_id(row)
            settlement = settlements.get(key)
            if settlement is not None:
                # Copy the row and override resolution fields
                merged_row = dict(row)
                merged_row["resolved"] = settlement.get("resolved", True)
                merged_row["resolved_outcome"] = settlement.get("resolved_outcome")
                merged_row["settlement_price"] = settlement.get("settlement_price")
                merged_row["pnl"] = settlement.get("pnl")
                # Carry through exit_method if the settlement recorded one
                if settlement.get("exit_method") is not None:
                    merged_row["exit_method"] = settlement["exit_method"]
                result.append(merged_row)
            else:
                result.append(row)
        else:
            # Non-strategy, non-settlement events pass through unchanged
            result.append(row)
    return result


def build_weather_temperature_summary(rows: list[dict]) -> dict:
    """Build summary from JSONL strategy_result rows."""
    merged = merge_entries_with_settlements(rows)
    now = datetime.now(tz=UTC)

    # Collect run_ids for report metadata
    run_ids: set[str] = set()

    # Group buckets
    arena_buckets: dict[str, dict[str, Any]] = {}
    strategy_buckets: dict[str, dict[str, Any]] = {}
    city_buckets: dict[str, dict[str, Any]] = {}
    unresolved_trades: list[dict[str, Any]] = []
    observation_dates: set[str] = set()

    for row in merged:
        if row.get("event") != "strategy_result":
            continue

        run_id = row.get("run_id")
        if run_id:
            run_ids.add(str(run_id))

        classification = _classify_row(row)

        arena = row.get("arena", "unknown")
        strategy_name = normalize_strategy_name(row)
        city = row.get("city", "unknown")
        obs_date = row.get("observation_date")
        if obs_date:
            observation_dates.add(str(obs_date))

        # Arena
        if arena not in arena_buckets:
            arena_buckets[arena] = _empty_bucket()
        _accumulate(arena_buckets[arena], row, classification)

        # Strategy
        if strategy_name not in strategy_buckets:
            strategy_buckets[strategy_name] = _empty_bucket()
        _accumulate(strategy_buckets[strategy_name], row, classification)

        # City
        if city not in city_buckets:
            city_buckets[city] = _empty_bucket()
        _accumulate(city_buckets[city], row, classification)

        # Collect unresolved trades
        if classification == "unresolved":
            unresolved_trades.append({
                "market_slug": row.get("market_slug"),
                "arena": arena,
                "strategy_name": strategy_name,
                "city": city,
                "observation_date": obs_date,
                "entry_price": row.get("entry_price"),
                "shares": row.get("shares"),
                "stake": row.get("stake"),
            })

    # Finalize all buckets
    arena_leaderboard: list[dict[str, Any]] = []
    for arena_name, bucket in sorted(arena_buckets.items()):
        _finalize_bucket(bucket)
        bucket["arena"] = arena_name
        arena_leaderboard.append(bucket)

    strategy_leaderboard: list[dict[str, Any]] = []
    for strat_name, bucket in sorted(strategy_buckets.items()):
        _finalize_bucket(bucket)
        bucket["strategy_name"] = strat_name
        strategy_leaderboard.append(bucket)

    city_breakdown: list[dict[str, Any]] = []
    for city_name, bucket in sorted(city_buckets.items()):
        _finalize_bucket(bucket)
        bucket["city"] = city_name
        city_breakdown.append(bucket)

    # Sort leaderboards by net_pnl descending
    arena_leaderboard.sort(key=lambda r: -r["net_pnl"])
    strategy_leaderboard.sort(key=lambda r: -r["net_pnl"])
    city_breakdown.sort(key=lambda r: -r["net_pnl"])

    # Totals
    total_wins = sum(b["resolved_wins"] for b in arena_leaderboard)
    total_losses = sum(b["resolved_losses"] for b in arena_leaderboard)
    total_resolved = total_wins + total_losses
    total_unresolved = sum(b["unresolved"] for b in arena_leaderboard)
    total_no_trade = sum(b["no_trade"] for b in arena_leaderboard)
    total_pnl = round(sum(b["net_pnl"] for b in arena_leaderboard), 6)

    totals: dict[str, Any] = {
        "resolved_wins": total_wins,
        "resolved_losses": total_losses,
        "resolved_trades": total_resolved,
        "unresolved": total_unresolved,
        "no_trade": total_no_trade,
        "resolved_win_rate": total_wins / total_resolved if total_resolved > 0 else 0.0,
        "net_pnl": total_pnl,
    }

    # Data quality warnings
    warnings: list[str] = []
    for arena_name in ALL_ARENAS:
        matching = [a for a in arena_leaderboard if a["arena"] == arena_name]
        if matching:
            rt = matching[0]["resolved_trades"]
            if rt < MIN_RESOLVED_PER_ARENA:
                warnings.append(
                    f"Arena {arena_name} has only {rt} resolved trades, minimum {MIN_RESOLVED_PER_ARENA} needed"
                )
        else:
            warnings.append(
                f"Arena {arena_name} has only 0 resolved trades, minimum {MIN_RESOLVED_PER_ARENA} needed"
            )

    for city_entry in city_breakdown:
        rt = city_entry["resolved_trades"]
        if rt < MIN_RESOLVED_PER_CITY:
            warnings.append(
                f"City {city_entry['city']} has only {rt} resolved trades, minimum {MIN_RESOLVED_PER_CITY} needed"
            )

    calendar_days = len(observation_dates)
    if calendar_days < MIN_CALENDAR_DAYS:
        warnings.append(
            f"Only {calendar_days} calendar days of data, minimum {MIN_CALENDAR_DAYS} needed"
        )

    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "run_ids": sorted(run_ids),
        "arena_leaderboard": arena_leaderboard,
        "strategy_leaderboard": strategy_leaderboard,
        "city_breakdown": city_breakdown,
        "unresolved_trades": unresolved_trades,
        "totals": totals,
        "data_quality": {
            "warnings": warnings,
            "calendar_days": calendar_days,
        },
    }


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def _fmt_money(value: float) -> str:
    return f"${value:+.4f}"


def _fmt_edge(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:+.1f}pp"


def render_weather_temperature_markdown(summary: dict[str, Any]) -> str:
    """Render summary dict as Markdown report."""
    md: list[str] = []

    md.append("# Weather Daily Temperature Trading Report")
    md.append("")
    md.append(f"> Last updated: {summary['generated_at']}")
    md.append("")

    # Run files
    md.append("## Run Files Included")
    md.append("")
    if summary["run_ids"]:
        for run_id in summary["run_ids"]:
            md.append(f"- `{run_id}`")
    else:
        md.append("- (no run files)")
    md.append("")

    # Arena leaderboard
    md.append("## Arena Leaderboard")
    md.append("")
    md.append(
        "| Arena | Resolved Trades | Wins | Losses | Unresolved | No Trade "
        "| Win Rate | Breakeven | Edge | Net P/L |"
    )
    md.append(
        "|-------|----------------:|-----:|-------:|-----------:|---------:"
        "|---------:|----------:|-----:|--------:|"
    )
    # Always show all arenas
    arena_by_name = {a["arena"]: a for a in summary["arena_leaderboard"]}
    for arena_name in ALL_ARENAS:
        if arena_name in arena_by_name:
            a = arena_by_name[arena_name]
            md.append(
                f"| {a['arena']} | {a['resolved_trades']} | {a['resolved_wins']} "
                f"| {a['resolved_losses']} | {a['unresolved']} | {a['no_trade']} "
                f"| {_fmt_pct(a['resolved_win_rate'])} | {_fmt_pct(a['breakeven_win_rate'])} "
                f"| {_fmt_edge(a['edge'])} | {_fmt_money(a['net_pnl'])} |"
            )
        else:
            md.append(
                f"| {arena_name} | 0 | 0 | 0 | 0 | 0 | - | - | - | $+0.0000 |"
            )
    # Totals row
    t = summary["totals"]
    md.append(
        f"| **TOTAL** | **{t['resolved_trades']}** | **{t['resolved_wins']}** "
        f"| **{t['resolved_losses']}** | **{t['unresolved']}** | **{t['no_trade']}** "
        f"| **{_fmt_pct(t['resolved_win_rate'])}** | | "
        f"| **{_fmt_money(t['net_pnl'])}** |"
    )
    md.append("")

    # Strategy leaderboard
    md.append("## Strategy Leaderboard")
    md.append("")
    md.append(
        "| Strategy | Resolved Trades | Wins | Losses | Unresolved | No Trade "
        "| Win Rate | Breakeven | Edge | Net P/L |"
    )
    md.append(
        "|----------|----------------:|-----:|-------:|-----------:|---------:"
        "|---------:|----------:|-----:|--------:|"
    )
    for s in summary["strategy_leaderboard"]:
        md.append(
            f"| {s['strategy_name']} | {s['resolved_trades']} | {s['resolved_wins']} "
            f"| {s['resolved_losses']} | {s['unresolved']} | {s['no_trade']} "
            f"| {_fmt_pct(s['resolved_win_rate'])} | {_fmt_pct(s['breakeven_win_rate'])} "
            f"| {_fmt_edge(s['edge'])} | {_fmt_money(s['net_pnl'])} |"
        )
    md.append("")

    # City breakdown
    md.append("## City Breakdown")
    md.append("")
    md.append(
        "| City | Resolved Trades | Wins | Losses | Unresolved | No Trade "
        "| Win Rate | Net P/L |"
    )
    md.append(
        "|------|----------------:|-----:|-------:|-----------:|---------:"
        "|---------:|--------:|"
    )
    for c in summary["city_breakdown"]:
        md.append(
            f"| {c['city']} | {c['resolved_trades']} | {c['resolved_wins']} "
            f"| {c['resolved_losses']} | {c['unresolved']} | {c['no_trade']} "
            f"| {_fmt_pct(c['resolved_win_rate'])} | {_fmt_money(c['net_pnl'])} |"
        )
    md.append("")

    # Unresolved trades
    md.append("## Unresolved Trades")
    md.append("")
    if summary["unresolved_trades"]:
        md.append("| Market Slug | Arena | Strategy | City | Obs Date | Entry | Shares | Stake |")
        md.append("|-------------|-------|----------|------|----------|------:|-------:|------:|")
        for u in summary["unresolved_trades"]:
            entry = f"{u['entry_price']:.2f}" if u.get("entry_price") is not None else "-"
            shares = f"{u['shares']:.1f}" if u.get("shares") is not None else "-"
            stake = f"{u['stake']:.2f}" if u.get("stake") is not None else "-"
            md.append(
                f"| {u.get('market_slug', '-')} | {u.get('arena', '-')} "
                f"| {u.get('strategy_name', '-')} | {u.get('city', '-')} "
                f"| {u.get('observation_date', '-')} | {entry} | {shares} | {stake} |"
            )
    else:
        md.append("No unresolved trades.")
    md.append("")

    # Data quality warnings
    md.append("## Data Quality Warnings")
    md.append("")
    warnings = summary.get("data_quality", {}).get("warnings", [])
    if warnings:
        for warning in warnings:
            md.append(f"- {warning}")
    else:
        md.append("No warnings.")
    md.append("")

    return "\n".join(md)
