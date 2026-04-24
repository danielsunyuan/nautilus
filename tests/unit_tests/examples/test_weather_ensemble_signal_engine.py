from __future__ import annotations

from datetime import date
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from examples.live.polymarket.weather_ensemble_models import (
    EnsembleForecastSnapshot,
    WeatherMarketSnapshot,
)
from examples.live.polymarket.weather_ensemble_signal_engine import (
    WeatherEnsembleSignalConfig,
    WeatherEnsembleSignalEngine,
)


def _forecast(
    *,
    highs: tuple[float, ...] = (20.0, 21.0, 22.0),
    lows: tuple[float, ...] = (8.0, 9.0, 10.0),
) -> EnsembleForecastSnapshot:
    return EnsembleForecastSnapshot(
        source="open_meteo_ensemble",
        model_name="icon_seamless_eps",
        target_date=date(2026, 4, 24),
        latitude=35.68,
        longitude=139.76,
        timezone="Asia/Tokyo",
        temperature_unit="C",
        ensemble_high=21.0,
        ensemble_low=9.0,
        member_highs=highs,
        member_lows=lows,
        generation_time_ms=2.4,
    )


def _market(
    *,
    metric: str = "high",
    band_type: str = "or_higher",
    threshold: float = 20.0,
    yes_price: float = 0.55,
) -> WeatherMarketSnapshot:
    return WeatherMarketSnapshot(
        market_slug="tokyo-20c",
        city="Tokyo",
        observation_date=date(2026, 4, 24),
        metric=metric,
        band_type=band_type,
        threshold=threshold,
        yes_price=yes_price,
    )


def test_signal_engine_selects_yes_when_model_yes_has_edge() -> None:
    engine = WeatherEnsembleSignalEngine(
        config=WeatherEnsembleSignalConfig(min_edge=0.05),
    )

    decision = engine.evaluate(
        forecast=_forecast(highs=(20.0, 21.0, 22.0)),
        market=_market(yes_price=0.60),
    )

    assert decision.filter_status == "actionable"
    assert decision.selected_side == "yes"
    assert decision.model_yes_probability == 0.95
    assert decision.entry_price == 0.60
    assert decision.edge == 0.35
    assert decision.filter_reasons == ()


def test_signal_engine_selects_no_when_market_yes_is_overpriced() -> None:
    engine = WeatherEnsembleSignalEngine(
        config=WeatherEnsembleSignalConfig(min_edge=0.05),
    )

    decision = engine.evaluate(
        forecast=_forecast(highs=(18.0, 18.5, 19.0)),
        market=_market(threshold=20.0, yes_price=0.70),
    )

    assert decision.filter_status == "actionable"
    assert decision.selected_side == "no"
    assert decision.model_yes_probability == 0.05
    assert decision.entry_price == 0.30
    assert decision.edge == 0.65


def test_signal_engine_filters_missing_forecast() -> None:
    engine = WeatherEnsembleSignalEngine(config=WeatherEnsembleSignalConfig())

    decision = engine.evaluate(
        forecast=None,
        market=_market(),
    )

    assert decision.filter_status == "filtered"
    assert decision.selected_side is None
    assert decision.filter_reasons == ("missing_forecast",)


def test_signal_engine_filters_exact_band_market() -> None:
    engine = WeatherEnsembleSignalEngine(config=WeatherEnsembleSignalConfig())

    decision = engine.evaluate(
        forecast=_forecast(),
        market=_market(band_type="exact"),
    )

    assert decision.filter_status == "filtered"
    assert decision.selected_side is None
    assert "unsupported_band_type: exact" in decision.filter_reasons


def test_signal_engine_filters_when_edge_is_too_small() -> None:
    engine = WeatherEnsembleSignalEngine(
        config=WeatherEnsembleSignalConfig(min_edge=0.08),
    )

    decision = engine.evaluate(
        forecast=_forecast(highs=(20.0, 21.0, 19.0)),
        market=_market(yes_price=0.70),
    )

    assert decision.filter_status == "filtered"
    assert decision.selected_side is None
    assert decision.model_yes_probability == 2 / 3
    assert decision.filter_reasons == ("edge_below_threshold: 0.0333 < 0.0800",)


def test_signal_engine_uses_low_metric_distribution() -> None:
    engine = WeatherEnsembleSignalEngine(
        config=WeatherEnsembleSignalConfig(min_edge=0.05),
    )

    decision = engine.evaluate(
        forecast=_forecast(lows=(7.0, 8.0, 9.0)),
        market=_market(metric="low", band_type="or_lower", threshold=8.0, yes_price=0.40),
    )

    assert decision.filter_status == "actionable"
    assert decision.selected_side == "yes"
    assert decision.model_yes_probability == 2 / 3
    assert decision.edge == 0.2667


def test_signal_engine_applies_entry_price_cap() -> None:
    engine = WeatherEnsembleSignalEngine(
        config=WeatherEnsembleSignalConfig(min_edge=0.05, max_entry_price=0.80),
    )

    decision = engine.evaluate(
        forecast=_forecast(highs=(20.0, 21.0, 22.0)),
        market=_market(yes_price=0.90),
    )

    assert decision.filter_status == "filtered"
    assert decision.selected_side is None
    assert decision.model_yes_probability == 0.95
    assert decision.filter_reasons == ("entry_price_above_cap: 0.9000 > 0.8000",)
