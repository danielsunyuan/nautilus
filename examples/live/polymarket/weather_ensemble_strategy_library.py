from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WeatherEnsembleStrategyPreset:
    name: str
    max_entry_price: float = 0.72
    max_spread: float = 0.04
    min_ask_size: float = 5.0
    min_edge: float = 0.05
    min_confidence: float = 0.55
    target_usd_per_market: float = 5.0
    min_order_size_shares: float = 5.0
    max_stake_per_market: float = 5.25
    max_open_positions: int = 8
    max_total_open_stake: float = 40.0
    one_position_per_family: bool = True


@dataclass(frozen=True, slots=True)
class WeatherEnsembleCandidate:
    strategy_name: str
    market_slug: str
    city: str
    threshold: float
    band_type: str
    forecast_source: str
    model_yes_probability: float | None
    market_yes_price: float | None
    edge: float | None
    selected_side: str | None
    confidence: float | None
    filter_status: str
    filter_reasons: tuple[str, ...]
    condition_id: str
    yes_token_id: str
    no_token_id: str
    observation_date: str
    metric: str


def weather_ensemble_presets() -> tuple[WeatherEnsembleStrategyPreset, ...]:
    return (WeatherEnsembleStrategyPreset(name="weather_ensemble_baseline"),)


def _coerce_mapping_field(payload: Any, key: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


def normalize_candidate_payload(
    *,
    payload: Any,
    preset: WeatherEnsembleStrategyPreset,
    market_slug: str,
    city: str,
    threshold: float,
    band_type: str,
    condition_id: str,
    yes_token_id: str,
    no_token_id: str,
    observation_date: str,
    metric: str,
) -> WeatherEnsembleCandidate:
    if isinstance(payload, WeatherEnsembleCandidate):
        return apply_candidate_filters(preset=preset, candidate=payload)

    reasons = _coerce_mapping_field(payload, "filter_reasons", ()) or ()
    if isinstance(reasons, str):
        reasons = (reasons,)
    else:
        reasons = tuple(str(reason) for reason in reasons)

    candidate = WeatherEnsembleCandidate(
        strategy_name=str(_coerce_mapping_field(payload, "strategy_name", preset.name)),
        market_slug=str(_coerce_mapping_field(payload, "market_slug", market_slug)),
        city=str(_coerce_mapping_field(payload, "city", city)),
        threshold=float(_coerce_mapping_field(payload, "threshold", threshold)),
        band_type=str(_coerce_mapping_field(payload, "band_type", band_type)),
        forecast_source=str(_coerce_mapping_field(payload, "forecast_source", "unknown")),
        model_yes_probability=_float_or_none(_coerce_mapping_field(payload, "model_yes_probability")),
        market_yes_price=_float_or_none(_coerce_mapping_field(payload, "market_yes_price")),
        edge=_float_or_none(_coerce_mapping_field(payload, "edge")),
        selected_side=_side_or_none(_coerce_mapping_field(payload, "selected_side")),
        confidence=_float_or_none(_coerce_mapping_field(payload, "confidence")),
        filter_status=str(_coerce_mapping_field(payload, "filter_status", "skipped")),
        filter_reasons=reasons,
        condition_id=str(_coerce_mapping_field(payload, "condition_id", condition_id)),
        yes_token_id=str(_coerce_mapping_field(payload, "yes_token_id", yes_token_id)),
        no_token_id=str(_coerce_mapping_field(payload, "no_token_id", no_token_id)),
        observation_date=str(_coerce_mapping_field(payload, "observation_date", observation_date)),
        metric=str(_coerce_mapping_field(payload, "metric", metric)),
    )
    return apply_candidate_filters(preset=preset, candidate=candidate)


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _side_or_none(value: Any) -> str | None:
    if value is None:
        return None
    side = str(value).strip().lower()
    if side in {"yes", "no"}:
        return side
    return None


def apply_candidate_filters(
    *,
    preset: WeatherEnsembleStrategyPreset,
    candidate: WeatherEnsembleCandidate,
) -> WeatherEnsembleCandidate:
    reasons = list(candidate.filter_reasons)
    status = candidate.filter_status

    if candidate.selected_side not in {"yes", "no"}:
        reasons.append("invalid_selected_side")
    if candidate.model_yes_probability is None:
        reasons.append("missing_model_probability")
    if candidate.market_yes_price is None:
        reasons.append("missing_market_yes_price")
    if candidate.edge is None:
        reasons.append("missing_edge")
    if candidate.confidence is None:
        reasons.append("missing_confidence")
    if candidate.edge is not None and candidate.edge < preset.min_edge:
        reasons.append("edge_below_minimum")
    if candidate.confidence is not None and candidate.confidence < preset.min_confidence:
        reasons.append("confidence_below_minimum")
    if candidate.market_yes_price is not None and not 0.0 <= candidate.market_yes_price <= 1.0:
        reasons.append("market_yes_price_out_of_range")
    if candidate.model_yes_probability is not None and not 0.0 <= candidate.model_yes_probability <= 1.0:
        reasons.append("model_probability_out_of_range")

    if reasons:
        status = "rejected" if status == "accepted" else status
        return WeatherEnsembleCandidate(
            strategy_name=candidate.strategy_name,
            market_slug=candidate.market_slug,
            city=candidate.city,
            threshold=candidate.threshold,
            band_type=candidate.band_type,
            forecast_source=candidate.forecast_source,
            model_yes_probability=candidate.model_yes_probability,
            market_yes_price=candidate.market_yes_price,
            edge=candidate.edge,
            selected_side=candidate.selected_side,
            confidence=candidate.confidence,
            filter_status=status,
            filter_reasons=tuple(dict.fromkeys(reasons)),
            condition_id=candidate.condition_id,
            yes_token_id=candidate.yes_token_id,
            no_token_id=candidate.no_token_id,
            observation_date=candidate.observation_date,
            metric=candidate.metric,
        )

    if status not in {"accepted", "skipped", "rejected"}:
        status = "accepted"

    return WeatherEnsembleCandidate(
        strategy_name=candidate.strategy_name,
        market_slug=candidate.market_slug,
        city=candidate.city,
        threshold=candidate.threshold,
        band_type=candidate.band_type,
        forecast_source=candidate.forecast_source,
        model_yes_probability=candidate.model_yes_probability,
        market_yes_price=candidate.market_yes_price,
        edge=candidate.edge,
        selected_side=candidate.selected_side,
        confidence=candidate.confidence,
        filter_status=status,
        filter_reasons=tuple(dict.fromkeys(reasons)),
        condition_id=candidate.condition_id,
        yes_token_id=candidate.yes_token_id,
        no_token_id=candidate.no_token_id,
        observation_date=candidate.observation_date,
        metric=candidate.metric,
    )


def candidate_to_event_row(candidate: WeatherEnsembleCandidate) -> dict[str, Any]:
    return {
        "strategy_name": candidate.strategy_name,
        "market_slug": candidate.market_slug,
        "city": candidate.city,
        "threshold": candidate.threshold,
        "band_type": candidate.band_type,
        "forecast_source": candidate.forecast_source,
        "model_yes_probability": candidate.model_yes_probability,
        "market_yes_price": candidate.market_yes_price,
        "edge": candidate.edge,
        "selected_side": candidate.selected_side,
        "confidence": candidate.confidence,
        "filter_status": candidate.filter_status,
        "filter_reasons": list(candidate.filter_reasons),
        "condition_id": candidate.condition_id,
        "yes_token_id": candidate.yes_token_id,
        "no_token_id": candidate.no_token_id,
        "observation_date": candidate.observation_date,
        "metric": candidate.metric,
    }


def should_enter_weather_ensemble_market(
    *,
    preset: WeatherEnsembleStrategyPreset,
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
) -> bool:
    if ask <= 0.0 or ask > preset.max_entry_price:
        return False
    if (ask - bid) > preset.max_spread:
        return False
    if ask_size < preset.min_ask_size:
        return False
    if bid <= 0.0 and bid_size <= 0.0:
        return False
    return True
