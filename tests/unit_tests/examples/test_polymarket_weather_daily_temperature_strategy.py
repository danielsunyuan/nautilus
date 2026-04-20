from __future__ import annotations

import importlib.util
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


lib = _load_module(
    "examples.live.polymarket.weather_daily_temperature_strategy_library",
    ROOT / "examples" / "live" / "polymarket" / "weather_daily_temperature_strategy_library.py",
)


# ---------------------------------------------------------------------------
# Helper to build common market kwargs
# ---------------------------------------------------------------------------

def _market(*, bid: float, ask: float, bid_size: float = 10.0, ask_size: float = 10.0):
    return dict(bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)


def _preset_by_name(name: str):
    return next(p for p in lib.daily_temperature_price_arena_presets() if p.name == name)


# ---------------------------------------------------------------------------
# Preset factory tests
# ---------------------------------------------------------------------------

def test_factory_returns_at_least_8_presets() -> None:
    presets = lib.daily_temperature_price_arena_presets()
    assert len(presets) >= 13


def test_factory_contains_required_names() -> None:
    names = {p.name for p in lib.daily_temperature_price_arena_presets()}
    required = {
        "temp_50c_band_only",
        "temp_50c_basic",
        "temp_60c_band_only",
        "temp_60c_basic",
        "temp_70c_band_only",
        "temp_70c_basic",
        "temp_80c_band_only",
        "temp_80c_basic",
        "temp_90c_band_only",
        "temp_90c_basic",
        "temp_70c_support",
        "temp_80c_support",
        "temp_90c_support",
    }
    assert required.issubset(names)


def test_preset_defaults() -> None:
    p = _preset_by_name("temp_50c_band_only")
    assert p.max_spread == 0.03
    assert p.min_ask_size == 5.0
    assert p.order_qty == 10.0
    assert p.mode == "band_only"


def test_basic_preset_defaults() -> None:
    p = _preset_by_name("temp_50c_basic")
    assert p.max_spread == 0.03
    assert p.min_ask_size == 5.0
    assert p.order_qty == 10.0
    assert p.mode == "basic"


def test_preset_is_frozen() -> None:
    p = _preset_by_name("temp_50c_basic")
    try:
        p.name = "changed"
        assert False, "should have raised"
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# temp_50c_band_only  (ask 0.50 - 0.60, ignores spread/liquidity)
# ---------------------------------------------------------------------------

def test_temp_50c_band_only_entry_inside_arena() -> None:
    p = _preset_by_name("temp_50c_band_only")
    assert lib.should_enter_temperature_market(preset=p, **_market(bid=0.53, ask=0.55))


def test_temp_50c_band_only_ignores_wide_spread_and_small_ask_size() -> None:
    p = _preset_by_name("temp_50c_band_only")
    assert lib.should_enter_temperature_market(
        preset=p,
        **_market(bid=0.10, ask=0.55, ask_size=1.0),
    )


def test_temp_50c_band_only_rejected_above_arena() -> None:
    p = _preset_by_name("temp_50c_band_only")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.58, ask=0.60))


# ---------------------------------------------------------------------------
# temp_50c_basic  (ask 0.50 - 0.60)
# ---------------------------------------------------------------------------

def test_temp_50c_basic_entry_inside_arena() -> None:
    p = _preset_by_name("temp_50c_basic")
    assert lib.should_enter_temperature_market(preset=p, **_market(bid=0.53, ask=0.55))


def test_temp_50c_basic_rejected_below_arena() -> None:
    p = _preset_by_name("temp_50c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.47, ask=0.49))


def test_temp_50c_basic_rejected_above_arena() -> None:
    p = _preset_by_name("temp_50c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.58, ask=0.60))


def test_temp_50c_basic_rejected_spread_too_wide() -> None:
    p = _preset_by_name("temp_50c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.50, ask=0.55))


def test_temp_50c_basic_rejected_ask_size_too_small() -> None:
    p = _preset_by_name("temp_50c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.52, ask=0.55, ask_size=4.0))


# ---------------------------------------------------------------------------
# temp_60c_basic  (ask 0.60 - 0.70)
# ---------------------------------------------------------------------------

def test_temp_60c_basic_entry_inside_arena() -> None:
    p = _preset_by_name("temp_60c_basic")
    assert lib.should_enter_temperature_market(preset=p, **_market(bid=0.63, ask=0.65))


def test_temp_60c_basic_rejected_below_arena() -> None:
    p = _preset_by_name("temp_60c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.57, ask=0.59))


def test_temp_60c_basic_rejected_above_arena() -> None:
    p = _preset_by_name("temp_60c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.68, ask=0.70))


def test_temp_60c_basic_rejected_spread_too_wide() -> None:
    p = _preset_by_name("temp_60c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.60, ask=0.65))


def test_temp_60c_basic_rejected_ask_size_too_small() -> None:
    p = _preset_by_name("temp_60c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.63, ask=0.65, ask_size=3.0))


# ---------------------------------------------------------------------------
# temp_70c_basic  (ask 0.70 - 0.80)
# ---------------------------------------------------------------------------

def test_temp_70c_basic_entry_inside_arena() -> None:
    p = _preset_by_name("temp_70c_basic")
    assert lib.should_enter_temperature_market(preset=p, **_market(bid=0.73, ask=0.75))


def test_temp_70c_basic_rejected_below_arena() -> None:
    p = _preset_by_name("temp_70c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.67, ask=0.69))


def test_temp_70c_basic_rejected_above_arena() -> None:
    p = _preset_by_name("temp_70c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.78, ask=0.80))


def test_temp_70c_basic_rejected_spread_too_wide() -> None:
    p = _preset_by_name("temp_70c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.70, ask=0.75))


def test_temp_70c_basic_rejected_ask_size_too_small() -> None:
    p = _preset_by_name("temp_70c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.73, ask=0.75, ask_size=2.0))


# ---------------------------------------------------------------------------
# temp_80c_basic  (ask 0.80 - 0.90)
# ---------------------------------------------------------------------------

def test_temp_80c_basic_entry_inside_arena() -> None:
    p = _preset_by_name("temp_80c_basic")
    assert lib.should_enter_temperature_market(preset=p, **_market(bid=0.83, ask=0.85))


def test_temp_80c_basic_rejected_below_arena() -> None:
    p = _preset_by_name("temp_80c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.77, ask=0.79))


def test_temp_80c_basic_rejected_above_arena() -> None:
    p = _preset_by_name("temp_80c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.88, ask=0.90))


def test_temp_80c_basic_rejected_spread_too_wide() -> None:
    p = _preset_by_name("temp_80c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.80, ask=0.85))


def test_temp_80c_basic_rejected_ask_size_too_small() -> None:
    p = _preset_by_name("temp_80c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.83, ask=0.85, ask_size=1.0))


# ---------------------------------------------------------------------------
# temp_90c_basic  (ask 0.90 - 0.98, i.e. < 0.98; take_profit_price=0.99)
# ---------------------------------------------------------------------------

def test_temp_90c_basic_entry_inside_arena() -> None:
    p = _preset_by_name("temp_90c_basic")
    assert lib.should_enter_temperature_market(preset=p, **_market(bid=0.93, ask=0.95))


def test_temp_90c_basic_entry_at_upper_boundary() -> None:
    # max_ask=0.98 is exclusive: ask=0.979 enters, ask=0.98 is rejected
    p = _preset_by_name("temp_90c_basic")
    assert lib.should_enter_temperature_market(preset=p, **_market(bid=0.96, ask=0.979))
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.96, ask=0.98))


def test_temp_90c_basic_rejected_below_arena() -> None:
    p = _preset_by_name("temp_90c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.87, ask=0.89))


def test_temp_90c_basic_rejected_above_arena() -> None:
    p = _preset_by_name("temp_90c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.97, ask=0.99))


def test_temp_90c_basic_rejected_spread_too_wide() -> None:
    p = _preset_by_name("temp_90c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.90, ask=0.95))


def test_temp_90c_basic_rejected_ask_size_too_small() -> None:
    p = _preset_by_name("temp_90c_basic")
    assert not lib.should_enter_temperature_market(preset=p, **_market(bid=0.93, ask=0.95, ask_size=4.9))


# ---------------------------------------------------------------------------
# temp_70c_support  (basic + bid-side liquidity dominance)
# ---------------------------------------------------------------------------

def test_temp_70c_support_entry_with_bid_dominance() -> None:
    p = _preset_by_name("temp_70c_support")
    assert lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.73, ask=0.75, bid_size=20.0, ask_size=10.0),
    )


def test_temp_70c_support_rejected_without_bid_dominance() -> None:
    p = _preset_by_name("temp_70c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.73, ask=0.75, bid_size=10.0, ask_size=10.0),
    )


def test_temp_70c_support_rejected_below_arena() -> None:
    p = _preset_by_name("temp_70c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.67, ask=0.69, bid_size=20.0, ask_size=10.0),
    )


def test_temp_70c_support_rejected_above_arena() -> None:
    p = _preset_by_name("temp_70c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.78, ask=0.80, bid_size=20.0, ask_size=10.0),
    )


def test_temp_70c_support_rejected_spread_too_wide() -> None:
    p = _preset_by_name("temp_70c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.70, ask=0.75, bid_size=20.0, ask_size=10.0),
    )


def test_temp_70c_support_rejected_ask_size_too_small() -> None:
    p = _preset_by_name("temp_70c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.73, ask=0.75, bid_size=20.0, ask_size=4.0),
    )


# ---------------------------------------------------------------------------
# temp_80c_support
# ---------------------------------------------------------------------------

def test_temp_80c_support_entry_with_bid_dominance() -> None:
    p = _preset_by_name("temp_80c_support")
    assert lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.83, ask=0.85, bid_size=15.0, ask_size=10.0),
    )


def test_temp_80c_support_rejected_without_bid_dominance() -> None:
    p = _preset_by_name("temp_80c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.83, ask=0.85, bid_size=10.0, ask_size=10.0),
    )


def test_temp_80c_support_rejected_below_arena() -> None:
    p = _preset_by_name("temp_80c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.77, ask=0.79, bid_size=15.0, ask_size=10.0),
    )


def test_temp_80c_support_rejected_above_arena() -> None:
    p = _preset_by_name("temp_80c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.88, ask=0.90, bid_size=15.0, ask_size=10.0),
    )


def test_temp_80c_support_rejected_spread_too_wide() -> None:
    p = _preset_by_name("temp_80c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.80, ask=0.85, bid_size=15.0, ask_size=10.0),
    )


def test_temp_80c_support_rejected_ask_size_too_small() -> None:
    p = _preset_by_name("temp_80c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.83, ask=0.85, bid_size=15.0, ask_size=4.5),
    )


# ---------------------------------------------------------------------------
# temp_90c_support
# ---------------------------------------------------------------------------

def test_temp_90c_support_entry_with_bid_dominance() -> None:
    p = _preset_by_name("temp_90c_support")
    assert lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.93, ask=0.95, bid_size=15.0, ask_size=10.0),
    )


def test_temp_90c_support_rejected_without_bid_dominance() -> None:
    p = _preset_by_name("temp_90c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.93, ask=0.95, bid_size=10.0, ask_size=10.0),
    )


def test_temp_90c_support_rejected_below_arena() -> None:
    p = _preset_by_name("temp_90c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.87, ask=0.89, bid_size=15.0, ask_size=10.0),
    )


def test_temp_90c_support_rejected_above_arena() -> None:
    p = _preset_by_name("temp_90c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.97, ask=0.99, bid_size=15.0, ask_size=10.0),
    )


def test_temp_90c_support_rejected_spread_too_wide() -> None:
    p = _preset_by_name("temp_90c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.90, ask=0.95, bid_size=15.0, ask_size=10.0),
    )


def test_temp_90c_support_rejected_ask_size_too_small() -> None:
    p = _preset_by_name("temp_90c_support")
    assert not lib.should_enter_temperature_market(
        preset=p, **_market(bid=0.93, ask=0.95, bid_size=15.0, ask_size=4.9),
    )
