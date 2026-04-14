from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
from contextlib import contextmanager


ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def _without_repo_root_on_sys_path():
    original = list(sys.path)
    sys.path = [
        entry
        for entry in original
        if Path(entry or ".").resolve() != ROOT
    ]
    try:
        yield
    finally:
        sys.path = original


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        with _without_repo_root_on_sys_path():
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
live_strategy = _load_module(
    "examples.live.polymarket.crypto_5m_live_strategy",
    ROOT / "examples" / "live" / "polymarket" / "crypto_5m_live_strategy.py",
)


def _quote_tick(*, bid: float, ask: float, bid_size: float, ask_size: float, ts_event: int):
    return SimpleNamespace(
        bid_price=SimpleNamespace(as_double=lambda: bid),
        ask_price=SimpleNamespace(as_double=lambda: ask),
        bid_size=SimpleNamespace(as_double=lambda: bid_size),
        ask_size=SimpleNamespace(as_double=lambda: ask_size),
        ts_event=ts_event,
    )


def test_plan_quote_action_returns_entry_after_signal_threshold() -> None:
    round_start = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)
    market_end = round_start + timedelta(minutes=5)
    preset = next(p for p in strategy_library.first_wave_strategy_presets() if p.name == "entry_95")
    engine = strategy_library.PolymarketCrypto5mSignalEngine(
        preset=preset,
        token_sides={"POLYMARKET.BTC-5M-UP": "up"},
    )
    first_action, max_bid = live_strategy.plan_quote_action(
        engine=engine,
        preset=preset,
        instrument_id="POLYMARKET.BTC-5M-UP",
        token_side="up",
        market_end_time=market_end,
        now=round_start,
        bid=0.94,
        ask=0.95,
        bid_size=20.0,
        ask_size=20.0,
        open_position=None,
        has_inflight_orders=False,
        max_bid_seen=0.0,
    )
    second_action, _ = live_strategy.plan_quote_action(
        engine=engine,
        preset=preset,
        instrument_id="POLYMARKET.BTC-5M-UP",
        token_side="up",
        market_end_time=market_end,
        now=round_start + timedelta(seconds=1.2),
        bid=0.94,
        ask=0.95,
        bid_size=20.0,
        ask_size=20.0,
        open_position=None,
        has_inflight_orders=False,
        max_bid_seen=max_bid,
    )

    assert first_action is None
    assert second_action is not None
    assert second_action["kind"] == "enter"
    assert second_action["signal"].token_id == "POLYMARKET.BTC-5M-UP"


def test_plan_quote_action_returns_exit_when_target_hit() -> None:
    round_start = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)
    market_end = round_start + timedelta(minutes=5)
    preset = next(p for p in strategy_library.first_wave_strategy_presets() if p.name == "entry_95")
    engine = strategy_library.PolymarketCrypto5mSignalEngine(
        preset=preset,
        token_sides={"POLYMARKET.BTC-5M-UP": "up"},
    )
    position = SimpleNamespace(
        avg_px_open=0.95,
        quantity="10",
    )

    action, max_bid = live_strategy.plan_quote_action(
        engine=engine,
        preset=preset,
        instrument_id="POLYMARKET.BTC-5M-UP",
        token_side="up",
        market_end_time=market_end,
        now=round_start + timedelta(seconds=100),
        bid=0.99,
        ask=1.0,
        bid_size=20.0,
        ask_size=20.0,
        open_position=position,
        has_inflight_orders=False,
        max_bid_seen=0.97,
    )

    assert max_bid == 0.99
    assert action is not None
    assert action["kind"] == "exit"
    assert action["reason"] == "target"
    assert action["position"] is position
