from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path


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


replay = _load_module(
    "scripts.polymarket_universe_replay_backtest",
    ROOT / "scripts" / "polymarket_universe_replay_backtest.py",
)


def _event(ts: str, *, bid: float, ask: float, asset_id: str = "asset-up") -> dict[str, object]:
    return {
        "t": "delta",
        "ts": ts,
        "token": asset_id,
        "slug": "btc-updown-5m-1778062800",
        "cat": "btc_rounds",
        "bb": str(bid),
        "ba": str(ask),
    }


def test_mid_momentum_backtest_closes_trade_after_hold_period() -> None:
    events = [
        _event("2026-05-10T00:00:00+00:00", bid=0.49, ask=0.51),
        _event("2026-05-10T00:00:10+00:00", bid=0.54, ask=0.56),
        _event("2026-05-10T00:01:10+00:00", bid=0.60, ask=0.62),
    ]

    result = replay.run_backtest(
        events,
        replay.BacktestConfig(
            category="btc_rounds",
            lookback_seconds=10.0,
            entry_move=0.04,
            hold_seconds=60.0,
            max_spread=0.03,
            stake=1.0,
        ),
    )

    assert result.summary["events_seen"] == 3
    assert result.summary["quotes_seen"] == 3
    assert result.summary["closed_trades"] == 1
    assert result.summary["net_pnl"] == 0.04
    assert result.trades[0].entry_price == 0.56
    assert result.trades[0].exit_price == 0.60


def test_iter_jsonl_events_reads_gzip_and_skips_bad_rows(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl.gz"
    rows = [
        _event("2026-05-10T00:00:00+00:00", bid=0.49, ask=0.51),
        "{bad-json",
        _event("2026-05-10T00:00:01+00:00", bid=0.50, ask=0.52),
    ]
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for row in rows:
            if isinstance(row, str):
                f.write(row)
            else:
                f.write(json.dumps(row))
            f.write("\n")

    loaded = list(replay.iter_jsonl_events([path]))

    assert len(loaded) == 2
    assert loaded[0]["token"] == "asset-up"


def test_quote_reconstruction_rejects_incomplete_or_crossed_quotes() -> None:
    config = replay.BacktestConfig(category="btc_rounds")
    base_ts = datetime(2026, 5, 10, tzinfo=UTC)

    assert replay.quote_from_event({"bb": "", "ba": "0.52"}, config=config, ts=base_ts) is None
    assert replay.quote_from_event({"bb": "0.53", "ba": "0.52"}, config=config, ts=base_ts) is None

    quote = replay.quote_from_event(
        {
            "t": "book",
            "ts": "2026-05-10T00:00:00+00:00",
            "token": "token-up",
            "slug": "btc-updown-5m-1778062800",
            "cat": "btc_rounds",
            "bb": 0.49,
            "ba": 0.51,
        },
        config=config,
        ts=base_ts,
    )

    assert quote is not None
    assert quote.mid == 0.50


def test_max_events_caps_processed_rows() -> None:
    events = [
        _event("2026-05-10T00:00:00+00:00", bid=0.49, ask=0.51),
        _event("2026-05-10T00:00:01+00:00", bid=0.50, ask=0.52),
        _event("2026-05-10T00:00:02+00:00", bid=0.51, ask=0.53),
    ]

    result = replay.run_backtest(events, replay.BacktestConfig(category="btc_rounds", max_events=2))

    assert result.summary["events_seen"] == 2
