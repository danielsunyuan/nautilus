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
        assert summary["leaderboard"][0]["strategy_name"] == "entry_95"
        assert summary["leaderboard"][0]["loop"] == "QUANT"
        assert summary["leaderboard"][0]["rounds"] == 2
        assert summary["leaderboard"][0]["trades"] == 2
        assert summary["leaderboard"][0]["target_exits"] == 1
        assert summary["leaderboard"][0]["stop_losses"] == 1
        assert summary["leaderboard"][0]["avg_entry_price"] == 0.955
        assert summary["leaderboard"][0]["net_pnl"] == 1.0
        assert summary["leaderboard"][0]["roi"] == 0.04
        assert summary["leaderboard"][1]["strategy_name"] == "support_ratio_95"
        assert summary["leaderboard"][1]["trades"] == 0
        assert summary["leaderboard"][1]["no_trade"] == 1
        assert summary["data_quality"]["provisional_metrics_present"] is True
        assert "support_ratio_95" in summary["data_quality"]["provisional_strategies"]


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
    assert "| 1 | QUANT | entry_95 | 2 | 2 | 1 | 1 | 0 | 1 | 1 | 0 | 0 | 50% | 0.955 | $+1.0000 | 4.0% |" in markdown
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
        assert json.loads(latest.read_text(encoding="utf-8"))["schema_version"] == "1"
        assert paths["results_markdown"].read_text(encoding="utf-8").startswith(
            "# Polymarket 5m Crypto Paper Trading — Nautilus Results"
        )
