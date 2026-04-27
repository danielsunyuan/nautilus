from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import sys
from pathlib import Path

try:
    from examples.live.polymarket.weather_ensemble_forecast import probability_high_above
    from examples.live.polymarket.weather_ensemble_forecast import probability_high_below
    from examples.live.polymarket.weather_ensemble_forecast import probability_low_above
    from examples.live.polymarket.weather_ensemble_forecast import probability_low_below
    from examples.live.polymarket.weather_ensemble_models import EnsembleForecastSnapshot
    from examples.live.polymarket.weather_ensemble_models import WeatherEnsembleSignalDecision
    from examples.live.polymarket.weather_ensemble_models import WeatherMarketSnapshot
except ModuleNotFoundError:
    # Load weather_ensemble_models first
    module_name = "examples.live.polymarket.weather_ensemble_models"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_models.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    EnsembleForecastSnapshot = module.EnsembleForecastSnapshot
    WeatherEnsembleSignalDecision = module.WeatherEnsembleSignalDecision
    WeatherMarketSnapshot = module.WeatherMarketSnapshot

    # Load weather_ensemble_forecast
    module_name = "examples.live.polymarket.weather_ensemble_forecast"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_forecast.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    probability_high_above = module.probability_high_above
    probability_high_below = module.probability_high_below
    probability_low_above = module.probability_low_above
    probability_low_below = module.probability_low_below


@dataclass(frozen=True, slots=True)
class WeatherEnsembleSignalConfig:
    min_edge: float = 0.10
    min_convergence: float = 0.75  # ≥75% of members must agree on direction
    probability_floor: float = 0.05
    probability_ceiling: float = 0.95
    max_entry_price: float = 0.90
    same_day_only: bool = True  # only trade markets resolving today
    signal_version: str = "weather_ensemble_v2"


class WeatherEnsembleSignalEngine:
    def __init__(self, *, config: WeatherEnsembleSignalConfig):
        self.config = config

    def evaluate(
        self,
        *,
        forecast: EnsembleForecastSnapshot | None,
        market: WeatherMarketSnapshot,
    ) -> WeatherEnsembleSignalDecision:
        if forecast is None:
            return self._filtered(
                market=market,
                forecast=None,
                model_yes_probability=None,
                edge=None,
                entry_price=None,
                confidence=None,
                reasons=("missing_forecast",),
            )

        if market.band_type == "exact":
            return self._filtered(
                market=market,
                forecast=forecast,
                model_yes_probability=None,
                edge=None,
                entry_price=None,
                confidence=None,
                reasons=(f"unsupported_band_type: {market.band_type}",),
            )

        if not (0.0 <= float(market.yes_price) <= 1.0):
            return self._filtered(
                market=market,
                forecast=forecast,
                model_yes_probability=None,
                edge=None,
                entry_price=None,
                confidence=None,
                reasons=(f"invalid_yes_price: {float(market.yes_price):.4f}",),
            )

        # Convergence gate: require strong member agreement
        convergence = self._member_convergence(forecast=forecast, market=market)
        if convergence is not None and convergence < self.config.min_convergence:
            return self._filtered(
                market=market,
                forecast=forecast,
                model_yes_probability=None,
                edge=None,
                entry_price=None,
                confidence=None,
                reasons=(f"low_convergence: {convergence:.2f} < {self.config.min_convergence:.2f}",),
            )

        model_yes_probability = self._model_yes_probability(forecast=forecast, market=market)
        if model_yes_probability is None:
            return self._filtered(
                market=market,
                forecast=forecast,
                model_yes_probability=None,
                edge=None,
                entry_price=None,
                confidence=None,
                reasons=(f"unsupported_metric_band: {market.metric}/{market.band_type}",),
            )

        yes_edge = model_yes_probability - float(market.yes_price)
        if yes_edge > 0:
            selected_side = "yes"
            edge = round(yes_edge, 4)
            entry_price = float(market.yes_price)
        else:
            selected_side = "no"
            edge = round(abs(yes_edge), 4)
            entry_price = round(1.0 - float(market.yes_price), 4)

        confidence = round(abs(model_yes_probability - 0.5) * 2.0, 4)
        filter_reasons: list[str] = []
        if edge < self.config.min_edge:
            filter_reasons.append(
                f"edge_below_threshold: {edge:.4f} < {self.config.min_edge:.4f}"
            )
        if entry_price > self.config.max_entry_price:
            filter_reasons.append(
                f"entry_price_above_cap: {entry_price:.4f} > {self.config.max_entry_price:.4f}"
            )

        if filter_reasons:
            return self._filtered(
                market=market,
                forecast=forecast,
                model_yes_probability=model_yes_probability,
                edge=edge,
                entry_price=entry_price,
                confidence=confidence,
                reasons=tuple(filter_reasons),
            )

        return WeatherEnsembleSignalDecision(
            market_slug=market.market_slug,
            city=market.city,
            observation_date=market.observation_date,
            metric=str(market.metric),
            band_type=str(market.band_type),
            threshold=float(market.threshold),
            selected_side=selected_side,
            model_yes_probability=model_yes_probability,
            market_yes_price=float(market.yes_price),
            edge=edge,
            entry_price=entry_price,
            confidence=confidence,
            filter_status="actionable",
            filter_reasons=(),
            forecast_source=forecast.source,
            forecast_model=forecast.model_name,
        )

    def _member_convergence(
        self,
        *,
        forecast: EnsembleForecastSnapshot,
        market: WeatherMarketSnapshot,
    ) -> float | None:
        """Fraction of ensemble members that agree on the dominant outcome.

        Returns the max(yes_fraction, no_fraction) for the relevant metric.
        E.g., if 35/40 members say high >= threshold, convergence = 0.875.
        """
        members = forecast.member_highs if market.metric == "high" else forecast.member_lows
        if not members:
            return None

        if market.band_type == "or_higher":
            count_yes = sum(1 for v in members if v >= market.threshold)
        elif market.band_type == "or_lower":
            count_yes = sum(1 for v in members if v <= market.threshold)
        else:
            return None

        yes_frac = count_yes / len(members)
        return max(yes_frac, 1.0 - yes_frac)

    def _model_yes_probability(
        self,
        *,
        forecast: EnsembleForecastSnapshot,
        market: WeatherMarketSnapshot,
    ) -> float | None:
        clip = (self.config.probability_floor, self.config.probability_ceiling)
        if market.metric == "high" and market.band_type == "or_higher":
            return probability_high_above(forecast, market.threshold, clip=clip)
        if market.metric == "high" and market.band_type == "or_lower":
            return probability_high_below(forecast, market.threshold, clip=clip)
        if market.metric == "low" and market.band_type == "or_higher":
            return probability_low_above(forecast, market.threshold, clip=clip)
        if market.metric == "low" and market.band_type == "or_lower":
            return probability_low_below(forecast, market.threshold, clip=clip)
        return None

    def _filtered(
        self,
        *,
        market: WeatherMarketSnapshot,
        forecast: EnsembleForecastSnapshot | None,
        model_yes_probability: float | None,
        edge: float | None,
        entry_price: float | None,
        confidence: float | None,
        reasons: tuple[str, ...],
    ) -> WeatherEnsembleSignalDecision:
        return WeatherEnsembleSignalDecision(
            market_slug=market.market_slug,
            city=market.city,
            observation_date=market.observation_date,
            metric=str(market.metric),
            band_type=str(market.band_type),
            threshold=float(market.threshold),
            selected_side=None,
            model_yes_probability=model_yes_probability,
            market_yes_price=float(market.yes_price),
            edge=edge,
            entry_price=entry_price,
            confidence=confidence,
            filter_status="filtered",
            filter_reasons=reasons,
            forecast_source=forecast.source if forecast is not None else None,
            forecast_model=forecast.model_name if forecast is not None else None,
        )
