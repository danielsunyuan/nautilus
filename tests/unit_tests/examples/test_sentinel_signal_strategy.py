from __future__ import annotations
import sys, importlib.util, json, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

EXAMPLES = Path(__file__).resolve().parents[3] / "examples/live/polymarket"

def _ensure_nautilus_stubs():
    """Install lightweight stubs so strategy imports resolve without compiled extensions."""
    for mod_name in [
        "nautilus_trader",
        "nautilus_trader.config",
        "nautilus_trader.model",
        "nautilus_trader.model.data",
        "nautilus_trader.model.enums",
        "nautilus_trader.model.identifiers",
        "nautilus_trader.model.instruments",
        "nautilus_trader.trading",
        "nautilus_trader.trading.strategy",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    # StrategyConfig must handle frozen=True kwarg
    class _FakeStrategyConfig:
        def __init_subclass__(cls, **kwargs):
            pass
    sys.modules["nautilus_trader.config"].StrategyConfig = _FakeStrategyConfig

    # Strategy base class
    class _FakeStrategy:
        def __init__(self, config=None):
            self.config = config
        def __init_subclass__(cls, **kwargs):
            pass
    sys.modules["nautilus_trader.trading.strategy"].Strategy = _FakeStrategy

    # OrderSide enum
    sys.modules["nautilus_trader.model.enums"].OrderSide = SimpleNamespace(BUY="BUY", SELL="SELL")

    # Identifiers
    class _FakeInstrumentId:
        @staticmethod
        def from_str(s):
            return s
    sys.modules["nautilus_trader.model.identifiers"].InstrumentId = _FakeInstrumentId

_ensure_nautilus_stubs()

def _load(name):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_signal(**overrides):
    base = dict(
        event="sentinel_news_signal",
        story_id="story-1",
        headline="Test headline",
        category="conflict",
        market_slug="will-russia-attack",
        market_question="Will Russia attack?",
        condition_id="0xdeadbeef",
        yes_token_id="token-yes",
        no_token_id="token-no",
        instrument_id="PM-WILL-RUSSIA-ATTACK-YES-TOKENYES.POLYMARKET",
        direction="YES",
        relevance_score=0.75,
        market_end_date_iso="2026-12-31T00:00:00+00:00",
        ts_ns=1_000_000_000,
    )
    base.update(overrides)
    return base


def test_load_signals_for_instrument_filters_by_id():
    m = _load("sentinel_signal_strategy")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(_make_signal()) + "\n")
        f.write(json.dumps(_make_signal(instrument_id="PM-OTHER.POLYMARKET")) + "\n")
        path = f.name
    signals = m.load_signals_for_instrument(
        signal_path=path,
        instrument_id="PM-WILL-RUSSIA-ATTACK-YES-TOKENYES.POLYMARKET",
    )
    assert len(signals) == 1
    assert signals[0]["story_id"] == "story-1"


def test_load_signals_returns_empty_for_missing_file():
    m = _load("sentinel_signal_strategy")
    signals = m.load_signals_for_instrument(
        signal_path="/nonexistent/path/signals.jsonl",
        instrument_id="PM-WHATEVER.POLYMARKET",
    )
    assert signals == []


def test_should_enter_when_signal_present_and_ask_in_band():
    m = _load("sentinel_signal_strategy")
    sig = _make_signal(relevance_score=0.75)
    result = m.should_enter_sentinel_market(
        signal=sig, ask=0.65, min_ask=0.50, max_ask=0.80,
        min_relevance=0.50, entry_submitted=False,
    )
    assert result is True


def test_should_not_enter_if_ask_out_of_band():
    m = _load("sentinel_signal_strategy")
    sig = _make_signal(relevance_score=0.75)
    result = m.should_enter_sentinel_market(
        signal=sig, ask=0.95, min_ask=0.50, max_ask=0.80,
        min_relevance=0.50, entry_submitted=False,
    )
    assert result is False


def test_should_not_enter_if_already_submitted():
    m = _load("sentinel_signal_strategy")
    sig = _make_signal(relevance_score=0.75)
    result = m.should_enter_sentinel_market(
        signal=sig, ask=0.65, min_ask=0.50, max_ask=0.80,
        min_relevance=0.50, entry_submitted=True,
    )
    assert result is False


def test_should_not_enter_if_relevance_too_low():
    m = _load("sentinel_signal_strategy")
    sig = _make_signal(relevance_score=0.10)
    result = m.should_enter_sentinel_market(
        signal=sig, ask=0.65, min_ask=0.50, max_ask=0.80,
        min_relevance=0.50, entry_submitted=False,
    )
    assert result is False
