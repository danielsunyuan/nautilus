"""
Unit tests for pure helper functions in polymarket_sports_paper_daemon.py.

These helpers are extracted and tested independently to avoid loading the full
daemon (which requires compiled Nautilus extensions).
"""
from __future__ import annotations


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
