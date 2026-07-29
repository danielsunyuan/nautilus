from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
import sys

import httpx
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
fetch_market_resolution = mod.fetch_market_resolution
run_settlement_loop = mod.run_settlement_loop
_build_parser = mod._build_parser


def _entry(
    *,
    entry_price: float,
    shares: float,
    entry_fee: float | None,
    token_id: str = "yankees-token",
    outcome_name: str = "New York Yankees",
):
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
        outcome_name=outcome_name,
        game_time="2026-07-29T23:00:00Z",
        source_file="sports.jsonl",
        entry_fee=entry_fee,
        fee_status="known" if entry_fee is not None else "missing",
        token_id=token_id,
    )


def test_compute_settlement_win_reports_gross_fee_and_net_pnl():
    entry = _entry(entry_price=0.60, shares=5.0, entry_fee=0.018)
    resolution = MarketResolution(
        condition_id="0xabc",
        slug=entry.market_slug,
        resolved=True,
        winning_outcome=entry.outcome_name,
        resolution_price=1.0,
        winning_token_id=entry.token_id,
        resolution_source="https://clob.test/markets/0xabc",
        observed_at="2026-07-29T12:00:00+00:00",
    )

    result = compute_settlement(entry, resolution)

    assert result is not None
    assert result["gross_pnl"] == 2.0
    assert result["entry_fee"] == 0.018
    assert result["net_pnl"] == 1.982
    assert result["pnl"] == result["net_pnl"]
    assert result["fee_status"] == "known"
    assert result["winning_token_id"] == "yankees-token"
    assert result["winning_outcome"] == "New York Yankees"
    assert result["resolution_source"] == "https://clob.test/markets/0xabc"
    assert result["resolution_observed_at"] == "2026-07-29T12:00:00+00:00"


def test_compute_settlement_loss_reports_gross_fee_and_net_pnl():
    entry = _entry(entry_price=0.70, shares=4.0, entry_fee=0.0252)
    resolution = MarketResolution(
        condition_id="0xabc",
        slug=entry.market_slug,
        resolved=True,
        winning_outcome="Boston Red Sox",
        resolution_price=1.0,
        winning_token_id="red-sox-token",
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
        winning_token_id=entry.token_id,
    )

    result = compute_settlement(entry, resolution)

    assert result is not None
    assert result["gross_pnl"] == 2.0
    assert result["entry_fee"] is None
    assert result["net_pnl"] is None
    assert result["pnl"] is None
    assert result["fee_status"] == "missing"


def test_compute_settlement_uses_token_identity_not_outcome_label_for_win():
    entry = _entry(
        entry_price=0.60,
        shares=5.0,
        entry_fee=0.018,
        outcome_name="Persisted label differs",
    )
    resolution = MarketResolution(
        condition_id="0xabc",
        slug=entry.market_slug,
        resolved=True,
        winning_outcome="New York Yankees",
        resolution_price=1.0,
        winning_token_id=entry.token_id,
    )

    result = compute_settlement(entry, resolution)

    assert result is not None
    assert result["settlement_price"] == 1.0


def test_compute_settlement_uses_token_identity_not_outcome_label_for_loss():
    entry = _entry(
        entry_price=0.60,
        shares=5.0,
        entry_fee=0.018,
        outcome_name="New York Yankees",
    )
    resolution = MarketResolution(
        condition_id="0xabc",
        slug=entry.market_slug,
        resolved=True,
        winning_outcome=entry.outcome_name,
        resolution_price=1.0,
        winning_token_id="red-sox-token",
    )

    result = compute_settlement(entry, resolution)

    assert result is not None
    assert result["settlement_price"] == 0.0


def test_compute_settlement_without_entry_token_remains_unresolved():
    entry = _entry(
        entry_price=0.60,
        shares=5.0,
        entry_fee=0.018,
        token_id="",
    )
    resolution = MarketResolution(
        condition_id="0xabc",
        slug=entry.market_slug,
        resolved=True,
        winning_outcome=entry.outcome_name,
        resolution_price=1.0,
        winning_token_id="yankees-token",
    )

    assert compute_settlement(entry, resolution) is None


def test_compute_settlement_rejects_mismatched_condition_id():
    entry = _entry(entry_price=0.60, shares=5.0, entry_fee=0.018)
    resolution = MarketResolution(
        condition_id="0xdifferent",
        slug=entry.market_slug,
        resolved=True,
        winning_outcome=entry.outcome_name,
        resolution_price=1.0,
        winning_token_id=entry.token_id,
    )

    assert compute_settlement(entry, resolution) is None


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
        "token_id": "yankees-token",
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
    assert entries[0].token_id == "yankees-token"


def test_scan_unresolved_entries_treats_null_token_as_missing(tmp_path: Path):
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
        "token_id": None,
        "game_time": "2026-07-29T23:00:00Z",
        "resolved": False,
        "accounting_status": "open",
    }
    (tmp_path / "sports.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    entries = scan_unresolved_entries(tmp_path)

    assert len(entries) == 1
    assert entries[0].token_id == ""


def _fetch_resolution(
    handler,
    *,
    condition_id: str = "0xabc",
):
    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await fetch_market_resolution(
                condition_id=condition_id,
                http_client=client,
                clob_base_url="https://clob.test",
                now_fn=lambda: datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
            )

    return asyncio.run(run())


def test_fetch_market_resolution_returns_exact_clob_winner_evidence():
    def handler(request: httpx.Request):
        assert request.url == httpx.URL("https://clob.test/markets/0xabc")
        return httpx.Response(
            200,
            json={
                "condition_id": "0xabc",
                "closed": True,
                "tokens": [
                    {
                        "token_id": "yankees-token",
                        "outcome": "New York Yankees",
                        "winner": True,
                    },
                    {
                        "token_id": "red-sox-token",
                        "outcome": "Boston Red Sox",
                        "winner": False,
                    },
                ],
            },
        )

    result = _fetch_resolution(handler)

    assert result == MarketResolution(
        condition_id="0xabc",
        slug="",
        resolved=True,
        winning_outcome="New York Yankees",
        resolution_price=1.0,
        winning_token_id="yankees-token",
        resolution_source="https://clob.test/markets/0xabc",
        observed_at="2026-07-29T12:00:00+00:00",
    )


def test_fetch_market_resolution_returns_unresolved_for_open_market():
    def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "condition_id": "0xabc",
                "closed": False,
                "tokens": [],
            },
        )

    result = _fetch_resolution(handler)

    assert result is not None
    assert result.resolved is False
    assert result.resolution_source == "https://clob.test/markets/0xabc"
    assert result.observed_at == "2026-07-29T12:00:00+00:00"


@pytest.mark.parametrize(
    "tokens",
    [
        [
            {"token_id": "yankees-token", "outcome": "New York Yankees", "winner": False},
            {"token_id": "red-sox-token", "outcome": "Boston Red Sox", "winner": False},
        ],
        [
            {"token_id": "yankees-token", "outcome": "New York Yankees", "winner": True},
            {"token_id": "red-sox-token", "outcome": "Boston Red Sox", "winner": True},
        ],
    ],
)
def test_fetch_market_resolution_rejects_ambiguous_winners(tokens):
    def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "condition_id": "0xabc",
                "closed": True,
                "tokens": tokens,
            },
        )

    assert _fetch_resolution(handler) is None


def test_fetch_market_resolution_rejects_mismatched_condition_id():
    def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "condition_id": "0xdifferent",
                "closed": True,
                "tokens": [
                    {
                        "token_id": "yankees-token",
                        "outcome": "New York Yankees",
                        "winner": True,
                    },
                ],
            },
        )

    assert _fetch_resolution(handler) is None


def test_fetch_market_resolution_returns_none_on_request_failure():
    def handler(request: httpx.Request):
        raise httpx.ConnectError("network unavailable", request=request)

    assert _fetch_resolution(handler) is None


def test_settlement_loop_logs_gross_pnl_when_net_pnl_is_unknown(
    tmp_path: Path,
    monkeypatch,
):
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
        "token_id": "yankees-token",
        "game_time": "2026-07-29T23:00:00Z",
        "resolved": False,
        "accounting_status": "open",
        "entry_fee": None,
        "fee_status": "missing",
    }
    (tmp_path / "sports.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    class MemoryWriter:
        def __init__(self):
            self.events = []

        def write(self, event):
            self.events.append(event)

    class EagerLogger:
        def __init__(self):
            self.messages = []

        def info(self, message, *args):
            self.messages.append(message % args)

    async def fetch_resolution(**_kwargs):
        return MarketResolution(
            condition_id="0xabc",
            slug="mlb-nyy-bos-moneyline",
            resolved=True,
            winning_outcome="New York Yankees",
            resolution_price=1.0,
            winning_token_id="yankees-token",
            resolution_source="https://clob.test/markets/0xabc",
            observed_at="2026-07-29T12:00:00+00:00",
        )

    writer = MemoryWriter()
    logger = EagerLogger()
    monkeypatch.setattr(mod, "log", logger)

    asyncio.run(
        run_settlement_loop(
            jsonl_dir=tmp_path,
            writer=writer,
            fetch_resolution=fetch_resolution,
            poll_interval_seconds=0,
            max_iterations=1,
            now_fn=lambda: datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
        ),
    )

    assert len(writer.events) == 1
    assert writer.events[0]["gross_pnl"] == 2.0
    assert writer.events[0]["net_pnl"] is None
    assert any(
        "gross_pnl=2.0000 net_pnl=unknown" in message
        for message in logger.messages
    )


def test_parser_defaults_to_clob_market_host_and_accepts_override():
    parser = _build_parser()

    assert parser.parse_args([]).clob_host == "https://clob.polymarket.com"
    assert (
        parser.parse_args(["--clob-host", "https://clob.test"]).clob_host
        == "https://clob.test"
    )
