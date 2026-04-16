from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
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


strategy_library = _load_module(
    "examples.live.polymarket.crypto_5m_strategy_library",
    ROOT / "examples" / "live" / "polymarket" / "crypto_5m_strategy_library.py",
)


def _market_window():
    round_start = datetime(2026, 4, 13, 10, 0, tzinfo=UTC)
    market_end = round_start + timedelta(minutes=5)
    return round_start, market_end


def _engine_for(name: str):
    preset = next(p for p in strategy_library.research_strategy_presets() if p.name == name)
    return strategy_library.PolymarketCrypto5mSignalEngine(
        preset=preset,
        token_sides={"up-token": "up", "down-token": "down"},
    )


def test_entry_grid_strategy_presets_cover_89_to_95() -> None:
    presets = strategy_library.entry_grid_strategy_presets()

    assert [preset.name for preset in presets] == [
        "entry_89",
        "entry_90",
        "entry_91",
        "entry_92",
        "entry_93",
        "entry_94",
        "entry_95",
    ]
    assert [preset.entry_price for preset in presets] == [0.89, 0.90, 0.91, 0.92, 0.93, 0.94, 0.95]


def test_first_wave_strategy_presets_include_required_names() -> None:
    names = {preset.name for preset in strategy_library.first_wave_strategy_presets()}

    assert {
        "entry_95",
        "entry_90",
        "microprice_95",
        "support_ratio_95",
        "stable_quotes_95",
        "late_half_95",
        "flow_bullish_90",
    }.issubset(names)


def test_ninety_microstructure_strategy_presets_are_research_only() -> None:
    presets = strategy_library.ninety_microstructure_strategy_presets()
    research_names = {preset.name for preset in strategy_library.research_strategy_presets()}
    all_names = {preset.name for preset in strategy_library.all_strategy_presets()}

    assert [preset.name for preset in presets] == [
        "ninety_microprice_support",
        "ninety_flow_imbalance",
        "ninety_trend_confirmed",
    ]
    assert all(preset.stop_loss_price is not None for preset in presets)
    assert set(preset.name for preset in presets).issubset(research_names)
    assert not set(preset.name for preset in presets).intersection(all_names)


def test_microprice_95_requires_supportive_microprice() -> None:
    engine = _engine_for("microprice_95")
    round_start, market_end = _market_window()

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.94,
        best_ask=0.95,
        best_bid_size=80.0,
        best_ask_size=20.0,
        now=round_start,
    )

    signal = engine.entry_signal(now=round_start + timedelta(seconds=1.1), market_end=market_end)

    assert signal is not None
    assert signal.token_id == "up-token"
    assert signal.side == "up"


def test_support_ratio_95_requires_bid_dominance() -> None:
    engine = _engine_for("support_ratio_95")
    round_start, market_end = _market_window()

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.94,
        best_ask=0.95,
        best_bid_size=5.0,
        best_ask_size=10.0,
        now=round_start,
    )

    blocked = engine.entry_signal(now=round_start + timedelta(seconds=1.1), market_end=market_end)
    assert blocked is None

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.94,
        best_ask=0.95,
        best_bid_size=15.0,
        best_ask_size=10.0,
        now=round_start + timedelta(seconds=2.0),
    )

    allowed = engine.entry_signal(now=round_start + timedelta(seconds=3.1), market_end=market_end)
    assert allowed is not None
    assert allowed.token_id == "up-token"


def test_stable_quotes_95_requires_quotes_to_remain_stable_for_window() -> None:
    engine = _engine_for("stable_quotes_95")
    round_start, market_end = _market_window()

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.94,
        best_ask=0.95,
        best_bid_size=20.0,
        best_ask_size=20.0,
        now=round_start,
    )
    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.94,
        best_ask=0.95,
        best_bid_size=20.0,
        best_ask_size=20.0,
        now=round_start + timedelta(seconds=1.0),
    )

    blocked = engine.entry_signal(now=round_start + timedelta(seconds=1.5), market_end=market_end)
    assert blocked is None

    allowed = engine.entry_signal(now=round_start + timedelta(seconds=2.2), market_end=market_end)
    assert allowed is not None


def test_late_half_95_waits_until_second_half_of_round() -> None:
    engine = _engine_for("late_half_95")
    round_start, market_end = _market_window()

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.94,
        best_ask=0.95,
        best_bid_size=10.0,
        best_ask_size=10.0,
        now=round_start + timedelta(seconds=149.0),
    )

    blocked = engine.entry_signal(now=round_start + timedelta(seconds=149.5), market_end=market_end)
    assert blocked is None

    allowed = engine.entry_signal(now=round_start + timedelta(seconds=150.2), market_end=market_end)
    assert allowed is not None


def test_flow_bullish_90_requires_bid_heavy_rolling_imbalance() -> None:
    engine = _engine_for("flow_bullish_90")
    round_start, market_end = _market_window()

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.89,
        best_ask=0.90,
        best_bid_size=10.0,
        best_ask_size=10.0,
        now=round_start,
    )
    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.89,
        best_ask=0.90,
        best_bid_size=10.0,
        best_ask_size=10.0,
        now=round_start + timedelta(seconds=1.0),
    )
    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.89,
        best_ask=0.90,
        best_bid_size=10.0,
        best_ask_size=10.0,
        now=round_start + timedelta(seconds=2.0),
    )

    blocked = engine.entry_signal(now=round_start + timedelta(seconds=2.2), market_end=market_end)
    assert blocked is None

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.89,
        best_ask=0.90,
        best_bid_size=30.0,
        best_ask_size=10.0,
        now=round_start + timedelta(seconds=3.0),
    )

    allowed = engine.entry_signal(now=round_start + timedelta(seconds=3.2), market_end=market_end)
    assert allowed is not None
    assert allowed.side == "up"


def test_microprice_support_90_requires_both_support_signals() -> None:
    engine = _engine_for("microprice_support_90")
    round_start, market_end = _market_window()

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.899,
        best_ask=0.90,
        best_bid_size=12.0,
        best_ask_size=10.0,
        now=round_start,
    )

    blocked = engine.entry_signal(now=round_start + timedelta(seconds=1.2), market_end=market_end)
    assert blocked is None

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.899,
        best_ask=0.90,
        best_bid_size=18.0,
        best_ask_size=10.0,
        now=round_start + timedelta(seconds=2.0),
    )

    allowed = engine.entry_signal(now=round_start + timedelta(seconds=3.2), market_end=market_end)
    assert allowed is not None


def test_spread_switch_90_uses_tight_entry_threshold_when_spread_is_tight() -> None:
    engine = _engine_for("spread_switch_90")
    round_start, market_end = _market_window()

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.945,
        best_ask=0.95,
        best_bid_size=10.0,
        best_ask_size=10.0,
        now=round_start,
    )

    allowed = engine.entry_signal(now=round_start + timedelta(seconds=1.1), market_end=market_end)
    assert allowed is not None
    assert allowed.ask_price == 0.95


def test_momentum_95_requires_upward_reference_momentum() -> None:
    engine = _engine_for("momentum_95")
    round_start, market_end = _market_window()

    for index, mid_price in enumerate((100.0, 100.0, 100.0)):
        engine.record_reference_mid_price(
            mid_price=mid_price,
            now=round_start + timedelta(seconds=index * 10),
        )

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.94,
        best_ask=0.95,
        best_bid_size=10.0,
        best_ask_size=10.0,
        now=round_start,
    )

    blocked = engine.entry_signal(now=round_start + timedelta(seconds=30), market_end=market_end)
    assert blocked is None

    engine.record_reference_mid_price(mid_price=101.0, now=round_start + timedelta(seconds=40))
    allowed = engine.entry_signal(now=round_start + timedelta(seconds=40), market_end=market_end)
    assert allowed is not None


def test_ninety_microprice_support_requires_supportive_order_book_momentum() -> None:
    engine = _engine_for("ninety_microprice_support")
    round_start, market_end = _market_window()

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.899,
        best_ask=0.90,
        best_bid_size=18.0,
        best_ask_size=30.0,
        now=round_start,
    )

    blocked = engine.entry_signal(now=round_start + timedelta(seconds=1.1), market_end=market_end)
    assert blocked is None

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.899,
        best_ask=0.90,
        best_bid_size=30.0,
        best_ask_size=10.0,
        now=round_start + timedelta(seconds=2.0),
    )

    allowed = engine.entry_signal(now=round_start + timedelta(seconds=3.2), market_end=market_end)
    assert allowed is not None
    assert allowed.side == "up"


def test_ninety_flow_imbalance_requires_bid_heavy_flow() -> None:
    engine = _engine_for("ninety_flow_imbalance")
    round_start, market_end = _market_window()

    for index, sizes in enumerate(
        (
            (10.0, 14.0),
            (11.0, 15.0),
            (12.0, 16.0),
        ),
    ):
        engine.record_top_of_book(
            token_id="up-token",
            best_bid=0.899,
            best_ask=0.90,
            best_bid_size=sizes[0],
            best_ask_size=sizes[1],
            now=round_start + timedelta(seconds=index),
        )

    blocked = engine.entry_signal(now=round_start + timedelta(seconds=3.2), market_end=market_end)
    assert blocked is None

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.899,
        best_ask=0.90,
        best_bid_size=50.0,
        best_ask_size=1.0,
        now=round_start + timedelta(seconds=4.0),
    )

    allowed = engine.entry_signal(now=round_start + timedelta(seconds=4.2), market_end=market_end)
    assert allowed is not None
    assert allowed.side == "up"


def test_ninety_trend_confirmed_requires_late_upward_momentum() -> None:
    engine = _engine_for("ninety_trend_confirmed")
    round_start, market_end = _market_window()

    for index, mid_price in enumerate((100.0, 100.0, 100.0)):
        engine.record_reference_mid_price(
            mid_price=mid_price,
            now=round_start + timedelta(seconds=index * 10),
        )

    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.899,
        best_ask=0.90,
        best_bid_size=20.0,
        best_ask_size=18.0,
        now=round_start + timedelta(seconds=100.0),
    )

    blocked = engine.entry_signal(now=round_start + timedelta(seconds=100.2), market_end=market_end)
    assert blocked is None

    for index, mid_price in enumerate((100.0, 100.0, 101.0), start=13):
        engine.record_reference_mid_price(
            mid_price=mid_price,
            now=round_start + timedelta(seconds=index * 10),
        )
    engine.record_top_of_book(
        token_id="up-token",
        best_bid=0.899,
        best_ask=0.90,
        best_bid_size=30.0,
        best_ask_size=8.0,
        now=round_start + timedelta(seconds=151.0),
    )

    allowed = engine.entry_signal(now=round_start + timedelta(seconds=151.2), market_end=market_end)
    assert allowed is not None
    assert allowed.side == "up"


def test_effective_stop_loss_price_supports_fixed_adaptive_and_trailing_modes() -> None:
    basic_preset = next(p for p in strategy_library.all_strategy_presets() if p.name == "entry_90")
    adaptive_preset = next(
        p for p in strategy_library.all_strategy_presets() if p.name == "adaptive_10pct_90"
    )
    trailing_preset = next(
        p for p in strategy_library.all_strategy_presets() if p.name == "trailing_10pct_90"
    )

    assert strategy_library.effective_stop_loss_price(
        preset=basic_preset,
        entry_price=0.90,
        max_bid_seen=0.90,
    ) == 0.50
    assert strategy_library.effective_stop_loss_price(
        preset=adaptive_preset,
        entry_price=0.90,
        max_bid_seen=0.94,
    ) == 0.81
    assert strategy_library.effective_stop_loss_price(
        preset=trailing_preset,
        entry_price=0.90,
        max_bid_seen=0.95,
    ) == 0.855
