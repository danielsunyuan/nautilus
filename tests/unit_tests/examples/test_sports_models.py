import sys
from pathlib import Path
import importlib.util


def _load(name, fname):
    p = Path("examples/live/polymarket") / fname
    spec = importlib.util.spec_from_file_location(name, p.resolve())
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


models = _load("examples.live.polymarket.sports_models", "sports_models.py")
SportsMarket = models.SportsMarket
select_highest_price_outcome_per_condition = models.select_highest_price_outcome_per_condition


def _market(**overrides):
    defaults = dict(
        slug="fight-1",
        condition_id="0xabc",
        sport="ufc",
        match_title="Fighter A vs Fighter B",
        market_type="moneyline",
        outcome_name="Fighter A",
        token_id="token-a",
        game_time="2026-04-24T12:00:00Z",
        active=True,
        accepting_orders=True,
        current_price=0.62,
    )
    return SportsMarket(**{**defaults, **overrides})


def test_select_highest_price_outcome_per_condition_keeps_ufc_favorite():
    markets = [
        _market(outcome_name="Fighter A", token_id="token-a", current_price=0.62),
        _market(outcome_name="Fighter B", token_id="token-b", current_price=0.38),
        _market(
            slug="fight-2",
            condition_id="0xdef",
            outcome_name="Fighter C",
            token_id="token-c",
            current_price=0.55,
        ),
        _market(
            slug="fight-2",
            condition_id="0xdef",
            outcome_name="Fighter D",
            token_id="token-d",
            current_price=0.45,
        ),
    ]

    selected = select_highest_price_outcome_per_condition(markets)

    assert [market.token_id for market in selected] == ["token-a", "token-c"]


def test_select_highest_price_outcome_per_condition_is_stable_on_ties():
    markets = [
        _market(outcome_name="Fighter A", token_id="token-b", current_price=0.50),
        _market(outcome_name="Fighter B", token_id="token-a", current_price=0.50),
    ]

    selected = select_highest_price_outcome_per_condition(markets)

    assert [market.token_id for market in selected] == ["token-a"]
