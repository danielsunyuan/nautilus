"""Family B forecast-error snapshot adapter for London weather paper trading."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Callable
from datetime import date
from pathlib import Path
import sys
from typing import Any

import pandas as pd


MODEL_VERSION = "family_b_forecast_error_calibrated_v1"
RAW_MODEL_VERSION = "family_b_forecast_error_baseline_v1"
DEFAULT_SOURCE_MODEL = "best_match"
DEFAULT_SOURCE_API = "open_meteo_forecast"
TIMEZONE = "Europe/London"


def build_family_b_live_snapshot(
    *,
    historical_replay: pd.DataFrame,
    live_forecast: pd.DataFrame,
    decision_timestamp_utc: pd.Timestamp | str,
    market_lines: Iterable[float],
    target_local_dates: Iterable[date | str],
    calibration_history: pd.DataFrame | None = None,
) -> list[dict[str, Any]]:
    """Score live forecast highs with the Family B forecast-error model."""
    replay = _prepare_historical_replay(historical_replay)
    forecast = _prepare_live_forecast(live_forecast)
    if replay.empty or forecast.empty:
        return []

    decision_ts = _utc_timestamp(decision_timestamp_utc)
    decision_local_date = decision_ts.tz_convert(TIMEZONE).date()
    calibration = _prepare_calibration_history(calibration_history) if calibration_history is not None else _calibration_history(replay)
    rows: list[dict[str, Any]] = []

    for target_date in sorted({_to_date(value) for value in target_local_dates}):
        target_forecast = forecast[forecast["target_local_date"] == target_date]
        if target_forecast.empty:
            continue
        target_forecast = target_forecast[target_forecast["forecast_available_at_utc"] <= decision_ts]
        if target_forecast.empty:
            continue

        forecast_high = float(target_forecast["temperature_2m"].astype(float).max())
        horizon_days = max(0, (target_date - decision_local_date).days)
        source_api = str(target_forecast["source_api"].iloc[0])
        source_model = str(target_forecast["source_model"].iloc[0])
        forecast_fetched_at = target_forecast["fetched_at_utc"].max()
        training = _matching_training(
            replay,
            horizon_days=horizon_days,
            source_api=source_api,
            source_model=source_model,
        )
        if training.empty:
            training = replay
        if training.empty:
            continue

        for line_value in sorted({float(line) for line in market_lines}):
            raw_probability = _forecast_error_probability(
                training=training,
                forecast_high=forecast_high,
                market_line=line_value,
            )
            prior_predictions = _matching_calibration_rows(
                calibration,
                horizon_days=horizon_days,
                source_api=source_api,
                source_model=source_model,
                target_date=target_date,
            )
            probability, calibration_count = _calibrate_probability(
                raw_probability,
                prior_predictions,
            )
            rows.append(
                {
                    "decision_timestamp_utc": _isoformat_z(decision_ts),
                    "decision_local_date": decision_local_date.isoformat(),
                    "target_local_date": target_date.isoformat(),
                    "forecast_horizon_days": int(horizon_days),
                    "forecast_source_api": source_api,
                    "forecast_source_model": source_model,
                    "market_line": float(line_value),
                    "forecast_fetched_at_utc": _isoformat_z(forecast_fetched_at),
                    "forecast_high_temperature_2m": forecast_high,
                    "model_version": MODEL_VERSION,
                    "raw_model_version": RAW_MODEL_VERSION,
                    "predicted_probability": float(probability),
                    "raw_predicted_probability": float(raw_probability),
                    "training_row_count": int(len(training)),
                    "calibration_row_count": int(calibration_count),
                },
            )
    return rows


def build_family_b_live_snapshot_from_research_path(
    *,
    research_path: str | Path,
    market_lines: Iterable[float],
    target_local_dates: Iterable[date | str],
    decision_timestamp_utc: pd.Timestamp | str | None = None,
    fetch_forecast_payload: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Load research artifacts, fetch current forecast data, and score Family B rows."""
    root = Path(research_path)
    if not root.exists():
        raise FileNotFoundError(str(root))
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    from weather_research.collectors.open_meteo import fetch_forecast_payload as default_fetch
    from weather_research.collectors.open_meteo import normalize_hourly_forecast

    decision_ts = pd.Timestamp.now(tz="UTC") if decision_timestamp_utc is None else _utc_timestamp(decision_timestamp_utc)
    fetcher = fetch_forecast_payload or default_fetch
    payload = fetcher(forecast_days=4)
    live_forecast = normalize_hourly_forecast(
        payload,
        fetched_at_utc=decision_ts,
        source_api=DEFAULT_SOURCE_API,
        source_model=DEFAULT_SOURCE_MODEL,
    )
    replay_path = root / "data" / "replays" / "family_b_forecast_replay.parquet"
    historical_replay = pd.read_parquet(replay_path)
    calibration_path = root / "data" / "evaluation" / "family_b_predictions.parquet"
    calibration_history = pd.read_parquet(calibration_path) if calibration_path.exists() else None
    return build_family_b_live_snapshot(
        historical_replay=historical_replay,
        live_forecast=live_forecast,
        decision_timestamp_utc=decision_ts,
        market_lines=market_lines,
        target_local_dates=target_local_dates,
        calibration_history=calibration_history,
    )


def _prepare_historical_replay(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    required = {
        "target_local_date",
        "forecast_horizon_days",
        "forecast_source_api",
        "forecast_source_model",
        "market_line",
        "forecast_high_temperature_2m",
        "actual_final_daily_high",
        "actual_outcome",
    }
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"missing historical replay columns: {sorted(missing)}")
    out = df.copy()
    out["target_local_date"] = out["target_local_date"].map(_to_date)
    out["forecast_horizon_days"] = out["forecast_horizon_days"].astype(int)
    out["forecast_error"] = (
        out["actual_final_daily_high"].astype(float)
        - out["forecast_high_temperature_2m"].astype(float)
    )
    return out.sort_values(["target_local_date", "forecast_horizon_days", "market_line"]).reset_index(drop=True)


def _prepare_live_forecast(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    required = {"fetched_at_utc", "valid_time_utc", "target_local_date", "temperature_2m"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"missing live forecast columns: {sorted(missing)}")
    out = df.copy()
    out["fetched_at_utc"] = pd.to_datetime(out["fetched_at_utc"], utc=True)
    if "forecast_available_at_utc" not in out.columns:
        out["forecast_available_at_utc"] = out["fetched_at_utc"]
    out["forecast_available_at_utc"] = pd.to_datetime(out["forecast_available_at_utc"], utc=True)
    out["valid_time_utc"] = pd.to_datetime(out["valid_time_utc"], utc=True)
    out["target_local_date"] = out["target_local_date"].map(_to_date)
    if "source_api" not in out.columns:
        out["source_api"] = DEFAULT_SOURCE_API
    if "source_model" not in out.columns:
        out["source_model"] = DEFAULT_SOURCE_MODEL
    return out.sort_values(["target_local_date", "valid_time_utc"]).reset_index(drop=True)


def _matching_training(
    replay: pd.DataFrame,
    *,
    horizon_days: int,
    source_api: str,
    source_model: str,
) -> pd.DataFrame:
    return replay[
        (replay["forecast_horizon_days"].astype(int) == int(horizon_days))
        & (replay["forecast_source_api"] == source_api)
        & (replay["forecast_source_model"] == source_model)
    ]


def _forecast_error_probability(
    *,
    training: pd.DataFrame,
    forecast_high: float,
    market_line: float,
) -> float:
    threshold = float(market_line) - float(forecast_high)
    return float((training["forecast_error"].astype(float) >= threshold).mean())


def _calibration_history(replay: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in replay.iterrows():
        training = replay[replay["target_local_date"] < row["target_local_date"]]
        matched = _matching_training(
            training,
            horizon_days=int(row["forecast_horizon_days"]),
            source_api=str(row["forecast_source_api"]),
            source_model=str(row["forecast_source_model"]),
        )
        if matched.empty:
            matched = training
        if matched.empty:
            continue
        raw_probability = _forecast_error_probability(
            training=matched,
            forecast_high=float(row["forecast_high_temperature_2m"]),
            market_line=float(row["market_line"]),
        )
        rows.append(
            {
                "target_local_date": row["target_local_date"],
                "forecast_horizon_days": int(row["forecast_horizon_days"]),
                "forecast_source_api": row["forecast_source_api"],
                "forecast_source_model": row["forecast_source_model"],
                "raw_probability": raw_probability,
                "actual_outcome": int(row["actual_outcome"]),
            },
        )
    return pd.DataFrame(rows)


def _prepare_calibration_history(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    required = {
        "target_local_date",
        "forecast_horizon_days",
        "forecast_source_api",
        "forecast_source_model",
        "actual_outcome",
    }
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"missing calibration history columns: {sorted(missing)}")
    out = df.copy()
    out["target_local_date"] = out["target_local_date"].map(_to_date)
    out["forecast_horizon_days"] = out["forecast_horizon_days"].astype(int)
    if "raw_probability" not in out.columns:
        if "raw_predicted_probability" in out.columns:
            out["raw_probability"] = out["raw_predicted_probability"]
        elif "predicted_probability" in out.columns:
            out["raw_probability"] = out["predicted_probability"]
        else:
            raise ValueError("calibration history requires raw_probability or predicted_probability")
    out = out[out["raw_probability"].notna()]
    if "model_version" in out.columns:
        model_rows = out[out["model_version"] == MODEL_VERSION]
        if not model_rows.empty:
            out = model_rows
    return out[
        [
            "target_local_date",
            "forecast_horizon_days",
            "forecast_source_api",
            "forecast_source_model",
            "raw_probability",
            "actual_outcome",
        ]
    ].reset_index(drop=True)


def _matching_calibration_rows(
    calibration_history: pd.DataFrame,
    *,
    horizon_days: int,
    source_api: str,
    source_model: str,
    target_date: date,
) -> pd.DataFrame:
    if calibration_history.empty:
        return pd.DataFrame()
    return calibration_history[
        (calibration_history["forecast_horizon_days"].astype(int) == int(horizon_days))
        & (calibration_history["forecast_source_api"] == source_api)
        & (calibration_history["forecast_source_model"] == source_model)
        & (calibration_history["target_local_date"] < target_date)
    ]


def _calibrate_probability(
    raw_probability: float,
    prior_predictions: pd.DataFrame,
    *,
    min_bin_count: int = 20,
    blend_prior_weight: float = 4.0,
    bins: int = 10,
) -> tuple[float, int]:
    if prior_predictions.empty:
        return float(raw_probability), 0
    bin_index = min(bins - 1, int(float(raw_probability) * bins))
    lower = bin_index / bins
    upper = (bin_index + 1) / bins
    if bin_index == bins - 1:
        in_bin = prior_predictions[
            (prior_predictions["raw_probability"].astype(float) >= lower)
            & (prior_predictions["raw_probability"].astype(float) <= upper)
        ]
    else:
        in_bin = prior_predictions[
            (prior_predictions["raw_probability"].astype(float) >= lower)
            & (prior_predictions["raw_probability"].astype(float) < upper)
        ]
    if len(in_bin) < min_bin_count:
        return float(raw_probability), int(len(in_bin))
    empirical = float(in_bin["actual_outcome"].astype(float).mean())
    calibrated = (empirical * len(in_bin) + float(raw_probability) * blend_prior_weight) / (
        len(in_bin) + blend_prior_weight
    )
    return float(calibrated), int(len(in_bin))


def _utc_timestamp(value: pd.Timestamp | str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _to_date(value: date | str | object) -> date:
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _isoformat_z(value: pd.Timestamp) -> str:
    return pd.Timestamp(value).tz_convert("UTC").isoformat().replace("+00:00", "Z")
