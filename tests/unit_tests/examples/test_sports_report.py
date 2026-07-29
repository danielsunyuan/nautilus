from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module(
    "sports_report",
    ROOT / "examples" / "live" / "polymarket" / "sports_report.py",
)

build_sports_summary = mod.build_sports_summary
merge_entries_with_settlements = mod.merge_entries_with_settlements
render_sports_markdown = mod.render_sports_markdown


def _strategy_result(*, condition_id: str, preset_name: str = "sports_60c_basic") -> dict:
    return {
        "event": "strategy_result",
        "run_id": "run-001",
        "condition_id": condition_id,
        "outcome_name": "New York Yankees",
        "preset_name": preset_name,
        "arena": "sports_60c",
        "market_type": "moneyline",
        "sport": "mlb",
        "match_title": "New York Yankees vs Boston Red Sox",
        "game_time": "2026-07-29T23:00:00Z",
        "entry_price": 0.60,
        "shares": 5.0,
        "stake": 3.0,
        "accounting_status": "open",
        "resolved": False,
    }


def _settlement(
    *,
    condition_id: str,
    net_pnl: float | None,
    fee_status: str,
    legacy_pnl: float | None = None,
) -> dict:
    row = {
        "event": "settlement_update",
        "condition_id": condition_id,
        "outcome_name": "New York Yankees",
        "preset_name": "sports_60c_basic",
        "resolved": True,
        "resolved_outcome": "win",
        "settlement_price": 1.0,
    }
    if fee_status == "known":
        row.update({
            "gross_pnl": 2.0,
            "entry_fee": 0.02,
            "net_pnl": net_pnl,
            "fee_status": "known",
            "pnl": net_pnl,
        })
    elif legacy_pnl is not None:
        row["pnl"] = legacy_pnl
    return row


def test_merge_preserves_v2_settlement_accounting_fields():
    rows = [
        _strategy_result(condition_id="0xknown"),
        _settlement(condition_id="0xknown", net_pnl=1.98, fee_status="known"),
    ]

    merged = merge_entries_with_settlements(rows)

    assert len(merged) == 1
    assert merged[0]["gross_pnl"] == 2.0
    assert merged[0]["entry_fee"] == 0.02
    assert merged[0]["net_pnl"] == 1.98
    assert merged[0]["fee_status"] == "known"
    assert merged[0]["pnl"] == 1.98


def test_summary_aggregates_only_known_fee_net_pnl_and_counts_missing_fees():
    rows = [
        _strategy_result(condition_id="0xknown"),
        _settlement(condition_id="0xknown", net_pnl=1.98, fee_status="known"),
        _strategy_result(condition_id="0xlegacy"),
        _settlement(
            condition_id="0xlegacy",
            net_pnl=None,
            fee_status="missing",
            legacy_pnl=2.0,
        ),
    ]

    summary = build_sports_summary(rows)

    totals = summary["totals"]
    assert totals["resolved_trades"] == 2
    assert totals["fee_known"] == 1
    assert totals["fee_missing"] == 1
    assert totals["fee_complete"] is False
    assert totals["net_pnl"] == pytest.approx(1.98)

    arena = summary["arena_leaderboard"][0]
    assert arena["fee_known"] == 1
    assert arena["fee_missing"] == 1
    assert arena["net_pnl"] == pytest.approx(1.98)


def test_report_labels_partial_total_and_renders_fee_completeness():
    rows = [
        _strategy_result(condition_id="0xknown"),
        _settlement(condition_id="0xknown", net_pnl=1.98, fee_status="known"),
        _strategy_result(condition_id="0xlegacy"),
        _settlement(
            condition_id="0xlegacy",
            net_pnl=None,
            fee_status="missing",
            legacy_pnl=2.0,
        ),
    ]

    markdown = render_sports_markdown(build_sports_summary(rows))

    assert "**Fee completeness:** 1 / 2 resolved trades (50.0%)" in markdown
    assert "**Known-fee Net P/L:** $+1.9800" in markdown
    assert "**Net P/L:** $+1.9800" not in markdown


def test_report_labels_complete_total_as_net_pnl():
    rows = [
        _strategy_result(condition_id="0xknown"),
        _settlement(condition_id="0xknown", net_pnl=1.98, fee_status="known"),
    ]

    markdown = render_sports_markdown(build_sports_summary(rows))

    assert "**Fee completeness:** 1 / 1 resolved trades (100.0%)" in markdown
    assert "**Net P/L:** $+1.9800" in markdown
