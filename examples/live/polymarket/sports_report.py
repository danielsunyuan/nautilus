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
Markdown report generator for Polymarket Sports paper trading.

Pure functions: no I/O. File reading/writing is done by the caller or CLI.

Aggregates by:
  - Arena (50c / 60c / 70c / 80c / 90c)
  - Preset (band_only vs basic per arena)
  - Market type (moneyline / spreads / totals)
  - Sport (nba / tennis / boxing / mma)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any


ALL_ARENAS = ["sports_50c", "sports_60c", "sports_70c", "sports_80c", "sports_90c"]
ALL_MARKET_TYPES = ["moneyline", "spreads", "totals", "nrfi", "ufc_method_of_victory", "ufc_go_the_distance"]
ALL_SPORTS = ["nba", "mlb", "ufc", "tennis", "boxing", "hockey"]

MIN_RESOLVED_PER_ARENA = 100
MIN_RESOLVED_FOR_MARKET_TYPE = 50
MIN_CALENDAR_DAYS = 7


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def _classify_row(row: dict[str, Any]) -> str:
    """Classify a row as 'win', 'loss', 'unresolved', or 'no_trade'."""
    if row.get("accounting_status") == "no_position" or row.get("entry_price") is None:
        return "no_trade"
    if not row.get("resolved"):
        return "unresolved"
    pnl = row.get("pnl")
    if pnl is not None and pnl > 0:
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


def _accumulate(bucket: dict[str, Any], row: dict[str, Any], cls: str) -> None:
    if cls in ("win", "loss"):
        if cls == "win":
            bucket["resolved_wins"] += 1
        else:
            bucket["resolved_losses"] += 1
        bucket["net_pnl"] += float(row.get("pnl") or 0.0)
        if row.get("entry_price") is not None:
            bucket["entry_prices"].append(float(row["entry_price"]))
    elif cls == "unresolved":
        bucket["unresolved"] += 1
    else:
        bucket["no_trade"] += 1


def _finalize_bucket(bucket: dict[str, Any]) -> None:
    wins = bucket["resolved_wins"]
    losses = bucket["resolved_losses"]
    resolved = wins + losses
    bucket["resolved_trades"] = resolved
    bucket["resolved_win_rate"] = wins / resolved if resolved > 0 else None

    entry_prices = bucket.pop("entry_prices")
    avg_entry = sum(entry_prices) / len(entry_prices) if entry_prices else None
    bucket["breakeven_win_rate"] = avg_entry
    bucket["edge"] = (
        (bucket["resolved_win_rate"] - avg_entry)
        if bucket["resolved_win_rate"] is not None and avg_entry is not None
        else None
    )
    bucket["net_pnl"] = round(bucket["net_pnl"], 6)


# ---------------------------------------------------------------------------
# Settlement merge
# ---------------------------------------------------------------------------

def merge_entries_with_settlements(rows: list[dict]) -> list[dict]:
    """Merge strategy_result rows with settlement_update events.

    Match key: (condition_id, outcome_name, preset_name).
    settlement_update rows are consumed; only merged strategy_result rows appear in output.
    """
    settlements: dict[tuple, dict] = {}
    for row in rows:
        if row.get("event") == "settlement_update":
            key = (
                row.get("condition_id", ""),
                row.get("outcome_name", ""),
                row.get("preset_name", ""),
            )
            # Latest settlement wins
            settlements[key] = row

    result: list[dict] = []
    seen_keys: set[tuple] = set()

    for row in rows:
        event = row.get("event")
        if event == "settlement_update":
            continue
        if event != "strategy_result":
            result.append(row)
            continue

        # Deduplicate: one entry per (condition_id, outcome_name, preset_name).
        # Prefer 'open' over 'no_position': if we already recorded a no_position entry
        # and a later round produced an actual entry, upgrade to the open entry.
        dedup_key = (
            row.get("condition_id", ""),
            row.get("outcome_name", ""),
            row.get("preset_name", ""),
        )
        is_no_position = (
            row.get("accounting_status") == "no_position"
            or row.get("entry_price") is None
        )
        if dedup_key in seen_keys:
            # Only upgrade if the stored entry was no_position and this one is open
            if is_no_position:
                continue
            # Remove the previously added no_position entry so we can replace it
            for i in range(len(result) - 1, -1, -1):
                prev = result[i]
                if prev.get("event") == "strategy_result" and (
                    prev.get("condition_id", ""),
                    prev.get("outcome_name", ""),
                    prev.get("preset_name", ""),
                ) == dedup_key:
                    result.pop(i)
                    break
            # Fall through to re-add with the open entry
        seen_keys.add(dedup_key)

        key = dedup_key
        settlement = settlements.get(key)
        if settlement is not None:
            merged = dict(row)
            merged["resolved"] = True
            merged["resolved_outcome"] = settlement.get("resolved_outcome")
            merged["settlement_price"] = settlement.get("settlement_price")
            merged["pnl"] = settlement.get("pnl")
            result.append(merged)
        else:
            result.append(row)

    return result


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def build_sports_summary(rows: list[dict]) -> dict[str, Any]:
    """Build aggregated summary from raw JSONL rows (strategy_result + settlement_update)."""
    merged = merge_entries_with_settlements(rows)
    now = datetime.now(tz=UTC)

    run_ids: set[str] = set()
    game_dates: set[str] = set()

    arena_buckets: dict[str, dict] = {}
    preset_buckets: dict[str, dict] = {}
    market_type_buckets: dict[str, dict] = {}
    sport_buckets: dict[str, dict] = {}
    unresolved_trades: list[dict] = []

    for row in merged:
        if row.get("event") != "strategy_result":
            continue

        run_id = row.get("run_id")
        if run_id:
            run_ids.add(str(run_id))

        game_time = row.get("game_time", "")
        if game_time:
            game_dates.add(game_time[:10])  # YYYY-MM-DD

        cls = _classify_row(row)

        arena = row.get("arena", "unknown")
        preset = row.get("preset_name", "unknown")
        market_type = row.get("market_type", "unknown")
        sport = row.get("sport", "unknown")

        for buckets, key in [
            (arena_buckets, arena),
            (preset_buckets, preset),
            (market_type_buckets, market_type),
            (sport_buckets, sport),
        ]:
            if key not in buckets:
                buckets[key] = _empty_bucket()
            _accumulate(buckets[key], row, cls)

        if cls == "unresolved":
            unresolved_trades.append({
                "match_title": row.get("match_title"),
                "outcome_name": row.get("outcome_name"),
                "sport": sport,
                "market_type": market_type,
                "arena": arena,
                "preset_name": preset,
                "game_time": game_time,
                "entry_price": row.get("entry_price"),
                "shares": row.get("shares"),
                "stake": row.get("stake"),
            })

    # Finalize
    def _finalize_all(buckets: dict) -> list[dict]:
        result = []
        for name, b in buckets.items():
            _finalize_bucket(b)
            result.append({"_key": name, **b})
        result.sort(key=lambda r: -(r["net_pnl"]))
        return result

    arena_leaderboard = _finalize_all(arena_buckets)
    preset_leaderboard = _finalize_all(preset_buckets)
    market_type_breakdown = _finalize_all(market_type_buckets)
    sport_breakdown = _finalize_all(sport_buckets)

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
        "resolved_win_rate": total_wins / total_resolved if total_resolved > 0 else None,
        "net_pnl": total_pnl,
    }

    # Data quality warnings
    warnings: list[str] = []
    arena_by_name = {a["_key"]: a for a in arena_leaderboard}
    for arena_name in ALL_ARENAS:
        rt = arena_by_name.get(arena_name, {}).get("resolved_trades", 0)
        if rt < MIN_RESOLVED_PER_ARENA:
            warnings.append(
                f"Arena {arena_name}: {rt} resolved trades (need {MIN_RESOLVED_PER_ARENA})"
            )
    mt_by_name = {m["_key"]: m for m in market_type_breakdown}
    for mt in ALL_MARKET_TYPES:
        rt = mt_by_name.get(mt, {}).get("resolved_trades", 0)
        if rt < MIN_RESOLVED_FOR_MARKET_TYPE:
            warnings.append(
                f"Market type {mt}: {rt} resolved trades (need {MIN_RESOLVED_FOR_MARKET_TYPE})"
            )
    calendar_days = len(game_dates)
    if calendar_days < MIN_CALENDAR_DAYS:
        warnings.append(
            f"Only {calendar_days} calendar days of game data (need {MIN_CALENDAR_DAYS})"
        )

    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "run_ids": sorted(run_ids),
        "arena_leaderboard": arena_leaderboard,
        "preset_leaderboard": preset_leaderboard,
        "market_type_breakdown": market_type_breakdown,
        "sport_breakdown": sport_breakdown,
        "unresolved_trades": sorted(unresolved_trades, key=lambda r: r.get("game_time") or ""),
        "totals": totals,
        "data_quality": {
            "warnings": warnings,
            "calendar_days": calendar_days,
        },
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _pct(v: float | None) -> str:
    return f"{v * 100:.1f}%" if v is not None else "-"


def _money(v: float) -> str:
    return f"${v:+.4f}"


def _edge(v: float | None) -> str:
    if v is None:
        return "-"
    emoji = "🟢" if v > 0.02 else ("🟡" if v > 0 else "🔴")
    return f"{emoji} {v * 100:+.1f}pp"


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def render_sports_markdown(summary: dict[str, Any]) -> str:
    md: list[str] = []

    md.append("# Polymarket Sports Trading Report")
    md.append("")
    md.append(f"> Last updated: {summary['generated_at']}")
    md.append("")

    # Totals
    t = summary["totals"]
    md.append("## Portfolio Summary")
    md.append("")
    md.append(
        f"**Resolved:** {t['resolved_trades']} trades "
        f"({t['resolved_wins']}W / {t['resolved_losses']}L)  "
    )
    md.append(f"**Unresolved:** {t['unresolved']}  ")
    md.append(f"**No Trade:** {t['no_trade']}  ")
    md.append(f"**Overall Win Rate:** {_pct(t['resolved_win_rate'])}  ")
    md.append(f"**Net P/L:** {_money(t['net_pnl'])}  ")
    md.append("")

    # Arena leaderboard
    md.append("## Arena Leaderboard")
    md.append("")
    md.append(
        "| Arena | Resolved | W | L | Unresolved | No Trade "
        "| Win Rate | Breakeven | Edge | Net P/L |"
    )
    md.append(
        "|-------|----------:|--:|--:|-----------:|---------:"
        "|---------:|----------:|-----:|--------:|"
    )
    arena_by_name = {a["_key"]: a for a in summary["arena_leaderboard"]}
    for arena_name in ALL_ARENAS:
        a = arena_by_name.get(arena_name)
        if a:
            md.append(
                f"| {arena_name} | {a['resolved_trades']} | {a['resolved_wins']} "
                f"| {a['resolved_losses']} | {a['unresolved']} | {a['no_trade']} "
                f"| {_pct(a['resolved_win_rate'])} | {_pct(a['breakeven_win_rate'])} "
                f"| {_edge(a['edge'])} | {_money(a['net_pnl'])} |"
            )
        else:
            md.append(f"| {arena_name} | 0 | 0 | 0 | 0 | 0 | - | - | - | $+0.0000 |")
    md.append(
        f"| **TOTAL** | **{t['resolved_trades']}** | **{t['resolved_wins']}** "
        f"| **{t['resolved_losses']}** | **{t['unresolved']}** | **{t['no_trade']}** "
        f"| **{_pct(t['resolved_win_rate'])}** | | | **{_money(t['net_pnl'])}** |"
    )
    md.append("")

    # Preset leaderboard (band_only vs basic per arena)
    md.append("## Preset Leaderboard (band_only vs basic)")
    md.append("")
    md.append(
        "| Preset | Resolved | W | L | Unresolved | No Trade "
        "| Win Rate | Breakeven | Edge | Net P/L |"
    )
    md.append(
        "|--------|----------:|--:|--:|-----------:|---------:"
        "|---------:|----------:|-----:|--------:|"
    )
    for p in sorted(summary["preset_leaderboard"], key=lambda r: r["_key"]):
        md.append(
            f"| {p['_key']} | {p['resolved_trades']} | {p['resolved_wins']} "
            f"| {p['resolved_losses']} | {p['unresolved']} | {p['no_trade']} "
            f"| {_pct(p['resolved_win_rate'])} | {_pct(p['breakeven_win_rate'])} "
            f"| {_edge(p['edge'])} | {_money(p['net_pnl'])} |"
        )
    md.append("")

    # Market type breakdown
    md.append("## Market Type Breakdown")
    md.append("")
    md.append(
        "| Type | Resolved | W | L | Unresolved | Win Rate | Breakeven | Edge | Net P/L |"
    )
    md.append(
        "|------|----------:|--:|--:|-----------:|---------:|----------:|-----:|--------:|"
    )
    mt_order = {t: i for i, t in enumerate(ALL_MARKET_TYPES)}
    mt_sorted = sorted(
        summary["market_type_breakdown"],
        key=lambda r: mt_order.get(r["_key"], 99),
    )
    for m in mt_sorted:
        md.append(
            f"| {m['_key']} | {m['resolved_trades']} | {m['resolved_wins']} "
            f"| {m['resolved_losses']} | {m['unresolved']} "
            f"| {_pct(m['resolved_win_rate'])} | {_pct(m['breakeven_win_rate'])} "
            f"| {_edge(m['edge'])} | {_money(m['net_pnl'])} |"
        )
    md.append("")

    # Sport breakdown
    md.append("## Sport Breakdown")
    md.append("")
    md.append(
        "| Sport | Resolved | W | L | Unresolved | Win Rate | Breakeven | Edge | Net P/L |"
    )
    md.append(
        "|-------|----------:|--:|--:|-----------:|---------:|----------:|-----:|--------:|"
    )
    for s in summary["sport_breakdown"]:
        md.append(
            f"| {s['_key']} | {s['resolved_trades']} | {s['resolved_wins']} "
            f"| {s['resolved_losses']} | {s['unresolved']} "
            f"| {_pct(s['resolved_win_rate'])} | {_pct(s['breakeven_win_rate'])} "
            f"| {_edge(s['edge'])} | {_money(s['net_pnl'])} |"
        )
    md.append("")

    # Open / unresolved positions
    md.append("## Open Positions (Unresolved)")
    md.append("")
    unresolved = summary.get("unresolved_trades", [])
    if unresolved:
        md.append(
            "| Game | Outcome | Sport | Type | Arena | Preset | Game Time | Entry | Stake |"
        )
        md.append(
            "|------|---------|-------|------|-------|--------|-----------|------:|------:|"
        )
        for u in unresolved[:60]:  # cap at 60 rows
            entry = f"{u['entry_price']:.3f}" if u.get("entry_price") is not None else "-"
            stake = f"{u['stake']:.2f}" if u.get("stake") is not None else "-"
            game_time = (u.get("game_time") or "")[:16].replace("T", " ")
            md.append(
                f"| {u.get('match_title', '-')} | {u.get('outcome_name', '-')} "
                f"| {u.get('sport', '-')} | {u.get('market_type', '-')} "
                f"| {u.get('arena', '-')} | {u.get('preset_name', '-')} "
                f"| {game_time} | {entry} | {stake} |"
            )
        if len(unresolved) > 60:
            md.append(f"")
            md.append(f"*... and {len(unresolved) - 60} more*")
    else:
        md.append("No open positions.")
    md.append("")

    # Data quality warnings
    md.append("## Data Quality")
    md.append("")
    warnings = summary.get("data_quality", {}).get("warnings", [])
    calendar_days = summary.get("data_quality", {}).get("calendar_days", 0)
    md.append(f"**Calendar days of data:** {calendar_days} / {MIN_CALENDAR_DAYS} required")
    md.append("")
    if warnings:
        md.append("**Warnings (insufficient data for reliable conclusions):**")
        md.append("")
        for w in warnings:
            md.append(f"- ⚠️ {w}")
    else:
        md.append("✅ All data quality gates passed.")
    md.append("")

    return "\n".join(md)
