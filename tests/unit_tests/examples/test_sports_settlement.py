from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


mod = _load_module(
    "sports_settlement",
    ROOT / "examples" / "live" / "polymarket" / "sports_settlement.py",
)

UnresolvedEntry = mod.UnresolvedEntry
MarketResolution = mod.MarketResolution
compute_settlement = mod.compute_settlement
scan_unresolved_entries = mod.scan_unresolved_entries


def _entry(*, entry_price: float, shares: float, entry_fee: float | None):
    return UnresolvedEntry(
        market_slug="mlb-nyy-bos-moneyline",
        condition_id="0xabc",
        preset_name="sports_60c_basic",
        arena="sports_60c",
        entry_price=entry_price,
        shares=shares,
        stake=entry_price * shares,
        sport="mlb",
        match_title="New York Yankees vs Boston Red Sox",
        outcome_name="New York Yankees",
        game_time="2026-07-29T23:00:00Z",
        source_file="sports.jsonl",
        entry_fee=entry_fee,
        fee_status="known" if entry_fee is not None else "missing",
    )


def test_compute_settlement_win_reports_gross_fee_and_net_pnl():
    entry = _entry(entry_price=0.60, shares=5.0, entry_fee=0.018)
    resolution = MarketResolution(
        condition_id="0xabc",
        slug=entry.market_slug,
        resolved=True,
        winning_outcome=entry.outcome_name,
        resolution_price=1.0,
    )

    result = compute_settlement(entry, resolution)

    assert result is not None
    assert result["gross_pnl"] == 2.0
    assert result["entry_fee"] == 0.018
    assert result["net_pnl"] == 1.982
    assert result["pnl"] == result["net_pnl"]
    assert result["fee_status"] == "known"


def test_compute_settlement_loss_reports_gross_fee_and_net_pnl():
    entry = _entry(entry_price=0.70, shares=4.0, entry_fee=0.0252)
    resolution = MarketResolution(
        condition_id="0xabc",
        slug=entry.market_slug,
        resolved=True,
        winning_outcome="Boston Red Sox",
        resolution_price=1.0,
    )

    result = compute_settlement(entry, resolution)

    assert result is not None
    assert result["gross_pnl"] == -2.8
    assert result["entry_fee"] == 0.0252
    assert result["net_pnl"] == pytest.approx(-2.8252)
    assert result["pnl"] == result["net_pnl"]
    assert result["fee_status"] == "known"


def test_missing_entry_fee_does_not_claim_net_pnl():
    entry = _entry(entry_price=0.60, shares=5.0, entry_fee=None)
    resolution = MarketResolution(
        condition_id="0xabc",
        slug=entry.market_slug,
        resolved=True,
        winning_outcome=entry.outcome_name,
        resolution_price=1.0,
    )

    result = compute_settlement(entry, resolution)

    assert result is not None
    assert result["gross_pnl"] == 2.0
    assert result["entry_fee"] is None
    assert result["net_pnl"] is None
    assert result["pnl"] is None
    assert result["fee_status"] == "missing"


def test_scan_unresolved_entries_carries_fee_data(tmp_path: Path):
    row = {
        "event": "strategy_result",
        "market_slug": "mlb-nyy-bos-moneyline",
        "condition_id": "0xabc",
        "preset_name": "sports_60c_basic",
        "arena": "sports_60c",
        "entry_price": 0.60,
        "shares": 5.0,
        "stake": 3.0,
        "sport": "mlb",
        "match_title": "New York Yankees vs Boston Red Sox",
        "outcome_name": "New York Yankees",
        "game_time": "2026-07-29T23:00:00Z",
        "resolved": False,
        "accounting_status": "open",
        "entry_fee": 0.018,
        "fee_status": "known",
    }
    (tmp_path / "sports.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    entries = scan_unresolved_entries(tmp_path)

    assert len(entries) == 1
    assert entries[0].entry_fee == 0.018
    assert entries[0].fee_status == "known"
