from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


snapshot = _load_module(
    "examples.live.polymarket.london_weather_family_b_snapshot",
    ROOT / "examples" / "live" / "polymarket" / "london_weather_family_b_snapshot.py",
)


def _historical_replay() -> pd.DataFrame:
    rows = []
    for decision_day, target_day, forecast_high, actual_high in [
        ("2026-05-20", date(2026, 5, 21), 15.0, 16.0),
        ("2026-05-21", date(2026, 5, 22), 15.0, 14.0),
        ("2026-05-22", date(2026, 5, 23), 15.0, 16.0),
    ]:
        for line in [15.0, 16.0, 17.0]:
            rows.append(
                {
                    "decision_timestamp_utc": pd.Timestamp(f"{decision_day}T06:00:00Z"),
                    "decision_local_date": date.fromisoformat(decision_day),
                    "target_local_date": target_day,
                    "forecast_horizon_days": 1,
                    "forecast_source_api": "open_meteo_forecast",
                    "forecast_source_model": "best_match",
                    "market_line": line,
                    "forecast_fetched_at_utc": pd.Timestamp(f"{decision_day}T06:00:00Z"),
                    "forecast_available_start_utc": pd.Timestamp(f"{decision_day}T06:00:00Z"),
                    "forecast_available_end_utc": pd.Timestamp(f"{decision_day}T06:00:00Z"),
                    "forecast_availability_mode": "snapshot_fetched_at",
                    "forecast_valid_start_utc": pd.Timestamp(target_day).tz_localize("UTC"),
                    "forecast_valid_end_utc": pd.Timestamp(target_day).tz_localize("UTC") + pd.Timedelta(hours=23),
                    "forecast_hour_count": 24,
                    "forecast_hour_coverage_ratio": 1.0,
                    "forecast_high_temperature_2m": forecast_high,
                    "wunderground_settlement_high_c": int(actual_high),
                    "actual_final_daily_high": actual_high,
                    "actual_outcome": int(actual_high >= line),
                },
            )
    return pd.DataFrame(rows)


def _live_forecast() -> pd.DataFrame:
    valid_times = pd.date_range("2026-05-26T00:00:00Z", periods=24, freq="h")
    return pd.DataFrame(
        {
            "fetched_at_utc": pd.Timestamp("2026-05-25T06:00:00Z"),
            "source_api": "open_meteo_forecast",
            "source_model": "best_match",
            "valid_time_utc": valid_times,
            "target_local_date": [date(2026, 5, 26)] * len(valid_times),
            "forecast_horizon_days": [1] * len(valid_times),
            "temperature_2m": [12.0] * 6 + [16.0] * 12 + [11.0] * 6,
        },
    )


def test_build_family_b_live_snapshot_scores_requested_market_lines() -> None:
    rows = snapshot.build_family_b_live_snapshot(
        historical_replay=_historical_replay(),
        live_forecast=_live_forecast(),
        decision_timestamp_utc=pd.Timestamp("2026-05-25T06:30:00Z"),
        market_lines=[15.0, 16.0],
        target_local_dates=[date(2026, 5, 26)],
    )

    assert [row["market_line"] for row in rows] == [15.0, 16.0]
    assert {row["model_version"] for row in rows} == {"family_b_forecast_error_calibrated_v1"}
    assert {row["target_local_date"] for row in rows} == {"2026-05-26"}
    assert {row["forecast_horizon_days"] for row in rows} == {1}
    assert all(0.0 <= row["predicted_probability"] <= 1.0 for row in rows)
    assert all(0.0 <= row["raw_predicted_probability"] <= 1.0 for row in rows)
    assert all(row["training_row_count"] > 0 for row in rows)


def test_build_family_b_live_snapshot_blocks_without_forecast_for_target() -> None:
    rows = snapshot.build_family_b_live_snapshot(
        historical_replay=_historical_replay(),
        live_forecast=_live_forecast(),
        decision_timestamp_utc=pd.Timestamp("2026-05-25T06:30:00Z"),
        market_lines=[15.0],
        target_local_dates=[date(2026, 5, 27)],
    )

    assert rows == []


def test_build_family_b_live_snapshot_from_research_path_uses_injected_forecast_fetcher() -> None:
    research_path = ROOT.parent / "research" / "weather"

    def fetcher(**_kwargs):
        return {
            "hourly": {
                "time": [f"2026-05-16T{hour:02d}:00" for hour in range(24)],
                "temperature_2m": [11.0] * 6 + [17.0] * 12 + [10.0] * 6,
                "relative_humidity_2m": [70.0] * 24,
                "cloud_cover": [50.0] * 24,
                "precipitation_probability": [5.0] * 24,
                "surface_pressure": [1010.0] * 24,
                "wind_speed_10m": [8.0] * 24,
            },
        }

    rows = snapshot.build_family_b_live_snapshot_from_research_path(
        research_path=research_path,
        market_lines=[16.0],
        target_local_dates=[date(2026, 5, 16)],
        decision_timestamp_utc=pd.Timestamp("2026-05-15T06:30:00Z"),
        fetch_forecast_payload=fetcher,
    )

    assert len(rows) == 1
    assert rows[0]["target_local_date"] == "2026-05-16"
    assert rows[0]["market_line"] == 16.0
    assert rows[0]["training_row_count"] > 0
