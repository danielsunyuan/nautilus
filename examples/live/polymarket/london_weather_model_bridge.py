from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
import importlib.util
import sys
from typing import Any

try:
    from examples.live.polymarket.weather_ensemble_strategy_library import WeatherEnsembleCandidate
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_ensemble_strategy_library"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_strategy_library.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    WeatherEnsembleCandidate = module.WeatherEnsembleCandidate


def build_london_model_candidates(
    markets: Iterable[Any],
    model_snapshot: Any,
    *,
    min_edge: float,
    preset: Any = None,
) -> list[WeatherEnsembleCandidate]:
    rows = _snapshot_rows(model_snapshot)
    probabilities = _index_probabilities(rows)
    candidates: list[WeatherEnsembleCandidate] = []

    for market in markets:
        candidate = _build_candidate(
            market=market,
            probabilities=probabilities,
            min_edge=float(min_edge),
            strategy_name=_strategy_name(preset),
        )
        candidates.append(candidate)

    return candidates


def _build_candidate(
    *,
    market: Any,
    probabilities: dict[tuple[str, float], dict[str, Any]],
    min_edge: float,
    strategy_name: str,
) -> WeatherEnsembleCandidate:
    market_slug = str(_field(market, "slug", _field(market, "market_slug", "")))
    city = str(_field(market, "city", ""))
    threshold = float(_field(market, "threshold_f", _market_line(market)))
    band_type = str(_field(market, "band_type", ""))
    observation_date = _date_key(
        _field(market, "observation_date", _field(market, "target_local_date", "")),
    )
    metric = str(_field(market, "metric", "high"))
    condition_id = str(_field(market, "condition_id", _field(market, "conditionId", "")))
    yes_token_id = str(_field(market, "yes_token_id", ""))
    no_token_id = str(_field(market, "no_token_id", ""))

    if band_type == "exact":
        return _candidate(
            strategy_name=strategy_name,
            market=market,
            threshold=threshold,
            observation_date=observation_date,
            model_yes_probability=None,
            market_yes_price=None,
            edge=None,
            selected_side=None,
            filter_status="rejected",
            filter_reasons=("unsupported_exact_bucket",),
            forecast_source="model_snapshot",
        )

    model_probability, forecast_source, reason = _model_yes_probability(
        band_type=band_type,
        observation_date=observation_date,
        threshold=threshold,
        probabilities=probabilities,
        market=market,
    )
    if reason is not None:
        return _candidate(
            strategy_name=strategy_name,
            market=market,
            threshold=threshold,
            observation_date=observation_date,
            model_yes_probability=None,
            market_yes_price=None,
            edge=None,
            selected_side=None,
            filter_status="rejected",
            filter_reasons=(reason,),
            forecast_source=forecast_source,
        )

    assert model_probability is not None
    model_probability = _clip_probability(model_probability)
    selected_side, entry_price, edge = _select_side(
        market=market,
        model_yes_probability=model_probability,
    )
    reasons = []
    if selected_side is None or entry_price is None or edge is None:
        reasons.append("missing_tradeable_quote")
    elif edge < min_edge:
        reasons.append("edge_below_minimum")

    return WeatherEnsembleCandidate(
        strategy_name=strategy_name,
        market_slug=market_slug,
        city=city,
        threshold=threshold,
        band_type=band_type,
        forecast_source=forecast_source,
        model_yes_probability=model_probability,
        # The existing candidate schema has only market_yes_price. For YES this
        # is YES ask; for NO it is the selected-side entry price, either NO ask
        # or the confirmed binary fallback price implied by YES bid.
        market_yes_price=entry_price,
        edge=edge,
        selected_side=selected_side,
        confidence=max(model_probability, 1.0 - model_probability),
        filter_status="rejected" if reasons else "accepted",
        filter_reasons=tuple(reasons),
        condition_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        observation_date=observation_date,
        metric=metric,
    )


def _model_yes_probability(
    *,
    band_type: str,
    observation_date: str,
    threshold: float,
    probabilities: dict[tuple[str, float], dict[str, Any]],
    market: Any,
) -> tuple[float | None, str, str | None]:
    if band_type == "or_higher":
        row = probabilities.get((observation_date, threshold))
        if row is None:
            return None, "model_snapshot", "missing_model_probability"
        return (
            float(row["predicted_probability"]),
            str(row.get("model_version") or "model_snapshot"),
            None,
        )

    if band_type == "or_lower":
        if not _truthy_field(market, "or_lower_complement_confirmed", "complement_confirmed"):
            return None, "model_snapshot", "unconfirmed_or_lower_semantics"
        next_row = _next_line_row(
            probabilities=probabilities,
            observation_date=observation_date,
            threshold=threshold,
        )
        if next_row is None:
            return None, "model_snapshot", "missing_next_line_probability"
        probability = 1.0 - float(next_row["predicted_probability"])
        return probability, str(next_row.get("model_version") or "model_snapshot"), None

    return None, "model_snapshot", "unsupported_band_type"


def _select_side(
    *,
    market: Any,
    model_yes_probability: float,
) -> tuple[str | None, float | None, float | None]:
    choices: list[tuple[str, float, float]] = []
    yes_ask = _float_or_none(_field(market, "yes_ask", _field(market, "best_ask", None)))
    if yes_ask is not None:
        choices.append(("yes", yes_ask, _round_edge(model_yes_probability - yes_ask)))

    no_ask = _float_or_none(_field(market, "no_ask", None))
    if no_ask is not None:
        choices.append(("no", no_ask, _round_edge((1.0 - model_yes_probability) - no_ask)))
    elif _truthy_field(market, "binary_yes_no_confirmed", "confirmed_binary_yes_no"):
        yes_bid = _float_or_none(_field(market, "yes_bid", _field(market, "best_bid", None)))
        if yes_bid is not None:
            choices.append(
                ("no", _round_edge(1.0 - yes_bid), _round_edge(yes_bid - model_yes_probability)),
            )

    if not choices:
        return None, None, None

    selected_side, entry_price, edge = max(choices, key=lambda item: item[2])
    return selected_side, entry_price, edge


def _next_line_row(
    *,
    probabilities: dict[tuple[str, float], dict[str, Any]],
    observation_date: str,
    threshold: float,
) -> dict[str, Any] | None:
    later_lines = [
        (line, row)
        for (row_date, line), row in probabilities.items()
        if row_date == observation_date and line > threshold
    ]
    if not later_lines:
        return None
    return min(later_lines, key=lambda item: item[0])[1]


def _index_probabilities(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, float], dict[str, Any]]:
    indexed = {}
    for row in rows:
        target_date = _date_key(
            _field(row, "target_local_date", _field(row, "observation_date", "")),
        )
        line = _float_or_none(_field(row, "market_line", _field(row, "threshold", None)))
        probability = _float_or_none(_field(row, "predicted_probability", None))
        if not target_date or line is None or probability is None:
            continue
        indexed[(target_date, line)] = {
            "predicted_probability": probability,
            "model_version": _field(row, "model_version", "model_snapshot"),
        }
    return indexed


def _snapshot_rows(model_snapshot: Any) -> list[dict[str, Any]]:
    if hasattr(model_snapshot, "to_dict"):
        records = model_snapshot.to_dict("records")
        return [dict(row) for row in records]
    return [dict(row) if isinstance(row, dict) else vars(row) for row in model_snapshot]


def _candidate(
    *,
    strategy_name: str,
    market: Any,
    threshold: float,
    observation_date: str,
    model_yes_probability: float | None,
    market_yes_price: float | None,
    edge: float | None,
    selected_side: str | None,
    filter_status: str,
    filter_reasons: tuple[str, ...],
    forecast_source: str,
) -> WeatherEnsembleCandidate:
    return WeatherEnsembleCandidate(
        strategy_name=strategy_name,
        market_slug=str(_field(market, "slug", _field(market, "market_slug", ""))),
        city=str(_field(market, "city", "")),
        threshold=threshold,
        band_type=str(_field(market, "band_type", "")),
        forecast_source=forecast_source,
        model_yes_probability=model_yes_probability,
        market_yes_price=market_yes_price,
        edge=edge,
        selected_side=selected_side,
        confidence=(
            None
            if model_yes_probability is None
            else max(model_yes_probability, 1.0 - model_yes_probability)
        ),
        filter_status=filter_status,
        filter_reasons=filter_reasons,
        condition_id=str(_field(market, "condition_id", _field(market, "conditionId", ""))),
        yes_token_id=str(_field(market, "yes_token_id", "")),
        no_token_id=str(_field(market, "no_token_id", "")),
        observation_date=observation_date,
        metric=str(_field(market, "metric", "high")),
    )


def _field(payload: Any, key: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _market_line(market: Any) -> Any:
    return _field(market, "threshold", _field(market, "market_line", 0.0))


def _truthy_field(payload: Any, *keys: str) -> bool:
    return any(bool(_field(payload, key, False)) for key in keys)


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _clip_probability(value: float) -> float:
    return round(min(1.0, max(0.0, float(value))), 10)


def _round_edge(value: float) -> float:
    return round(float(value), 10)


def _date_key(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _strategy_name(preset: Any) -> str:
    if preset is None:
        return "london_weather_model"
    return str(_field(preset, "name", preset))
