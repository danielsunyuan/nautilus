from __future__ import annotations

import asyncio
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path

from examples.live.polymarket.weather_ensemble_settlement import JsonlRunWriter
from examples.live.polymarket.weather_ensemble_settlement import MarketResolution
from examples.live.polymarket.weather_ensemble_settlement import UnresolvedEntry
from examples.live.polymarket.weather_ensemble_settlement import compute_settlement
from examples.live.polymarket.weather_ensemble_settlement import run_settlement_loop
from examples.live.polymarket.weather_ensemble_settlement import scan_unresolved_entries


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _strategy_row(
    *,
    market_slug: str,
    token_id: str = "tok-1",
    city: str = "Tokyo",
    threshold: float = 19.0,
    token_side: str = "yes",
    timestamp: str = "2026-04-23T07:00:00+00:00",
    strategy_name: str = "weather_ensemble_baseline",
) -> dict:
    return {
        "event": "strategy_result",
        "run_id": "run-1",
        "strategy_name": strategy_name,
        "preset_name": strategy_name,
        "market_slug": market_slug,
        "condition_id": "0xabc",
        "token_id": token_id,
        "city": city,
        "threshold": threshold,
        "band_type": "daily_high_or_higher",
        "forecast_source": "open-meteo-ensemble",
        "model_yes_probability": 0.73,
        "market_yes_price": 0.61,
        "edge": 0.12,
        "selected_side": token_side,
        "token_side": token_side,
        "confidence": 0.73,
        "entry_price": 0.61,
        "shares": 5.0,
        "stake": 3.05,
        "resolved": False,
        "accounting_status": "open",
        "timestamp": timestamp,
    }


def test_scan_unresolved_entries_skips_settled_and_partial_rows(tmp_path: Path) -> None:
    entry = _strategy_row(market_slug="tokyo-19c-apr-23", token_id="tok-1")
    settled_entry = _strategy_row(
        market_slug="busan-18c-apr-23",
        token_id="bus-1",
        city="Busan",
        timestamp="2026-04-23T07:05:00+00:00",
    )
    partial_entry = _strategy_row(market_slug="broken-row", token_id="")
    partial_entry.pop("token_id")

    settlement = {
        "event": "settlement_update",
        "entry_id": "busan-18c-apr-23|2026-04-23T07:05:00+00:00|yes|weather_ensemble_baseline",
        "market_slug": "busan-18c-apr-23",
        "token_id": "bus-1",
        "token_side": "yes",
        "strategy_name": "weather_ensemble_baseline",
        "resolved": True,
        "settlement_price": 1.0,
        "pnl": 1.95,
    }

    _write_jsonl(
        tmp_path / "weather_ensemble_20260423T070000Z.jsonl",
        [
            entry,
            settled_entry,
            partial_entry,
            {"event": "market_snapshot", "market_slug": "tokyo-19c-apr-23"},
        ],
    )
    _write_jsonl(tmp_path / "weather_ensemble_settlement.jsonl", [settlement])

    entries = scan_unresolved_entries(tmp_path)

    assert len(entries) == 1
    assert entries[0].market_slug == "tokyo-19c-apr-23"
    assert entries[0].token_id == "tok-1"


def test_compute_settlement_preserves_forecast_source_and_marks_clob_source() -> None:
    entry = UnresolvedEntry(
        market_slug="tokyo-19c-apr-23",
        token_id="tok-1",
        token_side="yes",
        strategy_name="weather_ensemble_baseline",
        city="Tokyo",
        threshold="19.0",
        band_type="daily_high_or_higher",
        forecast_source="open-meteo-ensemble",
        model_yes_probability=0.73,
        market_yes_price=0.61,
        edge=0.12,
        confidence=0.73,
        entry_price=0.61,
        shares=5.0,
        stake=3.05,
        source_file="weather_ensemble_20260423T070000Z.jsonl",
        entry_time="2026-04-23T07:00:00+00:00",
    )
    resolution = MarketResolution(
        token_id="tok-1",
        resolved=True,
        settlement_price=1.0,
        settlement_source="clob_last_trade_price",
    )

    settlement = compute_settlement(entry, resolution)

    assert settlement is not None
    assert settlement["forecast_source"] == "open-meteo-ensemble"
    assert settlement["settlement_source"] == "clob_last_trade_price"
    assert settlement["resolved_outcome"] == "win"
    assert settlement["pnl"] == 1.95


def test_run_settlement_loop_writes_updates_for_open_entries(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "weather_ensemble_20260423T070000Z.jsonl",
        [_strategy_row(market_slug="tokyo-19c-apr-23", token_id="tok-1")],
    )
    writer = JsonlRunWriter(tmp_path / "weather_ensemble_settlement.jsonl")

    async def fake_fetch(*, token_id: str, **_: object) -> MarketResolution | None:
        if token_id != "tok-1":
            return None
        return MarketResolution(
            token_id="tok-1",
            resolved=True,
            settlement_price=1.0,
            settlement_source="clob_midpoint",
        )

    asyncio.run(
        run_settlement_loop(
            jsonl_dir=tmp_path,
            writer=writer,
            fetch_resolution=fake_fetch,
            poll_interval_seconds=0.0,
            max_iterations=1,
            now_fn=lambda: datetime(2026, 4, 23, 21, 0, 0, tzinfo=UTC),
        ),
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "weather_ensemble_settlement.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["event"] == "settlement_update"
    assert rows[0]["settlement_source"] == "clob_midpoint"
    assert rows[0]["resolved"] is True
