from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def _without_repo_root_on_sys_path():
    original = list(sys.path)
    sys.path = [
        entry
        for entry in original
        if Path(entry or ".").resolve() != ROOT
    ]
    try:
        yield
    finally:
        sys.path = original


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        with _without_repo_root_on_sys_path():
            spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


reporting = _load_module(
    "examples.live.polymarket.polymarket_crypto_5m_reporting",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_crypto_5m_reporting.py",
)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")


def test_discover_run_paths_returns_sorted_overnight_files_only() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_jsonl(root / "polymarket" / "runs" / "overnight_quant_20260414T120500Z.jsonl", [])
        _write_jsonl(root / "polymarket" / "runs" / "overnight_flow_20260414T120000Z.jsonl", [])
        _write_jsonl(root / "polymarket" / "runs" / "debug.jsonl", [])

        paths = reporting.discover_run_paths(root)

        assert [path.name for path in paths] == [
            "overnight_flow_20260414T120000Z.jsonl",
            "overnight_quant_20260414T120500Z.jsonl",
        ]


def test_build_summary_aggregates_leaderboard_totals_and_data_quality() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        run_dir = root / "polymarket" / "runs"
        _write_jsonl(
            run_dir / "overnight_quant_20260414T120000Z.jsonl",
            [
                {
                    "event": "round_start",
                    "run_id": "run-1",
                    "session_id": "btc-1:1",
                    "slug": "btc-updown-5m-1",
                },
                {
                    "event": "strategy_result",
                    "run_id": "run-1",
                    "session_id": "btc-1:1",
                    "strategy_name": "entry_95",
                    "entry_price": 0.95,
                    "entry_time": "2026-04-14T12:01:00Z",
                    "exit_reason": "target_exit",
                    "pnl": 1.5,
                    "roi": 0.10,
                    "stake": 15.0,
                    "settled": False,
                },
                {
                    "event": "strategy_result",
                    "run_id": "run-1",
                    "session_id": "btc-1:1",
                    "strategy_name": "support_ratio_95",
                    "entry_price": 0.94,
                    "entry_time": None,
                    "exit_reason": "daemon_window_complete",
                    "pnl": None,
                    "roi": None,
                    "stake": None,
                    "settled": False,
                },
                {
                    "event": "round_end",
                    "run_id": "run-1",
                    "session_id": "btc-1:1",
                    "slug": "btc-updown-5m-1",
                },
            ],
        )
        _write_jsonl(
            run_dir / "overnight_quant_20260414T120500Z.jsonl",
            [
                {
                    "event": "round_start",
                    "run_id": "run-2",
                    "session_id": "btc-2:1",
                    "slug": "btc-updown-5m-2",
                },
                {
                    "event": "strategy_result",
                    "run_id": "run-2",
                    "session_id": "btc-2:1",
                    "strategy_name": "entry_95",
                    "entry_price": 0.96,
                    "entry_time": "2026-04-14T12:06:00Z",
                    "exit_reason": "stop_loss_exit",
                    "pnl": -0.5,
                    "roi": -0.05,
                    "stake": 10.0,
                    "settled": False,
                },
                {
                    "event": "round_skipped",
                    "run_id": "run-2",
                    "session_id": "btc-2:1",
                    "reason": "gamma unavailable",
                },
            ],
        )

        summary = reporting.build_summary(
            report_root=root,
            now=datetime(2026, 4, 14, 13, 0, tzinfo=UTC),
        )

        assert summary["schema_version"] == "1"
        assert summary["report_info"]["source_dir"].endswith("polymarket/runs")
        assert summary["totals"]["sessions"] == 2
        assert summary["totals"]["rounds"] == 2
        assert summary["totals"]["trades"] == 2
        assert summary["totals"]["wins"] == 1
        assert summary["totals"]["losses"] == 1
        assert summary["totals"]["rounds_skipped"] == 1
        assert summary["totals"]["net_pnl"] == 1.0
        assert summary["totals"]["total_stake"] == 25.0
        assert summary["leaderboard"][0]["strategy_name"] == "entry_95"
        assert summary["leaderboard"][0]["loop"] == "QUANT"
        assert summary["leaderboard"][0]["rounds"] == 2
        assert summary["leaderboard"][0]["trades"] == 2
        assert summary["leaderboard"][0]["target_exits"] == 1
        assert summary["leaderboard"][0]["stop_losses"] == 1
        assert summary["leaderboard"][0]["avg_entry_price"] == 0.955
        assert summary["leaderboard"][0]["net_pnl"] == 1.0
        assert summary["leaderboard"][0]["total_stake"] == 25.0
        assert summary["leaderboard"][0]["roi"] == 0.04
        assert summary["leaderboard"][1]["strategy_name"] == "support_ratio_95"
        assert summary["leaderboard"][1]["trades"] == 0
        assert summary["leaderboard"][1]["no_trade"] == 1
        assert summary["data_quality"]["provisional_metrics_present"] is True
        assert "support_ratio_95" in summary["data_quality"]["provisional_strategies"]


def test_build_summary_counts_round_skipped_file_without_round_start() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        run_dir = root / "polymarket" / "runs"
        _write_jsonl(
            run_dir / "overnight_quant_20260414T120000Z.jsonl",
            [
                {
                    "event": "error",
                    "run_id": "run-1",
                    "reason": "gamma unavailable",
                },
                {
                    "event": "round_skipped",
                    "run_id": "run-1",
                    "reason": "gamma unavailable",
                },
            ],
        )

        summary = reporting.build_summary(
            report_root=root,
            now=datetime(2026, 4, 14, 13, 0, tzinfo=UTC),
        )

        assert summary["totals"]["sessions"] == 1
        assert summary["totals"]["rounds"] == 1
        assert summary["totals"]["rounds_skipped"] == 1
        assert summary["sessions"][0]["rounds"] == 1


def test_build_summary_excludes_open_and_invalid_accounting_from_realized_pnl() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        run_dir = root / "polymarket" / "runs"
        _write_jsonl(
            run_dir / "overnight_edge_20260415T124236Z.jsonl",
            [
                {
                    "event": "round_start",
                    "session_id": "btc-1:1",
                    "slug": "btc-updown-5m-1",
                },
                {
                    "event": "strategy_result",
                    "session_id": "btc-1:1",
                    "strategy_name": "edge_pullback_75_tight",
                    "accounting_status": "open",
                    "entry_price": 0.77,
                    "entry_time": "2026-04-15T12:49:04Z",
                    "exit_reason": "position_open",
                    "pnl": -1.165356,
                    "roi": -0.15,
                    "stake": 7.7,
                    "settled": False,
                },
                {
                    "event": "strategy_result",
                    "session_id": "btc-1:1",
                    "strategy_name": "edge_pullback_70_tight",
                    "accounting_status": "invalid_entry_side",
                    "entry_side": "sell",
                    "entry_price": 0.96,
                    "entry_time": "2026-04-15T13:08:46Z",
                    "exit_reason": "accounting_invalid",
                    "pnl": -0.7448,
                    "roi": -0.01,
                    "stake": 4.8,
                    "settled": False,
                },
                {
                    "event": "strategy_result",
                    "session_id": "btc-1:1",
                    "strategy_name": "edge_pullback_70_tight",
                    "accounting_status": "settled",
                    "entry_in_configured_band": False,
                    "entry_side": "buy",
                    "entry_price": 0.69,
                    "entry_time": "2026-04-15T23:44:13Z",
                    "exit_reason": "position_closed",
                    "pnl": -17.425768,
                    "roi": -0.057,
                    "stake": 88.0992,
                    "settled": True,
                },
                {
                    "event": "strategy_result",
                    "session_id": "btc-1:1",
                    "strategy_name": "edge_pullback_70_tight",
                    "accounting_status": "settled",
                    "entry_side": "buy",
                    "entry_price": 0.71,
                    "entry_time": "2026-04-15T13:49:35Z",
                    "exit_reason": "position_closed",
                    "pnl": 1.2976,
                    "roi": 0.35,
                    "stake": 7.1,
                    "settled": True,
                },
            ],
        )

        summary = reporting.build_summary(
            report_root=root,
            now=datetime(2026, 4, 15, 14, 0, tzinfo=UTC),
        )

        totals = summary["totals"]
        assert totals["trades"] == 1
        assert totals["open_positions"] == 1
        assert totals["invalid_results"] == 2
        assert totals["net_pnl"] == 1.2976
        assert totals["total_stake"] == 7.1

        rows = {row["strategy_name"]: row for row in summary["leaderboard"]}
        assert rows["edge_pullback_75_tight"]["trades"] == 0
        assert rows["edge_pullback_75_tight"]["open_positions"] == 1
        assert rows["edge_pullback_75_tight"]["net_pnl"] == 0.0
        assert rows["edge_pullback_70_tight"]["trades"] == 1
        assert rows["edge_pullback_70_tight"]["invalid_results"] == 2
        assert rows["edge_pullback_70_tight"]["net_pnl"] == 1.2976
        assert summary["data_quality"]["invalid_accounting_present"] is True
        assert "edge_pullback_70_tight" in summary["data_quality"]["invalid_accounting_strategies"]


def test_render_results_markdown_formats_summary_view() -> None:
    summary = {
        "generated_at": "2026-04-14T13:00:00Z",
        "sessions": [{"loop": "QUANT", "file": "overnight_quant_20260414T120000Z.jsonl", "rounds": 2}],
        "leaderboard": [
            {
                "rank": 1,
                "loop": "QUANT",
                "strategy_name": "entry_95",
                "rounds": 2,
                "trades": 2,
                "wins": 1,
                "losses": 1,
                "no_trade": 0,
                "open_positions": 0,
                "invalid_results": 0,
                "target_exits": 1,
                "stop_losses": 1,
                "settled_wins": 0,
                "settled_losses": 0,
                "win_rate": 0.5,
                "avg_entry_price": 0.955,
                "net_pnl": 1.0,
                "roi": 0.04,
            }
        ],
        "totals": {
            "trades": 2,
            "wins": 1,
            "losses": 1,
            "no_trade": 0,
            "open_positions": 0,
            "invalid_results": 0,
            "target_exits": 1,
            "stop_losses": 1,
            "settled_wins": 0,
            "settled_losses": 0,
            "win_rate": 0.5,
            "net_pnl": 1.0,
            "roi": 0.04,
            "rounds": 2,
        },
        "notes": ["Metrics may be provisional until daemon trade accounting is expanded."],
        "data_quality": {
            "provisional_metrics_present": True,
            "provisional_strategies": ["entry_95"],
        },
    }

    markdown = reporting.render_results_markdown(summary)

    assert "# Polymarket 5m Crypto Paper Trading — Nautilus Results" in markdown
    assert "| 1 | QUANT | entry_95 | 2 | 2 | 1 | 1 | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 50% | 0.955 | $+1.0000 | 4.0% |" in markdown
    assert "## Notes" in markdown
    assert "Metrics may be provisional until daemon trade accounting is expanded." in markdown


def test_write_report_outputs_persists_json_and_markdown() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        summary = {
            "schema_version": "1",
            "generated_at": "2026-04-14T13:00:00Z",
            "sessions": [],
            "leaderboard": [],
            "totals": {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "no_trade": 0,
                "target_exits": 0,
                "stop_losses": 0,
                "settled_wins": 0,
                "settled_losses": 0,
                "win_rate": 0.0,
                "net_pnl": 0.0,
                "roi": 0.0,
                "rounds": 0,
            },
            "notes": [],
            "data_quality": {
                "provisional_metrics_present": False,
                "provisional_strategies": [],
            },
            "report_info": {
                "source_dir": str(root / "polymarket" / "runs"),
                "source_files": [],
            },
        }

        paths = reporting.write_report_outputs(report_root=root, summary=summary)

        latest = root / "polymarket" / "reports" / "summary_latest.json"
        assert paths["summary_latest"] == latest
        assert paths["summary_timestamped"].name.startswith("summary_")
        assert json.loads(latest.read_text(encoding="utf-8"))["schema_version"] == "1"
        assert json.loads(paths["summary_timestamped"].read_text(encoding="utf-8"))["generated_at"] == "2026-04-14T13:00:00Z"
        assert paths["results_markdown"].read_text(encoding="utf-8").startswith(
            "# Polymarket 5m Crypto Paper Trading — Nautilus Results"
        )


def test_write_report_outputs_rejects_parent_relative_root() -> None:
    with TemporaryDirectory() as tmp:
        summary = {
            "schema_version": "1",
            "generated_at": "2026-04-14T13:00:00Z",
            "sessions": [],
            "leaderboard": [],
            "totals": {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "no_trade": 0,
                "target_exits": 0,
                "stop_losses": 0,
                "settled_wins": 0,
                "settled_losses": 0,
                "win_rate": 0.0,
                "net_pnl": 0.0,
                "roi": 0.0,
                "rounds": 0,
            },
            "notes": [],
            "data_quality": {
                "provisional_metrics_present": False,
                "provisional_strategies": [],
            },
            "report_info": {
                "source_dir": str(Path(tmp) / "polymarket" / "runs"),
                "source_files": [],
            },
        }

        try:
            reporting.write_report_outputs(report_root=Path(tmp) / "..", summary=summary)
        except ValueError as exc:
            assert "report_root" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected report_root validation failure")
