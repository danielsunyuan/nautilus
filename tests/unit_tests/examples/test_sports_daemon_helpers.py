"""
Unit tests for pure helper functions in polymarket_sports_paper_daemon.py.

These helpers are extracted and tested independently to avoid loading the full
daemon (which requires compiled Nautilus extensions).
"""
from __future__ import annotations

from decimal import Decimal
import importlib.util
from pathlib import Path
from types import SimpleNamespace


# --- Minimal stubs so we can test the pure helpers without Nautilus imports ---

class _Market:
    """Minimal SportsMarket stand-in for grouping tests."""
    def __init__(self, slug, condition_id, token_id, match_title, game_time):
        self.slug = slug
        self.condition_id = condition_id
        self.token_id = token_id
        self.match_title = match_title
        self.game_time = game_time


def _build_instrument_id(market) -> str:
    return f"{market.condition_id}-{market.token_id}.POLYMARKET"


def _game_key(market) -> str:
    return f"{market.match_title}|{market.game_time}"


def _group_markets_by_game(markets) -> dict[str, list[str]]:
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    for market in markets:
        groups[_game_key(market)].append(_build_instrument_id(market))
    return dict(groups)


def _group_markets_by_condition(markets) -> dict[str, list[str]]:
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    for market in markets:
        groups[market.condition_id].append(_build_instrument_id(market))
    return dict(groups)


# --- Tests ---

def _make_market(slug="m1", cond="0xABC", token="tok1", title="A vs B", game_time="2026-04-20T18:00:00"):
    return _Market(slug=slug, condition_id=cond, token_id=token, match_title=title, game_time=game_time)


def test_two_markets_same_game_are_siblings():
    """Two markets from the same game are grouped together."""
    m1 = _make_market(cond="0xAAA", token="t1", title="A vs B", game_time="2026-04-20T18:00:00")
    m2 = _make_market(cond="0xBBB", token="t2", title="A vs B", game_time="2026-04-20T18:00:00")
    groups = _group_markets_by_game([m1, m2])
    key = _game_key(m1)
    assert key in groups
    assert len(groups[key]) == 2
    assert _build_instrument_id(m1) in groups[key]
    assert _build_instrument_id(m2) in groups[key]


def test_different_game_times_separate_groups():
    """Markets with different game_time values are not siblings."""
    m1 = _make_market(cond="0xAAA", token="t1", title="A vs B", game_time="2026-04-20T18:00:00")
    m2 = _make_market(cond="0xBBB", token="t2", title="A vs B", game_time="2026-04-21T18:00:00")
    groups = _group_markets_by_game([m1, m2])
    assert len(groups) == 2
    # Each group has exactly one market
    for key, ids in groups.items():
        assert len(ids) == 1


def test_single_market_gets_empty_sibling_list():
    """A single-market game has no siblings after self-exclusion."""
    m = _make_market(cond="0xAAA", token="t1")
    groups = _group_markets_by_game([m])
    inst_id = _build_instrument_id(m)
    siblings = [iid for iid in groups[_game_key(m)] if iid != inst_id]
    assert siblings == []


def test_condition_grouping_keeps_distinct_ufc_markets_separate():
    """Different condition_ids in the same fight should remain separate risk families."""
    moneyline = _make_market(cond="0xAAA", token="t1", title="A vs B", game_time="2026-04-20T18:00:00")
    prop = _make_market(cond="0xBBB", token="t2", title="A vs B", game_time="2026-04-20T18:00:00")

    groups = _group_markets_by_condition([moneyline, prop])

    assert groups["0xAAA"] == [_build_instrument_id(moneyline)]
    assert groups["0xBBB"] == [_build_instrument_id(prop)]


def _load_daemon_module():
    path = Path("examples/live/polymarket/polymarket_sports_paper_daemon.py")
    spec = importlib.util.spec_from_file_location("sports_daemon_for_tests", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CacheWithPositions:
    def __init__(self, positions):
        self._positions = positions

    def positions_open(self):
        return list(self._positions)


def test_result_extraction_credits_only_the_actual_runtime_strategy():
    daemon = _load_daemon_module()
    market = SimpleNamespace(
        slug="tennis-a-b",
        condition_id="0xAAA",
        token_id="token-1",
        sport="tennis",
        match_title="A vs B",
        market_type="moneyline",
        outcome_name="A",
        game_time="2026-07-30T10:00:00Z",
    )
    presets = (
        SimpleNamespace(
            name="preset_a",
            arena="sports_70c",
            mode="basic",
            min_ask=0.70,
            max_ask=0.80,
            allowed_sports=frozenset({"tennis"}),
            allowed_market_types=frozenset({"moneyline"}),
        ),
        SimpleNamespace(
            name="preset_b",
            arena="sports_70c",
            mode="basic",
            min_ask=0.70,
            max_ask=0.80,
            allowed_sports=frozenset({"tennis"}),
            allowed_market_types=frozenset({"moneyline"}),
        ),
    )
    instrument_id = daemon._build_instrument_id(market)
    position = SimpleNamespace(
        id="P-1",
        instrument_id=instrument_id,
        strategy_id="SPORTS-PRESET-A-001",
        peak_qty=Decimal("5"),
        avg_px_open=Decimal("0.72"),
        ts_opened=0,
    )

    rows = daemon.extract_sports_strategy_results(
        cache=_CacheWithPositions([position]),
        markets=[market],
        presets=presets,
        strategy_ids_by_key={
            "tennis-a-b:preset_a": "SPORTS-PRESET-A-001",
            "tennis-a-b:preset_b": "SPORTS-PRESET-B-001",
        },
    )

    credited = [row for row in rows if row["accounting_status"] == "open"]
    assert [(row["preset_name"], row["strategy_id"], row["position_id"]) for row in credited] == [
        ("preset_a", "SPORTS-PRESET-A-001", "P-1"),
    ]


def test_result_extraction_carries_position_commission():
    daemon = _load_daemon_module()
    market = SimpleNamespace(
        slug="tennis-a-b",
        condition_id="0xAAA",
        token_id="token-1",
        sport="tennis",
        match_title="A vs B",
        market_type="moneyline",
        outcome_name="A",
        game_time="2026-07-30T10:00:00Z",
    )
    preset = SimpleNamespace(
        name="preset_a",
        arena="sports_70c",
        mode="basic",
        min_ask=0.70,
        max_ask=0.80,
        allowed_sports=frozenset({"tennis"}),
        allowed_market_types=frozenset({"moneyline"}),
    )
    position = SimpleNamespace(
        id="P-1",
        instrument_id=daemon._build_instrument_id(market),
        strategy_id="SPORTS-PRESET-A-001",
        peak_qty=Decimal("5"),
        avg_px_open=Decimal("0.72"),
        ts_opened=0,
        commissions=lambda: [
            SimpleNamespace(currency="USDC", as_decimal=lambda: Decimal("0.0123")),
        ],
    )

    rows = daemon.extract_sports_strategy_results(
        cache=_CacheWithPositions([position]),
        markets=[market],
        presets=(preset,),
        strategy_ids_by_key={"tennis-a-b:preset_a": "SPORTS-PRESET-A-001"},
    )

    assert rows[0]["entry_fee"] == 0.0123
    assert rows[0]["fee_status"] == "known"


def test_result_extraction_marks_unavailable_commission_missing():
    daemon = _load_daemon_module()
    market = SimpleNamespace(
        slug="tennis-a-b",
        condition_id="0xAAA",
        token_id="token-1",
        sport="tennis",
        match_title="A vs B",
        market_type="moneyline",
        outcome_name="A",
        game_time="2026-07-30T10:00:00Z",
    )
    preset = SimpleNamespace(
        name="preset_a",
        arena="sports_70c",
        mode="basic",
        min_ask=0.70,
        max_ask=0.80,
        allowed_sports=frozenset({"tennis"}),
        allowed_market_types=frozenset({"moneyline"}),
    )

    def unavailable_commissions():
        raise RuntimeError("commission cache unavailable")

    position = SimpleNamespace(
        id="P-1",
        instrument_id=daemon._build_instrument_id(market),
        strategy_id="SPORTS-PRESET-A-001",
        peak_qty=Decimal("5"),
        avg_px_open=Decimal("0.72"),
        ts_opened=0,
        commissions=unavailable_commissions,
    )

    rows = daemon.extract_sports_strategy_results(
        cache=_CacheWithPositions([position]),
        markets=[market],
        presets=(preset,),
        strategy_ids_by_key={"tennis-a-b:preset_a": "SPORTS-PRESET-A-001"},
    )

    assert rows[0]["entry_fee"] is None
    assert rows[0]["fee_status"] == "missing"
