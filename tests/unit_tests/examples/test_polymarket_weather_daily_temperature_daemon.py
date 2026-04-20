from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC
from datetime import date
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock

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


def _ensure_nautilus_stubs():
    """Install lightweight stubs so daemon imports resolve without compiled extensions."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    stubs: dict[str, object] = {}

    # Core nautilus_trader stubs
    for mod_name in [
        "nautilus_trader",
        "nautilus_trader.core",
        "nautilus_trader.core.data",
        "nautilus_trader.core.nautilus_pyo3",
        "nautilus_trader.core.datetime",
        "nautilus_trader.model",
        "nautilus_trader.model.data",
        "nautilus_trader.model.enums",
        "nautilus_trader.model.identifiers",
        "nautilus_trader.model.instruments",
        "nautilus_trader.model.currencies",
        "nautilus_trader.config",
        "nautilus_trader.trading",
        "nautilus_trader.trading.strategy",
        "nautilus_trader.live",
        "nautilus_trader.live.node",
        "nautilus_trader.adapters",
        "nautilus_trader.adapters.polymarket",
        "nautilus_trader.adapters.polymarket.providers",
        "nautilus_trader.adapters.sandbox",
        "nautilus_trader.adapters.sandbox.config",
        "nautilus_trader.adapters.sandbox.factory",
    ]:
        if mod_name not in sys.modules:
            stubs[mod_name] = MagicMock()
            sys.modules[mod_name] = stubs[mod_name]

    # Provide realistic values for key symbols the daemon uses
    pm = sys.modules["nautilus_trader.adapters.polymarket"]
    pm.POLYMARKET = "POLYMARKET"

    class _FakeVenue:
        def __str__(self):
            return "POLYMARKET"
    pm.POLYMARKET_VENUE = _FakeVenue()

    class _FakeSandboxConfig:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    sys.modules["nautilus_trader.adapters.sandbox.config"].SandboxExecutionClientConfig = _FakeSandboxConfig

    class _FakeSandboxFactory:
        pass
    sys.modules["nautilus_trader.adapters.sandbox.factory"].SandboxLiveExecClientFactory = _FakeSandboxFactory

    class _FakePolymarketDataClientConfig:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    pm.PolymarketDataClientConfig = _FakePolymarketDataClientConfig

    class _FakePolymarketLiveDataClientFactory:
        pass
    pm.PolymarketLiveDataClientFactory = _FakePolymarketLiveDataClientFactory

    class _FakeInstrumentProviderConfig:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    sys.modules["nautilus_trader.adapters.polymarket.providers"].PolymarketInstrumentProviderConfig = _FakeInstrumentProviderConfig

    class _FakeCurrency:
        def __init__(self, code):
            self._code = code
        def __str__(self):
            return self._code
        def __repr__(self):
            return self._code

    usdc_pos = _FakeCurrency("USDC.e")
    sys.modules["nautilus_trader.model.currencies"].USDC_POS = usdc_pos

    class _FakeTraderId:
        def __init__(self, value):
            self.value = value
    sys.modules["nautilus_trader.model.identifiers"].TraderId = _FakeTraderId

    class _FakeInstrumentId:
        @staticmethod
        def from_str(s):
            return s
    sys.modules["nautilus_trader.model.identifiers"].InstrumentId = _FakeInstrumentId

    class _FakeStrategyId:
        def __init__(self, value):
            self.value = value
    sys.modules["nautilus_trader.model.identifiers"].StrategyId = _FakeStrategyId

    class _FakeStrategyConfig:
        class __init_subclass_kwargs__:
            pass
        def __init_subclass__(cls, **kwargs):
            pass
    sys.modules["nautilus_trader.config"].StrategyConfig = _FakeStrategyConfig

    class _FakeTradingNodeConfig:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    sys.modules["nautilus_trader.config"].TradingNodeConfig = _FakeTradingNodeConfig

    for cfg_name in ["CacheConfig", "DatabaseConfig", "LiveExecEngineConfig", "LoggingConfig", "MessageBusConfig"]:
        cls = type(cfg_name, (), {"__init__": lambda self, **kw: self.__dict__.update(kw)})
        setattr(sys.modules["nautilus_trader.config"], cfg_name, cls)

    class _FakeTradingNode:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    sys.modules["nautilus_trader.live.node"].TradingNode = _FakeTradingNode

    class _FakeStrategy:
        def __init__(self, config=None):
            self.config = config
        def __init_subclass__(cls, **kwargs):
            pass
    sys.modules["nautilus_trader.trading.strategy"].Strategy = _FakeStrategy

    sys.modules["nautilus_trader.model.enums"].OrderSide = SimpleNamespace(BUY="BUY", SELL="SELL")

    return stubs


_stubs = _ensure_nautilus_stubs()

resolver = _load_module(
    "examples.live.polymarket.weather_daily_temperature_resolver",
    ROOT / "examples" / "live" / "polymarket" / "weather_daily_temperature_resolver.py",
)

daemon = _load_module(
    "examples.live.polymarket.polymarket_weather_daily_temperature_paper_daemon",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_weather_daily_temperature_paper_daemon.py",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_market(
    *,
    slug: str = "will-the-high-temperature-nyc-april-15-2026-be-70f-or-above",
    city: str = "New York City",
    observation_date: date | None = None,
    threshold_f: float = 70.0,
    yes_token_id: str = "yes-token-001",
    no_token_id: str = "no-token-001",
) -> resolver.DailyTemperatureMarket:
    return resolver.DailyTemperatureMarket(
        slug=slug,
        condition_id=f"condition-{slug}",
        city=city,
        observation_date=observation_date or date(2026, 4, 15),
        metric="high",
        threshold_f=threshold_f,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        active=True,
        accepting_orders=True,
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Safety: daemon uses SandboxExecutionClientConfig, NOT real exec client
# ---------------------------------------------------------------------------

def test_daemon_imports_sandbox_execution_client_config() -> None:
    source = (
        ROOT / "examples" / "live" / "polymarket"
        / "polymarket_weather_daily_temperature_paper_daemon.py"
    ).read_text(encoding="utf-8")
    assert "SandboxExecutionClientConfig" in source


def test_daemon_does_not_import_polymarket_live_exec_client_factory() -> None:
    source = (
        ROOT / "examples" / "live" / "polymarket"
        / "polymarket_weather_daily_temperature_paper_daemon.py"
    ).read_text(encoding="utf-8")
    assert "PolymarketLiveExecClientFactory" not in source


def test_daemon_configures_usdc_pos_and_cash_account_type() -> None:
    source = (
        ROOT / "examples" / "live" / "polymarket"
        / "polymarket_weather_daily_temperature_paper_daemon.py"
    ).read_text(encoding="utf-8")
    assert "USDC_POS" in source
    assert 'account_type="CASH"' in source


def test_daemon_supports_max_rounds() -> None:
    source = (
        ROOT / "examples" / "live" / "polymarket"
        / "polymarket_weather_daily_temperature_paper_daemon.py"
    ).read_text(encoding="utf-8")
    assert "max_rounds" in source


def test_paper_balances_start_at_1000_usdc() -> None:
    source = (
        ROOT / "examples" / "live" / "polymarket"
        / "polymarket_weather_daily_temperature_paper_daemon.py"
    ).read_text(encoding="utf-8")
    assert "1_000" in source


def test_strategy_presets_for_supported_sets_cover_band_only_variants() -> None:
    all_presets = daemon._strategy_presets_for_set("all")
    band_only_presets = daemon._strategy_presets_for_set("band_only")
    basic_presets = daemon._strategy_presets_for_set("basic")
    support_presets = daemon._strategy_presets_for_set("support")
    live_presets = daemon._strategy_presets_for_set("live_90_basic")

    assert len(all_presets) >= 13
    assert len(band_only_presets) == 5
    assert {p.mode for p in band_only_presets} == {"band_only"}
    assert {p.mode for p in basic_presets} == {"basic"}
    assert {p.mode for p in support_presets} == {"support"}
    assert [p.name for p in live_presets] == ["temp_90c_basic"]


# ---------------------------------------------------------------------------
# Orchestration: round_start and round_end events
# ---------------------------------------------------------------------------

def test_run_daemon_once_writes_round_start_and_round_end(tmp_path: Path) -> None:
    market = _fake_market()
    output_path = tmp_path / "runs.jsonl"

    round_result = [
        {
            "event": "strategy_result",
            "strategy_name": "temp_70c_basic",
            "market_slug": market.slug,
            "asset_class": "weather",
            "weather_market_type": "daily_temperature",
            "city": market.city,
            "observation_date": str(market.observation_date),
            "arena": "temp_70c",
            "entry_price": 0.72,
            "shares": 10.0,
            "stake": 7.2,
            "pnl": 2.8,
            "resolved_outcome": "win",
            "resolved": True,
        },
    ]

    resolve_markets = AsyncMock(return_value=[market])
    run_round = AsyncMock(return_value=round_result)
    sleep_between_rounds = AsyncMock()
    writer = daemon.JsonlRunWriter(output_path)

    asyncio.run(
        daemon.run_daemon(
            preset_set="all",
            resolve_markets=resolve_markets,
            run_round=run_round,
            sleep_between_rounds=sleep_between_rounds,
            writer=writer,
            now_fn=lambda: datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
            max_rounds=1,
        ),
    )

    rows = _read_jsonl(output_path)
    events = [row["event"] for row in rows]
    assert "round_start" in events
    assert "round_end" in events
    assert events[0] == "round_start"
    assert events[-1] == "round_end"
    assert sleep_between_rounds.await_count == 0


# ---------------------------------------------------------------------------
# max_rounds respected
# ---------------------------------------------------------------------------

def test_run_daemon_respects_max_rounds(tmp_path: Path) -> None:
    market = _fake_market()
    output_path = tmp_path / "runs.jsonl"

    resolve_markets = AsyncMock(return_value=[market])
    run_round = AsyncMock(return_value=[
        {"event": "strategy_result", "strategy_name": "temp_70c_basic"},
    ])
    sleep_between_rounds = AsyncMock()

    asyncio.run(
        daemon.run_daemon(
            preset_set="all",
            resolve_markets=resolve_markets,
            run_round=run_round,
            sleep_between_rounds=sleep_between_rounds,
            writer=daemon.JsonlRunWriter(output_path),
            now_fn=lambda: datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
            max_rounds=3,
        ),
    )

    rows = _read_jsonl(output_path)
    round_starts = [r for r in rows if r["event"] == "round_start"]
    round_ends = [r for r in rows if r["event"] == "round_end"]
    assert len(round_starts) == 3
    assert len(round_ends) == 3
    assert sleep_between_rounds.await_count == 2  # no sleep after the last round


# ---------------------------------------------------------------------------
# SandboxExecutionClientConfig in build_daemon_node_config
# ---------------------------------------------------------------------------

def test_build_daemon_node_config_uses_sandbox_exec() -> None:
    config = daemon.build_daemon_node_config(
        instrument_ids=["instrument-1"],
        trader_id="PAPER-WEATHER-DAEMON",
        cache_host="redis",
        cache_port=6379,
    )
    exec_config = config.exec_clients[daemon.POLYMARKET]
    assert exec_config.venue == str(daemon.POLYMARKET_VENUE)
    assert exec_config.base_currency == "USDC.e"
    assert exec_config.account_type == "CASH"
    assert exec_config.starting_balances == ["1_000 USDC.e"]


# ---------------------------------------------------------------------------
# Error handling: resolver failure writes error event
# ---------------------------------------------------------------------------

def test_run_daemon_handles_resolver_failure(tmp_path: Path) -> None:
    output_path = tmp_path / "runs.jsonl"
    resolve_markets = AsyncMock(side_effect=RuntimeError("gamma unavailable"))
    backoff_sleep = AsyncMock()

    asyncio.run(
        daemon.run_daemon(
            preset_set="all",
            resolve_markets=resolve_markets,
            run_round=AsyncMock(),
            sleep_between_rounds=AsyncMock(),
            writer=daemon.JsonlRunWriter(output_path),
            now_fn=lambda: datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
            max_rounds=1,
            backoff_sleep=backoff_sleep,
        ),
    )

    rows = _read_jsonl(output_path)
    assert rows[0]["event"] == "error"
    assert "gamma unavailable" in rows[0]["reason"]
    assert backoff_sleep.await_count == 1


# ---------------------------------------------------------------------------
# Market discovery events
# ---------------------------------------------------------------------------

def test_run_daemon_writes_market_discovered_events(tmp_path: Path) -> None:
    market1 = _fake_market(slug="market-a", threshold_f=70.0)
    market2 = _fake_market(slug="market-b", threshold_f=80.0)
    output_path = tmp_path / "runs.jsonl"

    resolve_markets = AsyncMock(return_value=[market1, market2])
    run_round = AsyncMock(return_value=[])
    sleep_between_rounds = AsyncMock()

    asyncio.run(
        daemon.run_daemon(
            preset_set="all",
            resolve_markets=resolve_markets,
            run_round=run_round,
            sleep_between_rounds=sleep_between_rounds,
            writer=daemon.JsonlRunWriter(output_path),
            now_fn=lambda: datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
            max_rounds=1,
        ),
    )

    rows = _read_jsonl(output_path)
    discovered = [r for r in rows if r["event"] == "market_discovered"]
    assert len(discovered) == 2
    slugs = {r["market_slug"] for r in discovered}
    assert "market-a" in slugs
    assert "market-b" in slugs
