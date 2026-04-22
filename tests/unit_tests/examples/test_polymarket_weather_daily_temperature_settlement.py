from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
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

# Fake instrument_id template: <condition_id>-<token_id>.POLYMARKET
_FAKE_CONDITION_ID = "0xabc123"
_FAKE_TOKEN_ID = "99999abc"
_FAKE_INSTRUMENT_ID = f"{_FAKE_CONDITION_ID}-{_FAKE_TOKEN_ID}.POLYMARKET"


def _make_strategy_result(
    *,
    market_slug: str = "will-the-high-temperature-nyc-70f",
    condition_id: str = _FAKE_CONDITION_ID,
    token_id: str = _FAKE_TOKEN_ID,
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
    accounting_status: str = "open",
) -> dict:
    """Build a strategy_result row with the fields scan_unresolved_entries expects."""
    instrument_id = f"{condition_id}-{token_id}.POLYMARKET"
    row: dict = {
        "event": "strategy_result",
        "run_id": run_id,
        "market_slug": market_slug,
        "condition_id": condition_id,
        "instrument_id": instrument_id,
        "strategy_name": strategy_name,
        "arena": arena,
        "token_side": token_side,
        "entry_price": entry_price,
        "shares": shares,
        "stake": stake,
        "city": city,
        "observation_date": observation_date,
        "resolved": resolved,
        "accounting_status": accounting_status,
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
        _make_strategy_result(market_slug="slug-a", condition_id="0xaaa", token_id="tid_a", resolved=False),
        _make_strategy_result(market_slug="slug-b", condition_id="0xbbb", token_id="tid_b", resolved=False),
        _make_strategy_result(market_slug="slug-c", condition_id="0xccc", token_id="tid_c", resolved=True, resolved_outcome="win"),
    ]
    _write_jsonl(tmp_path / "weather_temp_live_run.jsonl", rows)

    entries = scan_unresolved_entries(tmp_path)
    assert len(entries) == 2
    slugs = {e.market_slug for e in entries}
    assert slugs == {"slug-a", "slug-b"}


def test_scan_ignores_non_strategy_result_events(tmp_path: Path):
    """Only strategy_result events are scanned; other event types are ignored."""
    rows = [
        {"event": "round_start", "market_slug": "slug-x"},
        {"event": "market_discovered", "market_slug": "slug-y"},
        _make_strategy_result(market_slug="slug-z", condition_id="0xzzz", token_id="tid_z"),
    ]
    _write_jsonl(tmp_path / "weather_temp_live_run.jsonl", rows)

    entries = scan_unresolved_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].market_slug == "slug-z"


def test_compute_settlement_win():
    """YES token at 0.72, market resolves YES => pnl = +2.8, outcome = win."""
    entry = UnresolvedEntry(
        market_slug="slug-a",
        condition_id="0xa",
        token_id="tid_a",
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
    # settlement_price=1.0 means the YES token won (oracle resolution)
    resolution = MarketResolution(token_id="tid_a", resolved=True, settlement_price=1.0)
    result = compute_settlement(entry, resolution)
    assert result is not None
    assert result["resolved"] is True
    assert result["resolved_outcome"] == "win"
    assert abs(result["pnl"] - 2.8) < 1e-9
    assert result["settlement_price"] == 1.0


def test_compute_settlement_loss():
    """YES token at 0.72, market resolves NO (token settles at 0) => pnl = -7.2."""
    entry = UnresolvedEntry(
        market_slug="slug-a",
        condition_id="0xa",
        token_id="tid_a",
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
    # settlement_price=0.0 means the YES token lost
    resolution = MarketResolution(token_id="tid_a", resolved=True, settlement_price=0.0)
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
        token_id="tid_a",
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
    resolution = MarketResolution(token_id="tid_a", resolved=False, settlement_price=None)
    result = compute_settlement(entry, resolution)
    assert result is None


def test_settlement_loop_processes_and_exits(tmp_path: Path):
    """Loop resolves one market, writes settlement_update, exits after max_iterations=1."""
    rows = [
        _make_strategy_result(market_slug="slug-a", condition_id="0xaaa", token_id="tid_a"),
    ]
    _write_jsonl(tmp_path / "weather_temp_live_run.jsonl", rows)

    writer = JsonlRunWriter(tmp_path / "settlement_live.jsonl")

    resolution = MarketResolution(token_id="tid_a", resolved=True, settlement_price=1.0)

    async def fake_fetch(*, token_id: str, **kwargs):
        if token_id == "tid_a":
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
    lines = (tmp_path / "settlement_live.jsonl").read_text().strip().split("\n")
    events = [json.loads(line) for line in lines]
    settlement_events = [e for e in events if e.get("event") == "settlement_update"]
    assert len(settlement_events) == 1
    ev = settlement_events[0]
    assert ev["market_slug"] == "slug-a"
    assert ev["resolved"] is True
    assert ev["resolved_outcome"] == "win"
    assert abs(ev["pnl"] - 2.8) < 1e-9


def test_settlement_loop_skips_already_settled(tmp_path: Path):
    """Markets that already have a settlement_update in JSONL are not re-queried."""
    _write_jsonl(tmp_path / "weather_temp_live_run.jsonl", [
        _make_strategy_result(market_slug="slug-a", condition_id="0xaaa", token_id="tid_a"),
    ])
    # Write a settlement_update with token_id so it is picked up as already settled
    _write_jsonl(tmp_path / "settlement_live.jsonl", [
        {
            "event": "settlement_update",
            "token_id": "tid_a",
            "condition_id": "0xaaa",
            "market_slug": "slug-a",
            "resolved": True,
            "resolved_outcome": "win",
            "pnl": 2.8,
        },
    ])

    writer = JsonlRunWriter(tmp_path / "settlement_live.jsonl")

    fetch_called_for: list[str] = []

    async def fake_fetch(*, token_id: str, **kwargs):
        fetch_called_for.append(token_id)
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

    # The already-settled token_id should not have been fetched
    assert "tid_a" not in fetch_called_for


def test_pnl_calculation_no_token(tmp_path: Path):
    """NO token at 0.30, market resolves NO wins => pnl = (1.0 - 0.30) * 10 = 7.0."""
    entry = UnresolvedEntry(
        market_slug="slug-n",
        condition_id="0xn",
        token_id="tid_n",
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
    # For a NO token, resolution_price=1.0 means NO won (from CLOB perspective)
    resolution = MarketResolution(token_id="tid_n", resolved=True, settlement_price=1.0)
    result = compute_settlement(entry, resolution)
    assert result is not None
    assert abs(result["pnl"] - 7.0) < 1e-9
    assert result["resolved_outcome"] == "win"
    assert result["settlement_price"] == 1.0


# ---------------------------------------------------------------------------
# Redemption tests
# ---------------------------------------------------------------------------

_collect_redeemed_condition_ids = mod._collect_redeemed_condition_ids
_redeem_pending_wins = mod._redeem_pending_wins


def test_collect_redeemed_condition_ids_empty():
    """Only redemption_completed events count; pending and other events do not."""
    all_rows = [
        ("run.jsonl", {"event": "settlement_update", "condition_id": "0xa"}),
        ("run.jsonl", {"event": "redemption_pending", "condition_id": "0xa"}),
    ]
    assert _collect_redeemed_condition_ids(all_rows) == set()


def test_collect_redeemed_condition_ids_finds_completed():
    all_rows = [
        ("run.jsonl", {"event": "redemption_completed", "condition_id": "0xabc"}),
        ("run.jsonl", {"event": "redemption_pending", "condition_id": "0xdef"}),
        ("run.jsonl", {"event": "redemption_completed", "condition_id": "0x123"}),
    ]
    assert _collect_redeemed_condition_ids(all_rows) == {"0xabc", "0x123"}


def test_redeem_pending_wins_calls_fn_for_wins_only(tmp_path: Path):
    """Calls redeem_fn for WIN condition_ids; skips LOSS settlements."""
    _write_jsonl(tmp_path / "settlement_live.jsonl", [
        {
            "event": "settlement_update",
            "condition_id": "0xwin1",
            "market_slug": "slug-win1",
            "token_id": "tid-win1",
            "token_side": "yes",
            "shares": 10.0,
            "resolved_outcome": "win",
            "resolved": True,
            "real_order": True,
        },
        {
            "event": "settlement_update",
            "condition_id": "0xloss1",
            "market_slug": "slug-loss1",
            "token_id": "tid-loss1",
            "token_side": "yes",
            "shares": 10.0,
            "resolved_outcome": "loss",
            "resolved": True,
            "real_order": True,
        },
    ])

    writer = JsonlRunWriter(tmp_path / "redemption.jsonl")
    redeemed: list[str] = []

    async def fake_redeem(*, condition_id: str, token_side: str) -> tuple[bool, str]:
        redeemed.append(condition_id)
        return True, "0xfaketxhash"

    asyncio.get_event_loop().run_until_complete(
        _redeem_pending_wins(
            jsonl_dir=tmp_path,
            writer=writer,
            redeem_fn=fake_redeem,
            now_fn=lambda: datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC),
        )
    )

    assert redeemed == ["0xwin1"]
    events = [json.loads(l) for l in (tmp_path / "redemption.jsonl").read_text().strip().split("\n")]
    assert len(events) == 1
    assert events[0]["event"] == "redemption_completed"
    assert events[0]["condition_id"] == "0xwin1"
    assert events[0]["tx_hash"] == "0xfaketxhash"


def test_redeem_pending_wins_skips_already_completed(tmp_path: Path):
    """condition_ids with a redemption_completed event are never re-attempted."""
    _write_jsonl(tmp_path / "settlement_live.jsonl", [
        {
            "event": "settlement_update",
            "condition_id": "0xwin1",
            "resolved_outcome": "win",
            "token_id": "tid-win1",
            "token_side": "yes",
            "shares": 10.0,
            "resolved": True,
            "real_order": True,
        },
        {
            "event": "redemption_completed",
            "condition_id": "0xwin1",
            "tx_hash": "0xprev",
        },
    ])

    writer = JsonlRunWriter(tmp_path / "redemption.jsonl")
    redeemed: list[str] = []

    async def fake_redeem(*, condition_id: str, token_side: str) -> tuple[bool, str]:
        redeemed.append(condition_id)
        return True, "0xnewtx"

    asyncio.get_event_loop().run_until_complete(
        _redeem_pending_wins(
            jsonl_dir=tmp_path,
            writer=writer,
            redeem_fn=fake_redeem,
            now_fn=lambda: datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC),
        )
    )

    assert redeemed == []
    assert not (tmp_path / "redemption.jsonl").exists()


def test_redeem_pending_wins_writes_pending_on_failure(tmp_path: Path):
    """On redemption failure (e.g. no MATIC), writes redemption_pending event."""
    _write_jsonl(tmp_path / "settlement_live.jsonl", [
        {
            "event": "settlement_update",
            "condition_id": "0xwin1",
            "market_slug": "slug-win1",
            "token_id": "tid-win1",
            "token_side": "yes",
            "shares": 5.0,
            "resolved_outcome": "win",
            "resolved": True,
            "real_order": True,
        },
    ])

    writer = JsonlRunWriter(tmp_path / "redemption.jsonl")

    async def fake_redeem(*, condition_id: str, token_side: str) -> tuple[bool, str]:
        return False, "insufficient funds for gas * price + value"

    asyncio.get_event_loop().run_until_complete(
        _redeem_pending_wins(
            jsonl_dir=tmp_path,
            writer=writer,
            redeem_fn=fake_redeem,
            now_fn=lambda: datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC),
        )
    )

    events = [json.loads(l) for l in (tmp_path / "redemption.jsonl").read_text().strip().split("\n")]
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "redemption_pending"
    assert ev["condition_id"] == "0xwin1"
    assert "insufficient funds" in ev["error"]


def test_redeem_pending_wins_deduplicates_condition_id(tmp_path: Path):
    """Multiple WIN entries for the same condition_id trigger only one redemption."""
    _write_jsonl(tmp_path / "settlement_live.jsonl", [
        {
            "event": "settlement_update",
            "condition_id": "0xwin1",
            "resolved_outcome": "win",
            "token_id": "tid-a",
            "token_side": "yes",
            "shares": 5.0,
            "resolved": True,
            "real_order": True,
        },
        {
            "event": "settlement_update",
            "condition_id": "0xwin1",
            "resolved_outcome": "win",
            "token_id": "tid-b",
            "token_side": "yes",
            "shares": 8.0,
            "resolved": True,
            "real_order": True,
        },
    ])

    writer = JsonlRunWriter(tmp_path / "redemption.jsonl")
    call_count = [0]

    async def fake_redeem(*, condition_id: str, token_side: str) -> tuple[bool, str]:
        call_count[0] += 1
        return True, "0xtx"

    asyncio.get_event_loop().run_until_complete(
        _redeem_pending_wins(
            jsonl_dir=tmp_path,
            writer=writer,
            redeem_fn=fake_redeem,
            now_fn=lambda: datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC),
        )
    )

    assert call_count[0] == 1
