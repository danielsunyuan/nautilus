from __future__ import annotations

import sys
import importlib.util
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock

EXAMPLES = Path(__file__).resolve().parents[3] / "examples/live/polymarket"


def _stub_nautilus():
    """Install lightweight stubs so daemon imports resolve without compiled extensions."""
    stubs = {}
    for mod_name in [
        "nautilus_trader",
        "nautilus_trader.config",
        "nautilus_trader.core",
        "nautilus_trader.core.nautilus_pyo3",
        "nautilus_trader.adapters",
        "nautilus_trader.adapters.polymarket",
        "nautilus_trader.adapters.polymarket.providers",
        "nautilus_trader.adapters.sandbox",
        "nautilus_trader.adapters.sandbox.config",
        "nautilus_trader.adapters.sandbox.factory",
        "nautilus_trader.live",
        "nautilus_trader.live.node",
        "nautilus_trader.model",
        "nautilus_trader.model.currencies",
        "nautilus_trader.model.identifiers",
    ]:
        if mod_name not in sys.modules:
            stubs[mod_name] = MagicMock()
            sys.modules[mod_name] = stubs[mod_name]

    # Core stub classes
    class _FakeStrategyConfig:
        def __init_subclass__(cls, **kwargs):
            # Accept frozen=True and other kwargs from subclasses
            pass
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _FakeStrategy:
        def __init__(self, config=None):
            self.config = config

    class _FakeCurrency:
        def __init__(self, code):
            self._code = code
        def __str__(self):
            return self._code
        def __repr__(self):
            return self._code

    class _FakeTraderId:
        def __init__(self, value):
            self.value = value

    class _FakeInstrumentId:
        @staticmethod
        def from_str(s):
            return s

    class _FakeTradingNodeConfig:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _FakeTradingNode:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
            self.cache = MagicMock()
            self.kernel = MagicMock()
            self.kernel.dispose = MagicMock()
            self.kernel.executor = None

    # Setup stubs
    sys.modules["nautilus_trader.config"].StrategyConfig = _FakeStrategyConfig
    sys.modules["nautilus_trader.trading.strategy"] = MagicMock()
    sys.modules["nautilus_trader.trading.strategy"].Strategy = _FakeStrategy
    sys.modules["nautilus_trader.model.enums"] = MagicMock()
    sys.modules["nautilus_trader.model.enums"].OrderSide = MagicMock(BUY="BUY", SELL="SELL")
    sys.modules["nautilus_trader.model.data"] = MagicMock()
    sys.modules["nautilus_trader.model.data"].QuoteTick = MagicMock()
    sys.modules["nautilus_trader.model.instruments"] = MagicMock()
    sys.modules["nautilus_trader.model.instruments"].Instrument = MagicMock()
    sys.modules["nautilus_trader.model.currencies"].USDC_POS = _FakeCurrency("USDC.e")
    sys.modules["nautilus_trader.model.identifiers"].TraderId = _FakeTraderId
    sys.modules["nautilus_trader.model.identifiers"].InstrumentId = _FakeInstrumentId
    sys.modules["nautilus_trader.config"].TradingNodeConfig = _FakeTradingNodeConfig
    sys.modules["nautilus_trader.live.node"].TradingNode = _FakeTradingNode

    # Setup polymarket stubs
    pm = sys.modules["nautilus_trader.adapters.polymarket"]
    pm.POLYMARKET = "POLYMARKET"
    pm.POLYMARKET_VENUE = MagicMock(__str__=lambda x: "POLYMARKET")
    pm.PolymarketDataClientConfig = MagicMock
    pm.PolymarketLiveDataClientFactory = MagicMock

    sys.modules["nautilus_trader.adapters.polymarket.providers"].PolymarketInstrumentProviderConfig = MagicMock
    sys.modules["nautilus_trader.adapters.sandbox.config"].SandboxExecutionClientConfig = MagicMock
    sys.modules["nautilus_trader.adapters.sandbox.factory"].SandboxLiveExecClientFactory = MagicMock

    # Setup config stubs
    for cfg_name in ["CacheConfig", "DatabaseConfig", "LiveExecEngineConfig", "LoggingConfig", "MessageBusConfig"]:
        cls = type(cfg_name, (), {"__init__": lambda self, **kw: self.__dict__.update(kw)})
        setattr(sys.modules["nautilus_trader.config"], cfg_name, cls)

    return stubs


def _load(name):
    _stub_nautilus()
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_signal(**overrides):
    base = dict(
        event="sentinel_news_signal",
        story_id="story-1",
        headline="Test",
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


def test_read_all_signals_returns_all_entries():
    _load("sentinel_signal_models")
    _load("sentinel_signal_bridge")
    _load("sentinel_signal_strategy")
    m = _load("polymarket_sentinel_news_daemon")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(_make_signal()) + "\n")
        f.write(json.dumps(_make_signal(instrument_id="PM-OTHER.POLYMARKET")) + "\n")
        f.write('{"event": "other_event"}\n')  # should be excluded
        path = f.name

    signals = m.read_all_signals(path)
    assert len(signals) == 2
    assert all(s["event"] == "sentinel_news_signal" for s in signals)


def test_read_all_signals_missing_file():
    _load("sentinel_signal_models")
    _load("sentinel_signal_bridge")
    _load("sentinel_signal_strategy")
    m = _load("polymarket_sentinel_news_daemon")
    signals = m.read_all_signals("/nonexistent/path.jsonl")
    assert signals == []


def test_group_signals_by_instrument_keeps_best_relevance():
    _load("sentinel_signal_models")
    _load("sentinel_signal_bridge")
    _load("sentinel_signal_strategy")
    m = _load("polymarket_sentinel_news_daemon")

    signals = [
        _make_signal(instrument_id="PM-A.POLYMARKET", relevance_score=0.8),
        _make_signal(instrument_id="PM-A.POLYMARKET", relevance_score=0.6),
        _make_signal(instrument_id="PM-B.POLYMARKET", relevance_score=0.5),
    ]
    grouped = m.group_signals_by_instrument(signals)
    assert set(grouped.keys()) == {"PM-A.POLYMARKET", "PM-B.POLYMARKET"}
    assert grouped["PM-A.POLYMARKET"]["relevance_score"] == 0.8


def test_build_daemon_output_path_format():
    _load("sentinel_signal_models")
    _load("sentinel_signal_bridge")
    _load("sentinel_signal_strategy")
    m = _load("polymarket_sentinel_news_daemon")

    now = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
    path = m.build_daemon_output_path(output_dir="/tmp/outputs", now=now)
    assert "sentinel" in str(path)
    assert "20260418" in str(path)
    assert str(path).endswith(".jsonl")
    assert "polymarket" in str(path).lower() or "sentinel" in str(path).lower()
