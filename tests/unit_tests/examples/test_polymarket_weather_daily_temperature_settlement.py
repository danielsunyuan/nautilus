from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
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


# Stub out the report module so the settlement module can optionally import it
# without needing the real thing at test time.
_report_mod_name = "weather_daily_temperature_report"
if _report_mod_name not in sys.modules:
    from types import ModuleType

    _stub = ModuleType(_report_mod_name)
    _stub.build_weather_temperature_summary = lambda rows: {}  # type: ignore[attr-defined]
    _stub.render_weather_temperature_markdown = lambda summary: ""  # type: ignore[attr-defined]
    sys.modules[_report_mod_name] = _stub

mod = _load_module(
    "weather_daily_temperature_settlement",
    ROOT / "examples" / "live" / "polymarket" / "weather_daily_temperature_settlement.py",
)

UnresolvedEntry = mod.UnresolvedEntry
MarketResolution = mod.MarketResolution
scan_unresolved_entries = mod.scan_unresolved_entries
compute_settlement = mod.compute_settlement
run_settlement_loop = mod.run_settlement_loop
JsonlRunWriter = mod.JsonlRunWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_strategy_result(
    *,
    market_slug: str = "will-the-high-temperature-nyc-70f",
    condition_id: str = "0xabc123",
    strategy_name: str = "temp_70c_basic",
    arena: str = "temp_70c",
    token_side: str = "yes",
    entry_price: float = 0.72,
    shares: float = 10.0,
    stake: float = 7.2,
    city: str = "New York",
    observation_date: str = "2026-04-15",
    resolved: bool = False,
    resolved_outcome: str | None = None,
    run_id: str = "run-001",
) -> dict:
    row: dict = {
        "event": "strategy_result",
        "run_id": run_id,
        "market_slug": market_slug,
        "condition_id": condition_id,
        "strategy_name": strategy_name,
        "arena": arena,
        "token_side": token_side,
        "entry_price": entry_price,
        "shares": shares,
        "stake": stake,
        "city": city,
        "observation_date": observation_date,
        "resolved": resolved,
    }
    if resolved_outcome is not None:
        row["resolved_outcome"] = resolved_outcome
    return row


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_unresolved_entries_finds_pending(tmp_path: Path):
    """Two unresolved + one already-resolved; scan returns only the two."""
    rows = [
        _make_strategy_result(market_slug="slug-a", condition_id="0xa", resolved=False),
        _make_strategy_result(market_slug="slug-b", condition_id="0xb", resolved=False),
        _make_strategy_result(market_slug="slug-c", condition_id="0xc", resolved=True, resolved_outcome="win"),
    ]
    _write_jsonl(tmp_path / "run.jsonl", rows)

    entries = scan_unresolved_entries(tmp_path)
    assert len(entries) == 2
    slugs = {e.market_slug for e in entries}
    assert slugs == {"slug-a", "slug-b"}


def test_scan_ignores_non_strategy_result_events(tmp_path: Path):
    """Only strategy_result events are scanned; other event types are ignored."""
    rows = [
        {"event": "round_start", "market_slug": "slug-x"},
        {"event": "market_discovered", "market_slug": "slug-y"},
        _make_strategy_result(market_slug="slug-z", condition_id="0xz"),
    ]
    _write_jsonl(tmp_path / "run.jsonl", rows)

    entries = scan_unresolved_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].market_slug == "slug-z"


def test_compute_settlement_win():
    """YES token at 0.72, market resolves YES wins => pnl = +2.8, outcome = win."""
    entry = UnresolvedEntry(
        market_slug="slug-a",
        condition_id="0xa",
        strategy_name="temp_70c_basic",
        arena="temp_70c",
        token_side="yes",
        entry_price=0.72,
        shares=10.0,
        stake=7.2,
        city="New York",
        observation_date="2026-04-15",
        source_file="run.jsonl",
    )
    resolution = MarketResolution(
        condition_id="0xa",
        slug="slug-a",
        resolved=True,
        winning_outcome="Yes",
        resolution_price_yes=1.0,
        resolution_price_no=0.0,
    )
    result = compute_settlement(entry, resolution)
    assert result is not None
    assert result["resolved"] is True
    assert result["resolved_outcome"] == "win"
    assert abs(result["pnl"] - 2.8) < 1e-9
    assert result["settlement_price"] == 1.0


def test_compute_settlement_loss():
    """YES token at 0.72, market resolves NO wins => pnl = -7.2, outcome = loss."""
    entry = UnresolvedEntry(
        market_slug="slug-a",
        condition_id="0xa",
        strategy_name="temp_70c_basic",
        arena="temp_70c",
        token_side="yes",
        entry_price=0.72,
        shares=10.0,
        stake=7.2,
        city="New York",
        observation_date="2026-04-15",
        source_file="run.jsonl",
    )
    resolution = MarketResolution(
        condition_id="0xa",
        slug="slug-a",
        resolved=True,
        winning_outcome="No",
        resolution_price_yes=0.0,
        resolution_price_no=1.0,
    )
    result = compute_settlement(entry, resolution)
    assert result is not None
    assert result["resolved"] is True
    assert result["resolved_outcome"] == "loss"
    assert abs(result["pnl"] - (-7.2)) < 1e-9
    assert result["settlement_price"] == 0.0


def test_compute_settlement_unresolved_returns_none():
    """Market not yet resolved => returns None."""
    entry = UnresolvedEntry(
        market_slug="slug-a",
        condition_id="0xa",
        strategy_name="temp_70c_basic",
        arena="temp_70c",
        token_side="yes",
        entry_price=0.72,
        shares=10.0,
        stake=7.2,
        city="New York",
        observation_date="2026-04-15",
        source_file="run.jsonl",
    )
    resolution = MarketResolution(
        condition_id="0xa",
        slug="slug-a",
        resolved=False,
        winning_outcome=None,
        resolution_price_yes=None,
        resolution_price_no=None,
    )
    result = compute_settlement(entry, resolution)
    assert result is None


def test_settlement_loop_processes_and_exits(tmp_path: Path):
    """Loop resolves one market, writes settlement_update, exits after max_iterations=1."""
    rows = [
        _make_strategy_result(market_slug="slug-a", condition_id="0xa"),
    ]
    _write_jsonl(tmp_path / "run.jsonl", rows)

    writer = JsonlRunWriter(tmp_path / "settlement.jsonl")

    resolution = MarketResolution(
        condition_id="0xa",
        slug="slug-a",
        resolved=True,
        winning_outcome="Yes",
        resolution_price_yes=1.0,
        resolution_price_no=0.0,
    )

    async def fake_fetch(*, condition_id, **kwargs):
        if condition_id == "0xa":
            return resolution
        return None

    fixed_now = datetime(2026, 4, 16, 6, 0, 0, tzinfo=UTC)

    asyncio.get_event_loop().run_until_complete(
        run_settlement_loop(
            jsonl_dir=tmp_path,
            writer=writer,
            fetch_resolution=fake_fetch,
            poll_interval_seconds=0.0,
            max_iterations=1,
            now_fn=lambda: fixed_now,
        )
    )

    # Verify settlement_update was written
    lines = (tmp_path / "settlement.jsonl").read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]
    settlement_events = [e for e in events if e.get("event") == "settlement_update"]
    assert len(settlement_events) == 1
    ev = settlement_events[0]
    assert ev["condition_id"] == "0xa"
    assert ev["resolved"] is True
    assert ev["resolved_outcome"] == "win"
    assert abs(ev["pnl"] - 2.8) < 1e-9


def test_settlement_loop_skips_already_settled(tmp_path: Path):
    """Markets that already have a settlement_update in JSONL are not re-queried."""
    # Write an unresolved strategy_result
    _write_jsonl(tmp_path / "run.jsonl", [
        _make_strategy_result(market_slug="slug-a", condition_id="0xa"),
    ])
    # Write a settlement_update for it already
    _write_jsonl(tmp_path / "settlement.jsonl", [
        {
            "event": "settlement_update",
            "condition_id": "0xa",
            "market_slug": "slug-a",
            "resolved": True,
            "resolved_outcome": "win",
            "pnl": 2.8,
        },
    ])

    writer = JsonlRunWriter(tmp_path / "settlement.jsonl")

    fetch_called_for: list[str] = []

    async def fake_fetch(*, condition_id, **kwargs):
        fetch_called_for.append(condition_id)
        return None

    asyncio.get_event_loop().run_until_complete(
        run_settlement_loop(
            jsonl_dir=tmp_path,
            writer=writer,
            fetch_resolution=fake_fetch,
            poll_interval_seconds=0.0,
            max_iterations=1,
            now_fn=lambda: datetime(2026, 4, 16, 6, 0, 0, tzinfo=UTC),
        )
    )

    # The already-settled condition_id should not have been fetched
    assert "0xa" not in fetch_called_for


def test_pnl_calculation_no_token(tmp_path: Path):
    """NO token at 0.30, market resolves NO wins => pnl = (1.0 - 0.30) * 10 = 7.0."""
    entry = UnresolvedEntry(
        market_slug="slug-n",
        condition_id="0xn",
        strategy_name="temp_80c_basic",
        arena="temp_80c",
        token_side="no",
        entry_price=0.30,
        shares=10.0,
        stake=3.0,
        city="Chicago",
        observation_date="2026-04-15",
        source_file="run.jsonl",
    )
    resolution = MarketResolution(
        condition_id="0xn",
        slug="slug-n",
        resolved=True,
        winning_outcome="No",
        resolution_price_yes=0.0,
        resolution_price_no=1.0,
    )
    result = compute_settlement(entry, resolution)
    assert result is not None
    assert abs(result["pnl"] - 7.0) < 1e-9
    assert result["resolved_outcome"] == "win"
    assert result["settlement_price"] == 1.0
