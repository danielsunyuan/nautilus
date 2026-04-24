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


def _ensure_nautilus_stubs():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    stubs: dict[str, object] = {}
    for mod_name in [
        "nautilus_trader",
        "nautilus_trader.core",
        "nautilus_trader.core.datetime",
        "nautilus_trader.config",
        "nautilus_trader.live",
        "nautilus_trader.live.node",
        "nautilus_trader.trading",
        "nautilus_trader.trading.strategy",
        "nautilus_trader.adapters",
        "nautilus_trader.adapters.polymarket",
        "nautilus_trader.adapters.polymarket.providers",
        "nautilus_trader.adapters.sandbox",
        "nautilus_trader.adapters.sandbox.config",
        "nautilus_trader.adapters.sandbox.factory",
        "nautilus_trader.model",
        "nautilus_trader.model.currencies",
        "nautilus_trader.model.identifiers",
        "nautilus_trader.model.enums",
        "nautilus_trader.model.instruments",
        "nautilus_trader.model.data",
    ]:
        if mod_name not in sys.modules:
            stubs[mod_name] = MagicMock()
            sys.modules[mod_name] = stubs[mod_name]

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
    sys.modules["nautilus_trader.adapters.sandbox.config"].SandboxExecutionClientConfig = type(
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
    for cfg_name in [
        "CacheConfig",
        "DatabaseConfig",
        "LiveExecEngineConfig",
        "LoggingConfig",
        "MessageBusConfig",
        "TradingNodeConfig",
    ]:
        setattr(
            cfg,
            cfg_name,
            type(cfg_name, (), {"__init__": lambda self, **kw: self.__dict__.update(kw)}),
        )

    # StrategyConfig stub must accept frozen=True in subclass declarations
    class _StrategyConfigStub:
        def __init__(self, **kw):
            self.__dict__.update(kw)

        def __init_subclass__(cls, frozen: bool = False, **kwargs):
            super().__init_subclass__(**kwargs)

    cfg.StrategyConfig = _StrategyConfigStub

    sys.modules["nautilus_trader.live.node"].TradingNode = type(
        "TradingNode",
        (),
        {"__init__": lambda self, **kw: self.__dict__.update(kw)},
    )
    sys.modules["nautilus_trader.model.currencies"].USDC_POS = "USDC.e"
    sys.modules["nautilus_trader.model.enums"].OrderSide = SimpleNamespace(BUY="BUY", SELL="SELL")
    sys.modules["nautilus_trader.model.identifiers"].InstrumentId = type(
        "InstrumentId",
        (),
        {"from_str": staticmethod(lambda value: value)},
    )
    sys.modules["nautilus_trader.model.identifiers"].StrategyId = type(
        "StrategyId",
        (),
        {"__init__": lambda self, value: setattr(self, "value", value)},
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
    sys.modules["nautilus_trader.core.datetime"].unix_nanos_to_dt = lambda value: datetime.fromtimestamp(
        int(value) / 1_000_000_000,
        tz=UTC,
    )

    return stubs


_stubs = _ensure_nautilus_stubs()

resolver = _load_module(
    "examples.live.polymarket.weather_daily_temperature_resolver",
    ROOT / "examples" / "live" / "polymarket" / "weather_daily_temperature_resolver.py",
)

lib = _load_module(
    "examples.live.polymarket.weather_ensemble_strategy_library",
    ROOT / "examples" / "live" / "polymarket" / "weather_ensemble_strategy_library.py",
)

daemon = _load_module(
    "examples.live.polymarket.polymarket_weather_ensemble_paper_daemon",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_weather_ensemble_paper_daemon.py",
)


def _fake_market(
    *,
    slug: str = "tokyo-19c-april-23",
    city: str = "Tokyo",
    observation_date: date | None = None,
    threshold_f: float = 19.0,
    metric: str = "high",
    band_type: str = "exact",
) -> resolver.DailyTemperatureMarket:
    return resolver.DailyTemperatureMarket(
        slug=slug,
        condition_id=f"condition-{slug}",
        city=city,
        observation_date=observation_date or date(2026, 4, 23),
        metric=metric,
        threshold_f=threshold_f,
        yes_token_id=f"{slug}-yes",
        no_token_id=f"{slug}-no",
        active=True,
        accepting_orders=True,
        band_type=band_type,
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_build_output_path_is_isolated_under_weather_ensemble(tmp_path: Path) -> None:
    output_path = daemon.build_output_path(
        output_dir=tmp_path,
        preset_set="weather_ensemble_baseline",
        now=datetime(2026, 4, 23, 7, 45, tzinfo=UTC),
    )

    assert output_path == (
        tmp_path.resolve()
        / "polymarket"
        / "weather_ensemble"
        / "weather_ensemble_weather_ensemble_baseline_20260423T074500Z.jsonl"
    )
    assert "/runs/" not in output_path.as_posix()


def test_filter_tradeable_markets_keeps_near_term_sorted_and_capped() -> None:
    markets = [
        _fake_market(slug="past", observation_date=date(2026, 4, 22), band_type="or_higher"),
        _fake_market(slug="today-b", observation_date=date(2026, 4, 23), threshold_f=20.0, band_type="or_higher"),
        _fake_market(slug="tomorrow", observation_date=date(2026, 4, 24), threshold_f=21.0, band_type="or_lower"),
        _fake_market(slug="today-a", observation_date=date(2026, 4, 23), threshold_f=18.0, band_type="or_higher"),
    ]

    filtered = resolver.filter_tradeable_daily_temperature_markets(
        markets,
        today=date(2026, 4, 23),
        max_markets=2,
    )

    # Should keep only near-term (today/tomorrow), filter out exact band types,
    # sort by date, then band_type (or_higher first), then threshold
    assert [market.slug for market in filtered] == ["today-a", "today-b"]


def test_filter_tradeable_markets_discovers_all_mainstream_markets_without_cap() -> None:
    """Verify that removing the max_markets cap discovers all or_higher/or_lower markets."""
    markets = [
        _fake_market(slug="nyc-high-70", city="NYC", observation_date=date(2026, 4, 23), threshold_f=70.0, band_type="or_higher"),
        _fake_market(slug="london-high-16", city="London", observation_date=date(2026, 4, 23), threshold_f=16.0, band_type="or_higher"),
        _fake_market(slug="tokyo-low-5", city="Tokyo", observation_date=date(2026, 4, 23), threshold_f=5.0, band_type="or_higher"),
        _fake_market(slug="seattle-exact-50", city="Seattle", observation_date=date(2026, 4, 23), threshold_f=50.0, band_type="exact"),
        _fake_market(slug="miami-high-85", city="Miami", observation_date=date(2026, 4, 24), threshold_f=85.0, band_type="or_higher"),
    ]

    # Without max_markets cap, should discover all or_higher/or_lower markets
    filtered = resolver.filter_tradeable_daily_temperature_markets(
        markets,
        today=date(2026, 4, 23),
    )

    # Should exclude past dates, exact band types
    # Should include: nyc-high-70, london-high-16, tokyo-low-5, miami-high-85
    assert len(filtered) == 4
    slugs = [m.slug for m in filtered]
    assert "seattle-exact-50" not in slugs
    assert all(m.band_type != "exact" for m in filtered)
    # Verify sort order: date first (today before tomorrow), then or_higher first,
    # then threshold ascending (5.0, 16.0, 70.0), then city name
    assert [m.slug for m in filtered] == ["tokyo-low-5", "london-high-16", "nyc-high-70", "miami-high-85"]


def test_run_daemon_writes_skipped_row_when_no_forecast_is_available(tmp_path: Path) -> None:
    market = _fake_market()
    output_path = tmp_path / "ensemble.jsonl"
    build_candidates = AsyncMock(
        return_value=[
            lib.WeatherEnsembleCandidate(
                strategy_name="weather_ensemble_baseline",
                market_slug=market.slug,
                city=market.city,
                threshold=market.threshold_f,
                band_type=market.band_type,
                forecast_source="open-meteo-ensemble",
                model_yes_probability=None,
                market_yes_price=None,
                edge=None,
                selected_side=None,
                confidence=None,
                filter_status="skipped",
                filter_reasons=("forecast_unavailable",),
                condition_id=market.condition_id,
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                observation_date=str(market.observation_date),
                metric=market.metric,
            ),
        ],
    )
    run_round = AsyncMock(return_value=[])

    asyncio.run(
        daemon.run_daemon(
            preset_set="weather_ensemble_baseline",
            resolve_markets=AsyncMock(return_value=[market]),
            build_candidates=build_candidates,
            run_round=run_round,
            sleep_between_rounds=AsyncMock(),
            writer=daemon.JsonlRunWriter(output_path),
            now_fn=lambda: datetime(2026, 4, 23, 9, 0, tzinfo=UTC),
            max_rounds=1,
        ),
    )

    rows = _read_jsonl(output_path)
    candidate_rows = [row for row in rows if row["event"] == "candidate_evaluated"]
    assert len(candidate_rows) == 1
    assert candidate_rows[0]["filter_status"] == "skipped"
    assert candidate_rows[0]["filter_reasons"] == ["forecast_unavailable"]
    assert candidate_rows[0]["strategy_name"] == "weather_ensemble_baseline"
    assert candidate_rows[0]["market_slug"] == market.slug
    assert candidate_rows[0]["city"] == market.city
    assert candidate_rows[0]["threshold"] == market.threshold_f
    assert candidate_rows[0]["band_type"] == market.band_type
    assert "forecast_source" in candidate_rows[0]
    assert run_round.await_count == 0


def test_run_daemon_smoke_one_round_writes_stable_jsonl_shape(tmp_path: Path) -> None:
    market = _fake_market()
    output_path = tmp_path / "ensemble.jsonl"
    candidate = lib.WeatherEnsembleCandidate(
        strategy_name="weather_ensemble_baseline",
        market_slug=market.slug,
        city=market.city,
        threshold=market.threshold_f,
        band_type=market.band_type,
        forecast_source="open-meteo-ensemble",
        model_yes_probability=0.78,
        market_yes_price=0.61,
        edge=0.17,
        selected_side="yes",
        confidence=0.82,
        filter_status="accepted",
        filter_reasons=(),
        condition_id=market.condition_id,
        yes_token_id=market.yes_token_id,
        no_token_id=market.no_token_id,
        observation_date=str(market.observation_date),
        metric=market.metric,
    )
    run_round = AsyncMock(
        return_value=[
            {
                "event": "strategy_result",
                "strategy_name": "weather_ensemble_baseline",
                "market_slug": market.slug,
                "city": market.city,
                "threshold": market.threshold_f,
                "band_type": market.band_type,
                "forecast_source": "open-meteo-ensemble",
                "model_yes_probability": 0.78,
                "market_yes_price": 0.61,
                "edge": 0.17,
                "selected_side": "yes",
                "confidence": 0.82,
                "filter_status": "accepted",
                "filter_reasons": [],
                "entry_price": 0.60,
                "shares": 8.3333,
                "stake": 5.0,
                "accounting_status": "open",
            },
        ],
    )

    asyncio.run(
        daemon.run_daemon(
            preset_set="weather_ensemble_baseline",
            resolve_markets=AsyncMock(return_value=[market]),
            build_candidates=AsyncMock(return_value=[candidate]),
            run_round=run_round,
            sleep_between_rounds=AsyncMock(),
            writer=daemon.JsonlRunWriter(output_path),
            now_fn=lambda: datetime(2026, 4, 23, 9, 5, tzinfo=UTC),
            max_rounds=1,
        ),
    )

    rows = _read_jsonl(output_path)
    events = [row["event"] for row in rows]
    assert events[0] == "round_start"
    assert "market_discovered" in events
    assert "markets_filtered" in events
    assert "candidate_evaluated" in events
    assert "strategy_result" in events
    assert events[-1] == "round_end"

    strategy_row = next(row for row in rows if row["event"] == "strategy_result")
    required_keys = {
        "strategy_name",
        "market_slug",
        "city",
        "threshold",
        "band_type",
        "forecast_source",
        "model_yes_probability",
        "market_yes_price",
        "edge",
        "selected_side",
        "confidence",
        "filter_status",
        "filter_reasons",
    }
    assert required_keys.issubset(strategy_row)
    assert run_round.await_count == 1
    accepted_candidates = run_round.await_args.kwargs["candidates"]
    assert len(accepted_candidates) == 1
    assert accepted_candidates[0].market_slug == market.slug


def test_run_daemon_keeps_existing_weather_namespace_untouched(tmp_path: Path) -> None:
    legacy_runs = tmp_path / "polymarket" / "runs"
    legacy_runs.mkdir(parents=True)
    legacy_file = legacy_runs / "weather_temp_live_legacy.jsonl"
    legacy_file.write_text('{"event":"legacy"}\n', encoding="utf-8")

    market = _fake_market()
    output_path = daemon.build_output_path(
        output_dir=tmp_path,
        preset_set="weather_ensemble_baseline",
        now=datetime(2026, 4, 23, 9, 10, tzinfo=UTC),
    )

    asyncio.run(
        daemon.run_daemon(
            preset_set="weather_ensemble_baseline",
            resolve_markets=AsyncMock(return_value=[market]),
            build_candidates=AsyncMock(return_value=[]),
            run_round=AsyncMock(return_value=[]),
            sleep_between_rounds=AsyncMock(),
            writer=daemon.JsonlRunWriter(output_path),
            now_fn=lambda: datetime(2026, 4, 23, 9, 10, tzinfo=UTC),
            max_rounds=1,
        ),
    )

    assert legacy_file.read_text(encoding="utf-8") == '{"event":"legacy"}\n'
    assert output_path.exists()
    assert output_path.parent.name == "weather_ensemble"
