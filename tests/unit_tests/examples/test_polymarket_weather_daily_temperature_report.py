from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


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


report_mod = _load_module(
    "examples.live.polymarket.weather_daily_temperature_report",
    ROOT / "examples" / "live" / "polymarket" / "weather_daily_temperature_report.py",
)


# ---------------------------------------------------------------------------
# Sample JSONL rows
# ---------------------------------------------------------------------------

def _resolved_win_70c() -> dict:
    """Resolved win in temp_70c arena."""
    return {
        "run_id": "run-001",
        "event": "strategy_result",
        "market_slug": "nyc-high-temp-70-2026-04-15",
        "asset_class": "weather",
        "weather_market_type": "daily_temperature",
        "city": "New York",
        "observation_date": "2026-04-15",
        "temperature_metric": "high",
        "threshold_f": 70.0,
        "token_side": "yes",
        "arena": "temp_70c",
        "strategy_name": "temp_70c_basic",
        "entry_price": 0.72,
        "exit_price": 1.0,
        "settlement_price": 1.0,
        "shares": 10.0,
        "stake": 7.2,
        "pnl": 2.8,
        "resolved_outcome": "win",
        "resolved": True,
        "timestamp": "2026-04-15T00:00:00+00:00",
    }


def _resolved_win_80c() -> dict:
    """Resolved win in temp_80c arena (different arena from first win)."""
    return {
        "run_id": "run-002",
        "event": "strategy_result",
        "market_slug": "chi-high-temp-80-2026-04-15",
        "asset_class": "weather",
        "weather_market_type": "daily_temperature",
        "city": "Chicago",
        "observation_date": "2026-04-15",
        "temperature_metric": "high",
        "threshold_f": 80.0,
        "token_side": "yes",
        "arena": "temp_80c",
        "strategy_name": "temp_80c_basic",
        "entry_price": 0.85,
        "exit_price": 1.0,
        "settlement_price": 1.0,
        "shares": 5.0,
        "stake": 4.25,
        "pnl": 0.75,
        "resolved_outcome": "win",
        "resolved": True,
        "timestamp": "2026-04-15T01:00:00+00:00",
    }


def _resolved_loss_70c() -> dict:
    """Resolved loss in temp_70c arena."""
    return {
        "run_id": "run-003",
        "event": "strategy_result",
        "market_slug": "nyc-high-temp-70-2026-04-16",
        "asset_class": "weather",
        "weather_market_type": "daily_temperature",
        "city": "New York",
        "observation_date": "2026-04-16",
        "temperature_metric": "high",
        "threshold_f": 70.0,
        "token_side": "yes",
        "arena": "temp_70c",
        "strategy_name": "temp_70c_basic",
        "entry_price": 0.75,
        "exit_price": 0.0,
        "settlement_price": 0.0,
        "shares": 8.0,
        "stake": 6.0,
        "pnl": -6.0,
        "resolved_outcome": "loss",
        "resolved": True,
        "timestamp": "2026-04-16T00:00:00+00:00",
    }


def _unresolved_70c() -> dict:
    """Unresolved trade in temp_70c arena."""
    return {
        "run_id": "run-004",
        "event": "strategy_result",
        "market_slug": "nyc-high-temp-70-2026-04-17",
        "asset_class": "weather",
        "weather_market_type": "daily_temperature",
        "city": "New York",
        "observation_date": "2026-04-17",
        "temperature_metric": "high",
        "threshold_f": 70.0,
        "token_side": "yes",
        "arena": "temp_70c",
        "strategy_name": "temp_70c_basic",
        "entry_price": 0.71,
        "exit_price": None,
        "settlement_price": None,
        "shares": 6.0,
        "stake": 4.26,
        "pnl": None,
        "resolved_outcome": None,
        "resolved": False,
        "timestamp": "2026-04-17T00:00:00+00:00",
    }


def _no_trade_70c() -> dict:
    """No-trade row: strategy_result with no entry."""
    return {
        "run_id": "run-005",
        "event": "strategy_result",
        "market_slug": "nyc-high-temp-70-2026-04-18",
        "asset_class": "weather",
        "weather_market_type": "daily_temperature",
        "city": "New York",
        "observation_date": "2026-04-18",
        "temperature_metric": "high",
        "threshold_f": 70.0,
        "token_side": "yes",
        "arena": "temp_70c",
        "strategy_name": "temp_70c_basic",
        "entry_price": None,
        "exit_price": None,
        "settlement_price": None,
        "shares": None,
        "stake": None,
        "pnl": None,
        "resolved_outcome": "no_trade",
        "resolved": False,
        "timestamp": "2026-04-18T00:00:00+00:00",
    }


def _all_sample_rows() -> list[dict]:
    return [
        _resolved_win_70c(),
        _resolved_win_80c(),
        _resolved_loss_70c(),
        _unresolved_70c(),
        _no_trade_70c(),
    ]


# ---------------------------------------------------------------------------
# Tests for build_weather_temperature_summary
# ---------------------------------------------------------------------------

class TestBuildWeatherTemperatureSummary:

    def test_win_rate_excludes_unresolved_and_no_trade(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)

        # Overall: 2 wins, 1 loss => win rate = 2/3
        totals = summary["totals"]
        assert totals["resolved_wins"] == 2
        assert totals["resolved_losses"] == 1
        assert totals["unresolved"] == 1
        assert totals["no_trade"] == 1
        # denominator is 3, NOT 5
        assert abs(totals["resolved_win_rate"] - 2.0 / 3.0) < 1e-6

    def test_arena_leaderboard_includes_breakeven_win_rate(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)

        arena_board = {a["arena"]: a for a in summary["arena_leaderboard"]}
        # temp_70c: 1 win, 1 loss. entry_prices=[0.72, 0.75], avg=0.735
        t70 = arena_board["temp_70c"]
        assert t70["resolved_wins"] == 1
        assert t70["resolved_losses"] == 1
        assert abs(t70["resolved_win_rate"] - 0.5) < 1e-6
        assert abs(t70["breakeven_win_rate"] - 0.735) < 1e-6
        assert abs(t70["edge"] - (0.5 - 0.735)) < 1e-6

        # temp_80c: 1 win, 0 loss. entry_prices=[0.85], avg=0.85
        t80 = arena_board["temp_80c"]
        assert t80["resolved_wins"] == 1
        assert t80["resolved_losses"] == 0
        assert abs(t80["resolved_win_rate"] - 1.0) < 1e-6
        assert abs(t80["breakeven_win_rate"] - 0.85) < 1e-6
        assert abs(t80["edge"] - (1.0 - 0.85)) < 1e-6

    def test_net_pnl_sums_resolved_only(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)

        # resolved pnl: 2.8 + 0.75 + (-6.0) = -2.45
        assert abs(summary["totals"]["net_pnl"] - (-2.45)) < 1e-6

    def test_strategy_leaderboard_groups_by_strategy(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)

        strat_board = {s["strategy_name"]: s for s in summary["strategy_leaderboard"]}
        assert "temp_70c_basic" in strat_board
        assert "temp_80c_basic" in strat_board

        s70 = strat_board["temp_70c_basic"]
        assert s70["resolved_wins"] == 1
        assert s70["resolved_losses"] == 1
        assert s70["unresolved"] == 1
        assert s70["no_trade"] == 1

    def test_city_breakdown(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)

        city_board = {c["city"]: c for c in summary["city_breakdown"]}
        assert "New York" in city_board
        assert "Chicago" in city_board
        ny = city_board["New York"]
        assert ny["resolved_wins"] == 1
        assert ny["resolved_losses"] == 1

    def test_unresolved_trades_section(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)

        assert len(summary["unresolved_trades"]) == 1
        assert summary["unresolved_trades"][0]["market_slug"] == "nyc-high-temp-70-2026-04-17"

    def test_data_quality_warnings_low_trade_count(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)

        warnings = summary["data_quality"]["warnings"]
        # Both arenas have far fewer than 100 resolved trades
        arena_warnings = [w for w in warnings if "temp_70c" in w or "temp_80c" in w]
        assert len(arena_warnings) >= 2


# ---------------------------------------------------------------------------
# Tests for render_weather_temperature_markdown
# ---------------------------------------------------------------------------

class TestRenderWeatherTemperatureMarkdown:

    def test_markdown_contains_all_arena_names(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)
        md = report_mod.render_weather_temperature_markdown(summary)

        assert "temp_70c" in md
        assert "temp_80c" in md
        # Arena names that must appear in the report template header/docs
        for arena_name in ["temp_50c", "temp_60c", "temp_70c", "temp_80c", "temp_90c"]:
            assert arena_name in md

    def test_markdown_has_last_updated(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)
        md = report_mod.render_weather_temperature_markdown(summary)

        assert "Last updated" in md

    def test_markdown_has_arena_leaderboard_table(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)
        md = report_mod.render_weather_temperature_markdown(summary)

        assert "Arena" in md
        assert "Breakeven" in md
        assert "Edge" in md

    def test_markdown_has_strategy_leaderboard(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)
        md = report_mod.render_weather_temperature_markdown(summary)

        assert "Strategy Leaderboard" in md

    def test_markdown_has_unresolved_section(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)
        md = report_mod.render_weather_temperature_markdown(summary)

        assert "Unresolved" in md

    def test_markdown_has_data_quality_warnings(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)
        md = report_mod.render_weather_temperature_markdown(summary)

        assert "Data Quality" in md or "Warning" in md
        assert "minimum 100" in md.lower() or "minimum 100" in md

    def test_markdown_includes_run_files(self) -> None:
        rows = _all_sample_rows()
        summary = report_mod.build_weather_temperature_summary(rows)
        md = report_mod.render_weather_temperature_markdown(summary)

        assert "Run Files" in md or "run_id" in md.lower() or "run-001" in md


# ---------------------------------------------------------------------------
# Tests for merge_entries_with_settlements
# ---------------------------------------------------------------------------

class TestMergeEntriesWithSettlements:

    def test_merge_settlement_overrides_entry(self) -> None:
        """A settlement_update for the same market+strategy overrides resolution fields."""
        entry = {
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-70-2026-04-20",
            "strategy_name": "temp_70c_basic",
            "arena": "temp_70c",
            "city": "New York",
            "observation_date": "2026-04-20",
            "entry_price": 0.60,
            "shares": 10.0,
            "stake": 6.0,
            "resolved": False,
            "resolved_outcome": None,
            "settlement_price": None,
            "pnl": None,
        }
        settlement = {
            "event": "settlement_update",
            "market_slug": "nyc-high-temp-70-2026-04-20",
            "strategy_name": "temp_70c_basic",
            "resolved": True,
            "resolved_outcome": "win",
            "settlement_price": 1.0,
            "pnl": 4.0,
        }
        merged = report_mod.merge_entries_with_settlements([entry, settlement])
        # Should return only strategy_result rows
        assert len(merged) == 1
        row = merged[0]
        assert row["resolved"] is True
        assert row["resolved_outcome"] == "win"
        assert row["settlement_price"] == 1.0
        assert row["pnl"] == 4.0

    def test_merge_no_settlement_leaves_unresolved(self) -> None:
        """An entry with no matching settlement stays unresolved."""
        entry = {
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-70-2026-04-21",
            "strategy_name": "temp_70c_basic",
            "resolved": False,
            "resolved_outcome": None,
            "settlement_price": None,
            "pnl": None,
        }
        merged = report_mod.merge_entries_with_settlements([entry])
        assert len(merged) == 1
        assert merged[0]["resolved"] is False
        assert merged[0]["resolved_outcome"] is None

    def test_merge_ignores_non_strategy_events(self) -> None:
        """Non-strategy_result events pass through unchanged."""
        rows = [
            {"event": "round_start", "market_slug": "abc"},
            {"event": "market_discovered", "market_slug": "def"},
            {"event": "strategy_result", "market_slug": "ghi", "strategy_name": "s1",
             "resolved": False, "pnl": None, "settlement_price": None, "resolved_outcome": None},
        ]
        merged = report_mod.merge_entries_with_settlements(rows)
        # All rows come back; non-strategy events are untouched
        assert len(merged) == 3
        assert merged[0]["event"] == "round_start"
        assert merged[1]["event"] == "market_discovered"
        assert merged[2]["event"] == "strategy_result"

    def test_summary_with_settlements_computes_win_rate(self) -> None:
        """Integration: 2 entries + 2 settlements (1 win, 1 loss) => win_rate = 0.5."""
        entry_a = {
            "run_id": "run-100",
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-70-2026-04-20",
            "strategy_name": "temp_70c_basic",
            "arena": "temp_70c",
            "city": "New York",
            "observation_date": "2026-04-20",
            "entry_price": 0.60,
            "shares": 10.0,
            "stake": 6.0,
            "resolved": False,
            "resolved_outcome": None,
            "settlement_price": None,
            "pnl": None,
        }
        entry_b = {
            "run_id": "run-100",
            "event": "strategy_result",
            "market_slug": "chi-high-temp-70-2026-04-20",
            "strategy_name": "temp_70c_basic",
            "arena": "temp_70c",
            "city": "Chicago",
            "observation_date": "2026-04-20",
            "entry_price": 0.55,
            "shares": 10.0,
            "stake": 5.5,
            "resolved": False,
            "resolved_outcome": None,
            "settlement_price": None,
            "pnl": None,
        }
        settle_a = {
            "event": "settlement_update",
            "market_slug": "nyc-high-temp-70-2026-04-20",
            "strategy_name": "temp_70c_basic",
            "resolved": True,
            "resolved_outcome": "win",
            "settlement_price": 1.0,
            "pnl": 4.0,
        }
        settle_b = {
            "event": "settlement_update",
            "market_slug": "chi-high-temp-70-2026-04-20",
            "strategy_name": "temp_70c_basic",
            "resolved": True,
            "resolved_outcome": "loss",
            "settlement_price": 0.0,
            "pnl": -5.5,
        }
        rows = [entry_a, entry_b, settle_a, settle_b]
        summary = report_mod.build_weather_temperature_summary(rows)
        totals = summary["totals"]
        assert totals["resolved_wins"] == 1
        assert totals["resolved_losses"] == 1
        assert totals["resolved_trades"] == 2
        assert abs(totals["resolved_win_rate"] - 0.5) < 1e-6


# ---------------------------------------------------------------------------
# New tests: ledger attribution, duplicate keys, TP classification, arenas
# ---------------------------------------------------------------------------


class TestNormalizeStrategyName:

    def test_uses_strategy_name_when_present(self) -> None:
        row = {"strategy_name": "temp_70c_basic", "preset_name": "other", "strategy_type": "basic"}
        assert report_mod.normalize_strategy_name(row) == "temp_70c_basic"

    def test_falls_back_to_preset_name_when_no_strategy_name(self) -> None:
        """Confirmed-entry rows have preset_name but no strategy_name."""
        row = {"preset_name": "confirmed_a1", "strategy_type": "basic"}
        assert report_mod.normalize_strategy_name(row) == "confirmed_a1"

    def test_falls_back_to_strategy_type_when_no_preset_name(self) -> None:
        """Old rows that only have strategy_type are attributed correctly."""
        row = {"strategy_type": "band_only"}
        assert report_mod.normalize_strategy_name(row) == "band_only"

    def test_returns_unknown_when_all_absent(self) -> None:
        row = {"arena": "temp_70c"}
        assert report_mod.normalize_strategy_name(row) == "unknown"

    def test_leaderboard_groups_confirmed_row_under_preset_name(self) -> None:
        """A strategy_result row with only preset_name appears under that name in
        the strategy leaderboard (not under 'unknown')."""
        row = {
            "run_id": "run-conf-001",
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-90-2026-04-20",
            "arena": "temp_confirmed",
            "city": "New York",
            "observation_date": "2026-04-20",
            "token_side": "yes",
            # No strategy_name — only preset_name (confirmed-entry daemon pattern)
            "preset_name": "confirmed_a1",
            "entry_price": 0.90,
            "shares": 5.0,
            "stake": 4.5,
            "pnl": 0.5,
            "settlement_price": 1.0,
            "resolved_outcome": "win",
            "resolved": True,
        }
        summary = report_mod.build_weather_temperature_summary([row])
        strat_names = [s["strategy_name"] for s in summary["strategy_leaderboard"]]
        assert "confirmed_a1" in strat_names
        assert "unknown" not in strat_names


class TestDuplicateEntryIds:

    def test_two_entries_same_market_different_times_both_survive(self) -> None:
        """Two strategy_result rows for the same market but at different entry_times
        must not overwrite each other during merge — both must appear in the output."""
        entry_a = {
            "run_id": "run-dup-001",
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-70-2026-04-20",
            "strategy_name": "temp_70c_basic",
            "arena": "temp_70c",
            "city": "New York",
            "observation_date": "2026-04-20",
            "token_side": "yes",
            "entry_price": 0.60,
            "shares": 10.0,
            "stake": 6.0,
            "resolved": False,
            "resolved_outcome": None,
            "settlement_price": None,
            "pnl": None,
            "entry_time": "2026-04-20T08:00:00Z",
        }
        entry_b = {
            "run_id": "run-dup-002",
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-70-2026-04-20",
            "strategy_name": "temp_70c_basic",
            "arena": "temp_70c",
            "city": "New York",
            "observation_date": "2026-04-20",
            "token_side": "yes",
            "entry_price": 0.65,
            "shares": 8.0,
            "stake": 5.2,
            "resolved": False,
            "resolved_outcome": None,
            "settlement_price": None,
            "pnl": None,
            "entry_time": "2026-04-20T14:00:00Z",
        }
        merged = report_mod.merge_entries_with_settlements([entry_a, entry_b])
        assert len(merged) == 2, "Both entries must survive the merge"

    def test_two_entries_same_market_different_entry_ids_both_survive(self) -> None:
        """Explicit entry_id fields prevent key collision."""
        entry_a = {
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-70-2026-04-20",
            "strategy_name": "temp_70c_basic",
            "entry_id": "eid-001",
            "shares": 10.0,
            "resolved": False,
            "pnl": None,
            "settlement_price": None,
            "resolved_outcome": None,
        }
        entry_b = {
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-70-2026-04-20",
            "strategy_name": "temp_70c_basic",
            "entry_id": "eid-002",
            "shares": 8.0,
            "resolved": False,
            "pnl": None,
            "settlement_price": None,
            "resolved_outcome": None,
        }
        merged = report_mod.merge_entries_with_settlements([entry_a, entry_b])
        assert len(merged) == 2, "Different entry_ids must keep both entries"


class TestTakeProfitClassification:

    def test_tp_exit_settlement_price_095_classified_as_win(self) -> None:
        """A take-profit exit at 0.95 with positive pnl must be classified as 'win',
        not 'loss' (the old oracle-only check would wrongly score this as a loss)."""
        row = {
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-90-2026-04-20",
            "arena": "temp_90c",
            "city": "New York",
            "observation_date": "2026-04-20",
            "token_side": "yes",
            "strategy_name": "temp_90c_basic",
            "entry_price": 0.80,
            "shares": 10.0,
            "stake": 8.0,
            "pnl": 1.0,
            "settlement_price": 0.95,
            "resolved_outcome": "win",
            "resolved": True,
            "exit_method": "take_profit",
        }
        result = report_mod._classify_row(row)
        assert result == "win", f"Expected 'win', got '{result}'"

    def test_tp_exit_mid_price_pnl_positive_classified_as_win(self) -> None:
        """Mid-market exit (settlement_price between 0.01 and 0.99) with positive pnl
        is a win even without an explicit exit_method."""
        row = {
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-90-2026-04-20",
            "arena": "temp_90c",
            "city": "New York",
            "observation_date": "2026-04-20",
            "token_side": "yes",
            "strategy_name": "temp_90c_basic",
            "entry_price": 0.80,
            "shares": 10.0,
            "stake": 8.0,
            "pnl": 1.5,
            "settlement_price": 0.95,
            "resolved_outcome": "win",
            "resolved": True,
        }
        result = report_mod._classify_row(row)
        assert result == "win", f"Expected 'win', got '{result}'"

    def test_tp_exit_pnl_negative_classified_as_loss(self) -> None:
        """A stop-loss exit with negative pnl at 0.40 is classified as 'loss'."""
        row = {
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-90-2026-04-20",
            "arena": "temp_90c",
            "city": "New York",
            "observation_date": "2026-04-20",
            "token_side": "yes",
            "strategy_name": "temp_90c_basic",
            "entry_price": 0.80,
            "shares": 10.0,
            "stake": 8.0,
            "pnl": -4.0,
            "settlement_price": 0.40,
            "resolved_outcome": "loss",
            "resolved": True,
            "exit_method": "stop_loss",
        }
        result = report_mod._classify_row(row)
        assert result == "loss", f"Expected 'loss', got '{result}'"


class TestArenaRendering:

    def test_temp_90c_no_appears_in_arena_table(self) -> None:
        """temp_90c_no trades must render in the arena leaderboard table."""
        row = {
            "run_id": "run-90no-001",
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-90-no-2026-04-20",
            "arena": "temp_90c_no",
            "strategy_name": "temp_90c_no_basic",
            "city": "New York",
            "observation_date": "2026-04-20",
            "token_side": "no",
            "entry_price": 0.10,
            "shares": 5.0,
            "stake": 0.5,
            "pnl": 4.5,
            "settlement_price": 1.0,
            "resolved_outcome": "win",
            "resolved": True,
        }
        summary = report_mod.build_weather_temperature_summary([row])
        md = report_mod.render_weather_temperature_markdown(summary)
        assert "temp_90c_no" in md

    def test_temp_confirmed_appears_in_arena_table(self) -> None:
        """temp_confirmed trades (confirmed-entry daemon) must render in the table."""
        row = {
            "run_id": "run-conf-002",
            "event": "strategy_result",
            "market_slug": "nyc-high-temp-90-2026-04-21",
            "arena": "temp_confirmed",
            "strategy_name": "confirmed_a1",
            "city": "New York",
            "observation_date": "2026-04-21",
            "token_side": "yes",
            "entry_price": 0.92,
            "shares": 3.0,
            "stake": 2.76,
            "pnl": 0.24,
            "settlement_price": 1.0,
            "resolved_outcome": "win",
            "resolved": True,
        }
        summary = report_mod.build_weather_temperature_summary([row])
        md = report_mod.render_weather_temperature_markdown(summary)
        assert "temp_confirmed" in md

    def test_all_known_arenas_always_render(self) -> None:
        """ALL_ARENAS entries always appear in the arena table even with no data."""
        summary = report_mod.build_weather_temperature_summary([])
        md = report_mod.render_weather_temperature_markdown(summary)
        for arena_name in report_mod.ALL_ARENAS:
            assert arena_name in md, f"Arena '{arena_name}' missing from report"
