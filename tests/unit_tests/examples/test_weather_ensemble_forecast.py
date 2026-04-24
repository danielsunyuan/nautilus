from __future__ import annotations

import asyncio
from datetime import date
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from examples.live.polymarket.weather_ensemble_forecast import (
    OpenMeteoEnsembleForecastClient,
    OpenMeteoEnsembleForecastConfig,
    clip_probability,
    parse_open_meteo_daily_payload,
    probability_high_above,
    probability_high_below,
    probability_low_above,
    probability_low_below,
)


def _payload() -> dict:
    return {
        "latitude": 35.68,
        "longitude": 139.76,
        "generationtime_ms": 2.4,
        "timezone": "Asia/Tokyo",
        "daily_units": {
            "time": "iso8601",
            "temperature_2m_max": "°C",
            "temperature_2m_max_member01": "°C",
            "temperature_2m_max_member02": "°C",
            "temperature_2m_max_member03": "°C",
            "temperature_2m_min": "°C",
            "temperature_2m_min_member01": "°C",
            "temperature_2m_min_member02": "°C",
            "temperature_2m_min_member03": "°C",
        },
        "daily": {
            "time": ["2026-04-23", "2026-04-24"],
            "temperature_2m_max": [21.0, 22.0],
            "temperature_2m_max_member01": [19.0, 20.0],
            "temperature_2m_max_member02": [20.0, 20.0],
            "temperature_2m_max_member03": [21.0, 22.0],
            "temperature_2m_min": [11.0, 10.0],
            "temperature_2m_min_member01": [10.0, 9.0],
            "temperature_2m_min_member02": [11.0, 10.0],
            "temperature_2m_min_member03": [12.0, 11.0],
        },
    }


def test_parse_open_meteo_daily_payload_extracts_target_day_members() -> None:
    snapshot = parse_open_meteo_daily_payload(
        _payload(),
        target_date=date(2026, 4, 24),
        source="open_meteo_ensemble",
        model_name="icon_seamless_eps",
    )

    assert snapshot is not None
    assert snapshot.target_date == date(2026, 4, 24)
    assert snapshot.temperature_unit == "C"
    assert snapshot.ensemble_high == 22.0
    assert snapshot.ensemble_low == 10.0
    assert snapshot.member_highs == (20.0, 20.0, 22.0)
    assert snapshot.member_lows == (9.0, 10.0, 11.0)
    assert snapshot.member_count == 3


def test_parse_open_meteo_daily_payload_returns_none_when_target_day_missing() -> None:
    snapshot = parse_open_meteo_daily_payload(
        _payload(),
        target_date=date(2026, 4, 26),
        source="open_meteo_ensemble",
        model_name="icon_seamless_eps",
    )

    assert snapshot is None


def test_parse_open_meteo_daily_payload_skips_malformed_member_series() -> None:
    payload = _payload()
    payload["daily"]["temperature_2m_max_member02"] = ["bad", 20.0]
    payload["daily"]["temperature_2m_min_member03"] = [12.0]

    snapshot = parse_open_meteo_daily_payload(
        payload,
        target_date=date(2026, 4, 24),
        source="open_meteo_ensemble",
        model_name="icon_seamless_eps",
    )

    assert snapshot is not None
    assert snapshot.member_highs == (20.0, 20.0)
    assert snapshot.member_lows == (9.0, 10.0)
    assert snapshot.member_count == 2


def test_probability_helpers_use_inclusive_thresholds() -> None:
    snapshot = parse_open_meteo_daily_payload(
        _payload(),
        target_date=date(2026, 4, 24),
        source="open_meteo_ensemble",
        model_name="icon_seamless_eps",
    )

    assert snapshot is not None
    assert probability_high_above(snapshot, 20.0, clip=None) == 1.0
    assert probability_high_above(snapshot, 21.0, clip=None) == 1 / 3
    assert probability_high_below(snapshot, 20.0, clip=None) == 2 / 3
    assert probability_low_above(snapshot, 10.0, clip=None) == 2 / 3
    assert probability_low_below(snapshot, 10.0, clip=None) == 2 / 3


def test_clip_probability_bounds_extremes() -> None:
    assert clip_probability(0.0, floor=0.05, ceiling=0.95) == 0.05
    assert clip_probability(1.0, floor=0.05, ceiling=0.95) == 0.95
    assert clip_probability(0.42, floor=0.05, ceiling=0.95) == 0.42


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str | float | int]]] = []

    async def get(self, url: str, *, params: dict, timeout: int, headers: dict) -> _FakeResponse:
        self.calls.append((url, params))
        return _FakeResponse(self.payload)


def test_open_meteo_client_uses_cache_for_identical_request() -> None:
    client = OpenMeteoEnsembleForecastClient(
        config=OpenMeteoEnsembleForecastConfig(cache_ttl_seconds=300),
    )
    http_client = _FakeHttpClient(_payload())

    async def _run() -> tuple[object | None, object | None]:
        first = await client.fetch_snapshot(
            http_client=http_client,
            latitude=35.68,
            longitude=139.76,
            target_date=date(2026, 4, 24),
        )
        second = await client.fetch_snapshot(
            http_client=http_client,
            latitude=35.68,
            longitude=139.76,
            target_date=date(2026, 4, 24),
        )
        return first, second

    first, second = asyncio.run(_run())

    assert first is not None
    assert second is first
    assert len(http_client.calls) == 1
