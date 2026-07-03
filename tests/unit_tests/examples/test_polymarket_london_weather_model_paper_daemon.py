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
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def _without_repo_root_on_sys_path():
    original = list(sys.path)
    sys.path = [entry for entry in original if Path(entry or ".").resolve() != ROOT]
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


def _ensure_nautilus_stubs() -> None:
    for mod_name in [
        "nautilus_trader",
        "nautilus_trader.adapters",
        "nautilus_trader.adapters.polymarket",
        "nautilus_trader.adapters.polymarket.providers",
        "nautilus_trader.adapters.sandbox",
        "nautilus_trader.adapters.sandbox.config",
        "nautilus_trader.adapters.sandbox.factory",
        "nautilus_trader.config",
        "nautilus_trader.live",
        "nautilus_trader.live.node",
        "nautilus_trader.model",
        "nautilus_trader.model.currencies",
        "nautilus_trader.model.enums",
        "nautilus_trader.model.identifiers",
        "nautilus_trader.model.instruments",
        "nautilus_trader.trading",
        "nautilus_trader.trading.strategy",
    ]:
        sys.modules.setdefault(mod_name, MagicMock())

    pm = sys.modules["nautilus_trader.adapters.polymarket"]
    pm.POLYMARKET = "POLYMARKET"
    pm.PolymarketLiveDataClientFactory = type("PolymarketLiveDataClientFactory", (), {})
    pm.PolymarketDataClientConfig = type(
        "PolymarketDataClientConfig",
        (),
        {"__init__": lambda self, **kw: self.__dict__.update(kw)},
    )

    class _FakeVenue:
        def __str__(self):
            return "POLYMARKET"

    pm.POLYMARKET_VENUE = _FakeVenue()

    sys.modules["nautilus_trader.adapters.polymarket.providers"].PolymarketInstrumentProviderConfig = type(
        "PolymarketInstrumentProviderConfig",
        (),
        {"__init__": lambda self, **kw: self.__dict__.update(kw)},
    )
    sandbox_config = sys.modules["nautilus_trader.adapters.sandbox.config"]
    sandbox_config.SandboxExecutionClientConfig = type(
        "SandboxExecutionClientConfig",
        (),
        {"__init__": lambda self, **kw: self.__dict__.update(kw)},
    )
    sys.modules["nautilus_trader.adapters.sandbox.factory"].SandboxLiveExecClientFactory = type(
        "SandboxLiveExecClientFactory",
        (),
        {},
    )

    cfg = sys.modules["nautilus_trader.config"]
    for name in [
        "CacheConfig",
        "LiveExecEngineConfig",
        "LoggingConfig",
        "MessageBusConfig",
        "TradingNodeConfig",
    ]:
        setattr(cfg, name, type(name, (), {"__init__": lambda self, **kw: self.__dict__.update(kw)}))

    class _StrategyConfigStub:
        def __init__(self, **kw):
            self.__dict__.update(kw)

        def __init_subclass__(cls, frozen: bool = False, **kwargs):
            super().__init_subclass__(**kwargs)

    cfg.StrategyConfig = _StrategyConfigStub
    sys.modules["nautilus_trader.live.node"].TradingNode = type(
        "TradingNode",
        (),
        {"__init__": lambda self, config: setattr(self, "config", config)},
    )
    sys.modules["nautilus_trader.model.currencies"].USDC_POS = "USDC.e"
    sys.modules["nautilus_trader.model.enums"].OrderSide = SimpleNamespace(BUY="BUY")
    sys.modules["nautilus_trader.model.identifiers"].InstrumentId = type(
        "InstrumentId",
        (),
        {"from_str": staticmethod(lambda value: value)},
    )
    sys.modules["nautilus_trader.model.identifiers"].StrategyId = type(
        "StrategyId",
        (),
        {"__init__": lambda self, value: setattr(self, "value", value), "__str__": lambda self: self.value},
    )
    sys.modules["nautilus_trader.model.identifiers"].TraderId = type(
        "TraderId",
        (),
        {"__init__": lambda self, value: setattr(self, "value", value)},
    )
    sys.modules["nautilus_trader.trading.strategy"].Strategy = type(
        "Strategy",
        (),
        {"__init__": lambda self, config=None: setattr(self, "config", config)},
    )


_ensure_nautilus_stubs()

lib = _load_module(
    "examples.live.polymarket.weather_ensemble_strategy_library",
    ROOT / "examples" / "live" / "polymarket" / "weather_ensemble_strategy_library.py",
)

daemon = _load_module(
    "examples.live.polymarket.polymarket_london_weather_model_paper_daemon",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_london_weather_model_paper_daemon.py",
)


def _candidate(filter_status: str = "accepted"):
    return lib.WeatherEnsembleCandidate(
        strategy_name="london_weather_model",
        market_slug="highest-temperature-in-london-on-june-1-2026-20c",
        city="London",
        threshold=20.0,
        band_type="or_higher",
        forecast_source="family_b_forecast_error_calibrated_v1",
        model_yes_probability=0.72,
        market_yes_price=0.58,
        edge=0.14,
        selected_side="yes",
        confidence=0.72,
        filter_status=filter_status,
        filter_reasons=(),
        condition_id="condition-london-20c",
        yes_token_id="yes-token",
        no_token_id="no-token",
        observation_date="2026-06-01",
        metric="high",
    )


def _no_candidate():
    return lib.WeatherEnsembleCandidate(
        strategy_name="london_weather_model",
        market_slug="highest-temperature-in-london-on-june-1-2026-25c",
        city="London",
        threshold=25.0,
        band_type="or_higher",
        forecast_source="family_b_forecast_error_calibrated_v1",
        model_yes_probability=0.20,
        market_yes_price=0.35,
        edge=0.45,
        selected_side="no",
        confidence=0.80,
        filter_status="accepted",
        filter_reasons=(),
        condition_id="condition-london-25c",
        yes_token_id="yes-token-25",
        no_token_id="no-token-25",
        observation_date="2026-06-01",
        metric="high",
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_build_output_path_uses_london_weather_namespace(tmp_path: Path) -> None:
    path = daemon.build_output_path(
        output_dir=tmp_path,
        label="paper",
        now=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
    )

    assert path == (
        tmp_path.resolve()
        / "polymarket"
        / "london_weather_model"
        / "london_weather_model_paper_20260530T120000Z.jsonl"
    )


def test_node_config_is_sandbox_only_with_polymarket_fee_model() -> None:
    config = daemon.build_daemon_node_config(instrument_ids=["condition-london-20c"])

    assert "POLYMARKET" in config.data_clients
    assert "POLYMARKET" in config.exec_clients
    sandbox = config.exec_clients["POLYMARKET"]
    assert sandbox.fee_model_path == "nautilus_trader.adapters.polymarket.fee_model.PolymarketFeeModel"
    assert not hasattr(daemon, "PolymarketLiveExecClientFactory")


def test_run_daemon_blocks_when_preflight_not_ready(tmp_path: Path) -> None:
    output_path = tmp_path / "blocked.jsonl"
    run_round = AsyncMock(return_value=[])

    asyncio.run(
        daemon.run_daemon(
            preflight={"ready_for_paper_round": False, "blocking_reasons": ["blocked"]},
            resolve_markets=AsyncMock(return_value=[]),
            build_candidates=AsyncMock(return_value=[]),
            writer=daemon.JsonlRunWriter(output_path),
            preset=daemon.london_weather_model_preset(
                min_edge=0.08,
                target_usd_per_market=1.0,
                max_total_open_stake=5.0,
            ),
            max_rounds=1,
            run_round=run_round,
            now_fn=lambda: datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        ),
    )

    rows = _read_jsonl(output_path)
    assert [row["event"] for row in rows] == ["preflight", "blocked"]
    assert run_round.await_count == 0


def test_run_daemon_writes_candidate_and_strategy_result(tmp_path: Path) -> None:
    output_path = tmp_path / "run.jsonl"
    candidate = _candidate()
    market = SimpleNamespace(
        slug=candidate.market_slug,
        city="London",
        threshold_f=20.0,
        band_type="or_higher",
        condition_id=candidate.condition_id,
    )
    run_round = AsyncMock(return_value=[daemon._result_row(candidate)])

    asyncio.run(
        daemon.run_daemon(
            preflight={"ready_for_paper_round": True},
            resolve_markets=AsyncMock(return_value=[market]),
            build_candidates=AsyncMock(return_value=[candidate]),
            writer=daemon.JsonlRunWriter(output_path),
            preset=daemon.london_weather_model_preset(
                min_edge=0.08,
                target_usd_per_market=1.0,
                max_total_open_stake=5.0,
            ),
            max_rounds=1,
            run_round=run_round,
            now_fn=lambda: datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        ),
    )

    rows = _read_jsonl(output_path)
    events = [row["event"] for row in rows]
    assert events == [
        "preflight",
        "round_start",
        "market_discovered",
        "candidate_evaluated",
        "strategy_result",
        "round_end",
    ]
    result = next(row for row in rows if row["event"] == "strategy_result")
    assert result["condition_id"] == "condition-london-20c"
    assert result["instrument_id"] == "condition-london-20c-yes-token.POLYMARKET"
    assert result["token_side"] == "yes"
    assert result["yes_token_id"] == "yes-token"
    assert result["no_token_id"] == "no-token"
    assert result["selected_side"] == "yes"
    assert result["accounting_status"] == "submitted_to_sandbox"


def test_run_paper_round_adds_live_data_and_sandbox_factories(monkeypatch) -> None:
    node = MagicMock()
    node.trader = MagicMock()
    captured: dict[str, object] = {}

    async def _fake_deadline(*, node, duration_seconds):
        captured["duration_seconds"] = duration_seconds

    monkeypatch.setattr(daemon, "_run_node_until_deadline", _fake_deadline)

    rows = asyncio.run(
        daemon.run_paper_round(
            candidates=[_candidate()],
            preset=daemon.london_weather_model_preset(
                min_edge=0.08,
                target_usd_per_market=1.0,
                max_total_open_stake=5.0,
            ),
            duration_seconds=12.0,
            node_factory=lambda config: node,
        ),
    )

    assert rows[0]["event"] == "strategy_result"
    node.trader.add_strategy.assert_called_once()
    node.add_data_client_factory.assert_called_once()
    node.add_exec_client_factory.assert_called_once()
    assert node.add_data_client_factory.call_args.args[0] == "POLYMARKET"
    assert node.add_exec_client_factory.call_args.args[0] == "POLYMARKET"
    node.build.assert_called_once()
    assert captured["duration_seconds"] == 12.0


def test_run_paper_round_routes_no_candidate_to_no_token_instrument() -> None:
    node = MagicMock()
    node.trader = MagicMock()

    async def _fake_deadline(*, node, duration_seconds):
        return None

    original_deadline = daemon._run_node_until_deadline
    daemon._run_node_until_deadline = _fake_deadline
    try:
        rows = asyncio.run(
            daemon.run_paper_round(
                candidates=[_no_candidate()],
                preset=daemon.london_weather_model_preset(
                    min_edge=0.08,
                    target_usd_per_market=1.0,
                    max_total_open_stake=5.0,
                ),
                duration_seconds=1.0,
                node_factory=lambda config: node,
            ),
        )
    finally:
        daemon._run_node_until_deadline = original_deadline

    assert rows[0]["instrument_id"] == "condition-london-25c-no-token-25.POLYMARKET"
    strategy = node.trader.add_strategy.call_args.args[0]
    assert strategy.config.instrument_id == "condition-london-25c-no-token-25.POLYMARKET"
    assert strategy.config.family_instrument_ids == (
        "condition-london-25c-yes-token-25.POLYMARKET",
        "condition-london-25c-no-token-25.POLYMARKET",
    )


def test_run_paper_round_ignores_rejected_candidates() -> None:
    node = MagicMock()
    rows = asyncio.run(
        daemon.run_paper_round(
            candidates=[_candidate(filter_status="rejected")],
            preset=daemon.london_weather_model_preset(
                min_edge=0.08,
                target_usd_per_market=1.0,
                max_total_open_stake=5.0,
            ),
            node_factory=lambda config: node,
        ),
    )

    assert rows == []
    node.trader.add_strategy.assert_not_called()
