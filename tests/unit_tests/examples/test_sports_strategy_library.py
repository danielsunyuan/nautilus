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


def test_time_gate_blocks_far_future_game():
    from datetime import UTC, datetime, timedelta
    preset = _make_preset(max_hours_before_game=2.0)
    game_time = (datetime.now(tz=UTC) + timedelta(hours=4)).isoformat()
    assert not should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", game_time=game_time,
    )


def test_time_gate_allows_imminent_game():
    from datetime import UTC, datetime, timedelta
    preset = _make_preset(max_hours_before_game=2.0)
    game_time = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", game_time=game_time,
    )


def test_no_time_gate_passes_any_game_time():
    from datetime import UTC, datetime, timedelta
    preset = _make_preset()  # max_hours_before_game=None
    game_time = (datetime.now(tz=UTC) + timedelta(hours=24)).isoformat()
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", game_time=game_time,
    )


def test_empty_game_time_passes_with_gate():
    """Markets with no game_time (game_time='') should not be blocked by the gate."""
    preset = _make_preset(max_hours_before_game=1.0)
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", game_time="",
    )


def test_time_gate_naive_iso_string_is_treated_as_utc():
    """Gamma returns naive ISO strings (no Z, no +00:00) — gate must treat them as UTC."""
    from datetime import UTC, datetime, timedelta
    preset = _make_preset(max_hours_before_game=2.0)
    # Naive ISO string 4 hours in the future — should block
    future_naive = (datetime.now(tz=UTC) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S")
    assert not should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", game_time=future_naive,
    )


def test_bid_ratio_blocks_ask_heavy_book():
    preset = _make_preset(min_bid_ratio=0.55)
    # bid_size=30, ask_size=100 → ratio=0.23 < 0.55 → block
    assert not should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=30, ask_size=100,
        sport="tennis", market_type="moneyline",
    )


def test_bid_ratio_allows_bid_heavy_book():
    preset = _make_preset(min_bid_ratio=0.55)
    # bid_size=100, ask_size=50 → ratio=0.67 >= 0.55 → allow
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=50,
        sport="tennis", market_type="moneyline",
    )


def test_bid_ratio_zero_total_size_does_not_crash():
    preset = _make_preset(min_bid_ratio=0.55)
    # both sizes zero — should not divide by zero, should block
    assert not should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=0, ask_size=0,
        sport="tennis", market_type="moneyline",
    )


def test_no_bid_ratio_passes_ask_heavy_book():
    preset = _make_preset()  # min_bid_ratio=None
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=10, ask_size=90,
        sport="tennis", market_type="moneyline",
    )


def test_depth_focused_presets_count_and_ratio():
    presets = lib.depth_focused_presets()
    assert len(presets) == 10  # 2 per arena × 5 arenas
    assert all(p.min_bid_ratio == 0.55 for p in presets)


def test_depth_focused_presets_names():
    presets = lib.depth_focused_presets()
    assert all("depth_focused" in p.name for p in presets)
    assert all("focused" in p.name for p in presets)


def test_depth_focused_presets_inherit_whitelists():
    focused = lib.focused_presets()
    depth = lib.depth_focused_presets()
    for fp, dp in zip(focused, depth):
        assert fp.allowed_sports == dp.allowed_sports
        assert fp.allowed_market_types == dp.allowed_market_types
        assert fp.mode == dp.mode


def test_depth_focused_presets_block_mlb():
    presets = lib.depth_focused_presets()
    for preset in presets:
        assert not should_enter_sports_market(
            preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
            sport="mlb", market_type="moneyline",
        ), f"{preset.name} should not enter mlb moneyline"


def test_clv_gate_blocks_when_overpriced():
    preset = _make_preset(min_clv_edge=0.05)
    # Polymarket ask=0.68, Vegas implied=0.70 → gap=0.02 < 0.05 → block
    assert not should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.68, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", vegas_implied=0.70,
    )


def test_clv_gate_allows_when_underpriced():
    preset = _make_preset(min_clv_edge=0.05)
    # Polymarket ask=0.63, Vegas implied=0.70 → gap=0.07 >= 0.05 → allow
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", vegas_implied=0.70,
    )


def test_clv_gate_passes_when_no_vegas_data():
    preset = _make_preset(min_clv_edge=0.05)
    # vegas_implied=None — don't block on missing data
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", vegas_implied=None,
    )


def test_no_clv_gate_ignores_vegas():
    preset = _make_preset()  # min_clv_edge=None
    # Even if Vegas would say "overpriced", no gate set → pass
    assert should_enter_sports_market(
        preset=preset, bid=0.62, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", vegas_implied=0.70,
    )


def test_clv_focused_presets_count_and_edge():
    presets = lib.clv_focused_presets()
    assert len(presets) == 10
    assert all(p.min_clv_edge == 0.05 for p in presets)


def test_clv_focused_presets_names():
    presets = lib.clv_focused_presets()
    assert all("clv_focused" in p.name for p in presets)


# ===== Kelly Criterion Tests =====

def test_kelly_stake_usd_basic():
    # edge=0.12, entry=0.65, bankroll=1000, max_fraction=0.25
    # full_kelly = 0.12 / (1.0 - 0.65) = 0.12 / 0.35 = 0.3429
    # capped at 0.25 → 250.0
    result = lib.kelly_stake_usd(edge=0.12, entry_price=0.65, bankroll_usd=1000.0)
    assert abs(result - 250.0) < 0.01


def test_kelly_stake_usd_below_cap():
    # edge=0.05, entry=0.65, bankroll=1000
    # full_kelly = 0.05 / 0.35 = 0.1429 < 0.25 → 142.9
    result = lib.kelly_stake_usd(edge=0.05, entry_price=0.65, bankroll_usd=1000.0)
    assert abs(result - 142.86) < 0.1


def test_kelly_stake_zero_edge():
    assert lib.kelly_stake_usd(edge=0.0, entry_price=0.65, bankroll_usd=1000.0) == 0.0


def test_kelly_stake_negative_edge():
    assert lib.kelly_stake_usd(edge=-0.05, entry_price=0.65, bankroll_usd=1000.0) == 0.0


def test_kelly_stake_entry_at_one():
    # entry_price >= 1.0 → 0.0 (undefined)
    assert lib.kelly_stake_usd(edge=0.10, entry_price=1.0, bankroll_usd=1000.0) == 0.0


def test_kelly_stake_custom_max_fraction():
    # max_fraction=0.10 → min(full_kelly, 0.10) * 1000
    # full_kelly = 0.12 / 0.35 = 0.3429 > 0.10 → 100.0
    result = lib.kelly_stake_usd(edge=0.12, entry_price=0.65, bankroll_usd=1000.0, max_fraction=0.10)
    assert abs(result - 100.0) < 0.01


def test_preset_has_kelly_edge_estimate():
    preset = _make_preset(kelly_edge_estimate=0.12, kelly_max_fraction=0.25)
    assert preset.kelly_edge_estimate == 0.12
    assert preset.kelly_max_fraction == 0.25


def test_preset_kelly_defaults_none():
    preset = _make_preset()
    assert preset.kelly_edge_estimate is None
    # kelly_max_fraction default is 0.25
    assert preset.kelly_max_fraction == 0.25


def test_kelly_sizing_end_to_end():
    """kelly_stake_usd + shares conversion mirrors _compute_order_quantity Kelly branch."""
    # edge=0.12, entry=0.65, order_qty=10 → bankroll=1000 → kelly_usd=250 → shares=250/0.65≈384.6
    kelly_usd = lib.kelly_stake_usd(edge=0.12, entry_price=0.65, bankroll_usd=1000.0)
    assert abs(kelly_usd - 250.0) < 0.01
    shares = kelly_usd / 0.65
    assert abs(shares - 384.62) < 0.1


def test_kelly_sizing_zero_edge_returns_zero():
    """kelly_stake_usd returns 0 for zero edge — _compute_order_quantity should refuse order."""
    assert lib.kelly_stake_usd(edge=0.0, entry_price=0.65, bankroll_usd=1000.0) == 0.0
