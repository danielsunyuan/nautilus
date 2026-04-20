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

lib = _load("examples.live.polymarket.sports_strategy_library", "sports_strategy_library.py")
SportsStrategyPreset = lib.SportsStrategyPreset
should_enter_sports_market = lib.should_enter_sports_market


def _make_preset(**overrides):
    defaults = dict(
        name="test", arena="sports_60c", min_ask=0.60, max_ask=0.70, mode="basic"
    )
    return SportsStrategyPreset(**{**defaults, **overrides})


def test_empty_sport_blocks_when_whitelist_active():
    """sport='' (default) must block when allowed_sports is set."""
    preset = _make_preset(allowed_sports=frozenset({"tennis"}))
    assert not should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
        # sport and market_type omitted — use defaults ("", "")
    )


def test_allowed_sports_blocks_wrong_sport():
    preset = _make_preset(allowed_sports=frozenset({"tennis"}))
    assert not should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100, sport="nba", market_type="moneyline"
    )


def test_allowed_sports_passes_correct_sport():
    preset = _make_preset(allowed_sports=frozenset({"tennis"}))
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100, sport="tennis", market_type="moneyline"
    )


def test_allowed_market_types_blocks_wrong_type():
    preset = _make_preset(allowed_market_types=frozenset({"totals"}))
    assert not should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100, sport="nba", market_type="spreads"
    )


def test_allowed_market_types_passes_correct_type():
    preset = _make_preset(allowed_market_types=frozenset({"totals"}))
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100, sport="nba", market_type="totals"
    )


def test_no_whitelist_passes_everything():
    preset = _make_preset()
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100, sport="mlb", market_type="spreads"
    )


def test_both_whitelists_combined():
    preset = _make_preset(
        allowed_sports=frozenset({"nba"}),
        allowed_market_types=frozenset({"totals"}),
    )
    # nba+totals passes
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100, sport="nba", market_type="totals"
    )
    # nba+spreads blocked
    assert not should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100, sport="nba", market_type="spreads"
    )
    # tennis+totals blocked
    assert not should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100, sport="tennis", market_type="totals"
    )


def test_focused_presets_exist():
    presets = lib.focused_presets()
    assert len(presets) == 10  # 2 per arena × 5 arenas
    # All focused presets are basic mode
    assert all(p.mode == "basic" for p in presets)
    # All focused presets have sport or market_type whitelists
    assert all(
        p.allowed_sports is not None or p.allowed_market_types is not None
        for p in presets
    )


def test_focused_presets_block_mlb():
    presets = lib.focused_presets()
    for preset in presets:
        result = should_enter_sports_market(
            preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
            sport="mlb", market_type="moneyline"
        )
        assert not result, f"{preset.name} should not enter mlb moneyline"


def test_focused_presets_block_nba_spreads():
    presets = lib.focused_presets()
    for preset in presets:
        result = should_enter_sports_market(
            preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
            sport="nba", market_type="spreads"
        )
        assert not result, f"{preset.name} should not enter nba spreads"


def test_focused_presets_allow_tennis():
    presets = lib.focused_presets()
    # At least one preset allows tennis moneyline in the 60c band
    any_tennis = any(
        should_enter_sports_market(
            preset=p, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
            sport="tennis", market_type="moneyline"
        )
        for p in presets
    )
    assert any_tennis


def test_focused_presets_allow_nba_totals():
    presets = lib.focused_presets()
    any_nba_totals = any(
        should_enter_sports_market(
            preset=p, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
            sport="nba", market_type="totals"
        )
        for p in presets
    )
    assert any_nba_totals
