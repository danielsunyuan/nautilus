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


models = _load_module(
    "examples.live.polymarket.weather_daily_temperature_models",
    ROOT / "examples" / "live" / "polymarket" / "weather_daily_temperature_models.py",
)


# --- Arena boundary tests ---


def test_classify_price_arena_boundaries():
    assert models.classify_price_arena(0.50) == "temp_50c"
    assert models.classify_price_arena(0.599) == "temp_50c"
    assert models.classify_price_arena(0.60) == "temp_60c"
    assert models.classify_price_arena(0.70) == "temp_70c"
    assert models.classify_price_arena(0.80) == "temp_80c"
    assert models.classify_price_arena(0.90) == "temp_90c"
    assert models.classify_price_arena(0.981) is None


# --- Resolved trade classification tests ---


def test_classify_resolved_trade_counts_net_positive_as_win():
    assert models.classify_resolved_trade(resolved=True, settlement_price=1.0, pnl=2.4) == "win"
    assert models.classify_resolved_trade(resolved=True, settlement_price=0.0, pnl=-7.0) == "loss"
    assert models.classify_resolved_trade(resolved=True, settlement_price=1.0, pnl=0.0) == "loss"
    assert models.classify_resolved_trade(resolved=False, settlement_price=None, pnl=None) == "unresolved"


# --- Breakeven win-rate tests ---


def test_breakeven_win_rate():
    assert models.breakeven_win_rate(0.72) == 0.72
    assert models.breakeven_win_rate(0.90) == 0.90
    assert models.breakeven_win_rate(0.50) == 0.50
