"""
Unit tests for weather_wunderground_fetcher.py

Tests the station map, utility helpers, caching, and fetch logic
(using mocked HTTP responses — no live network calls).
"""

from __future__ import annotations

import importlib.util
import sys
import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module import helper (no installed examples package)
# ---------------------------------------------------------------------------
_MOD_PATH = Path(__file__).resolve().parents[3] / "examples" / "live" / "polymarket" / "weather_wunderground_fetcher.py"
_spec = importlib.util.spec_from_file_location("weather_wunderground_fetcher", _MOD_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules["weather_wunderground_fetcher"] = mod
_spec.loader.exec_module(mod)

CITY_STATIONS = mod.CITY_STATIONS
StationObs = mod.StationObs
fetch_daily_high = mod.fetch_daily_high
fetch_all_cities = mod.fetch_all_cities
wunderground_history_url = mod.wunderground_history_url
oracle_url = mod.oracle_url
_cache = mod._cache


# ---------------------------------------------------------------------------
# Station map completeness
# ---------------------------------------------------------------------------

def test_all_50_cities_present():
    assert len(CITY_STATIONS) == 50


def test_all_cities_have_station_code():
    for city, (station, iso, unit, oracle) in CITY_STATIONS.items():
        assert len(station) >= 3, f"{city}: station code too short"
        assert len(iso) == 2, f"{city}: ISO code must be 2 chars"
        assert unit in ("F", "C"), f"{city}: unit must be F or C"
        assert oracle in ("wu", "noaa", "hko"), f"{city}: unknown oracle type"


def test_us_cities_are_fahrenheit():
    us_cities = [c for c, (_, iso, unit, _) in CITY_STATIONS.items() if iso == "US"]
    for city in us_cities:
        assert CITY_STATIONS[city][2] == "F", f"{city} should be Fahrenheit"


def test_non_us_cities_are_celsius():
    for city, (_, iso, unit, _) in CITY_STATIONS.items():
        if iso != "US":
            assert unit == "C", f"{city} should be Celsius"


def test_critical_station_corrections():
    """Verify the non-obvious station assignments that differ from naive ICAO assumptions."""
    assert CITY_STATIONS["Denver"][0] == "KBKF", "Denver must be KBKF (Buckley AFB), not KDEN"
    assert CITY_STATIONS["Jakarta"][0] == "WIHH", "Jakarta must be WIHH (Halim), not WIII"
    assert CITY_STATIONS["Lagos"][0] == "DNMM", "Lagos must be DNMM (Murtala), not DNBE"
    assert CITY_STATIONS["Paris"][0] == "LFPB", "Paris must be LFPB (Le Bourget), not LFPG"
    assert CITY_STATIONS["Taipei"][0] == "RCSS", "Taipei must be RCSS (Songshan), not RCTP"
    assert CITY_STATIONS["Moscow"][0] == "UUWW", "Moscow must be UUWW (Vnukovo), not UUDD"


def test_oracle_types():
    assert CITY_STATIONS["Istanbul"][3] == "noaa"
    assert CITY_STATIONS["Moscow"][3] == "noaa"
    assert CITY_STATIONS["Tel Aviv"][3] == "noaa"
    assert CITY_STATIONS["Hong Kong"][3] == "hko"
    assert CITY_STATIONS["NYC"][3] == "wu"
    assert CITY_STATIONS["Tokyo"][3] == "wu"


# ---------------------------------------------------------------------------
# URL helper functions
# ---------------------------------------------------------------------------

def test_wunderground_history_url_wu_city():
    url = wunderground_history_url("Tokyo")
    assert url is not None
    assert "RJTT" in url
    assert "wunderground.com" in url


def test_wunderground_history_url_noaa_city_returns_none():
    # NOAA cities don't have a WU history URL
    assert wunderground_history_url("Istanbul") is None
    assert wunderground_history_url("Moscow") is None


def test_oracle_url_wu():
    url = oracle_url("London")
    assert url is not None
    assert "EGLC" in url
    assert "wunderground" in url


def test_oracle_url_noaa():
    url = oracle_url("Istanbul")
    assert url is not None
    assert "weather.gov/wrh/timeseries" in url
    assert "LTFM" in url


def test_oracle_url_hko():
    url = oracle_url("Hong Kong")
    assert url is not None
    assert "weather.gov.hk" in url


def test_oracle_url_unknown_city():
    assert oracle_url("Atlantis") is None


# ---------------------------------------------------------------------------
# Caching behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_returns_same_obs():
    """Second call returns cached result without a new HTTP request."""
    _cache.clear()

    mock_obs = StationObs(
        city="Tokyo",
        station="RJTT",
        daily_max=22.0,
        unit="C",
        obs_count=12,
        as_of_utc=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
        oracle_type="wu",
        fetch_source="twc_historical",
    )

    with patch.object(mod, "_fetch_twc_daily_high", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_obs
        result1 = await fetch_daily_high("Tokyo")
        result2 = await fetch_daily_high("Tokyo")

    assert result1 == mock_obs
    assert result2 == mock_obs
    assert mock_fetch.call_count == 1, "Should only fetch once; second call is cached"
    _cache.clear()


@pytest.mark.asyncio
async def test_bypass_cache_re_fetches():
    _cache.clear()

    mock_obs = StationObs(
        city="London",
        station="EGLC",
        daily_max=13.0,
        unit="C",
        obs_count=8,
        as_of_utc=datetime(2026, 4, 20, 11, 0, tzinfo=UTC),
        oracle_type="wu",
        fetch_source="twc_historical",
    )

    with patch.object(mod, "_fetch_twc_daily_high", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_obs
        await fetch_daily_high("London")
        await fetch_daily_high("London", bypass_cache=True)

    assert mock_fetch.call_count == 2
    _cache.clear()


# ---------------------------------------------------------------------------
# TWC historical fetch logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_twc_daily_high_success():
    """_fetch_twc_daily_high parses obs list and returns running max."""
    twc_response = {
        "observations": [
            {"temp": 15, "max_temp": None, "valid_time_gmt": 1776610800},
            {"temp": 20, "max_temp": None, "valid_time_gmt": 1776614400},
            {"temp": 22, "max_temp": None, "valid_time_gmt": 1776618000},  # max
            {"temp": 19, "max_temp": None, "valid_time_gmt": 1776621600},
        ]
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = twc_response

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        obs = await mod._fetch_twc_daily_high(
            city="Tokyo",
            station="RJTT",
            iso="JP",
            unit="C",
            api_key="testkey",
            target_date=date(2026, 4, 20),
        )

    assert obs is not None
    assert obs.daily_max == 22.0
    assert obs.unit == "C"
    assert obs.obs_count == 4
    assert obs.station == "RJTT"
    assert obs.fetch_source == "twc_historical"


@pytest.mark.asyncio
async def test_fetch_twc_daily_high_http_error():
    """Returns None on non-200 HTTP status."""
    mock_response = MagicMock()
    mock_response.status_code = 400

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        obs = await mod._fetch_twc_daily_high(
            city="Istanbul",
            station="LTFM",
            iso="TR",
            unit="C",
            api_key="testkey",
        )

    assert obs is None


@pytest.mark.asyncio
async def test_fetch_twc_daily_high_empty_obs():
    """Returns None when observations list is empty."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"observations": []}

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        obs = await mod._fetch_twc_daily_high(
            city="NYC",
            station="KLGA",
            iso="US",
            unit="F",
            api_key="testkey",
        )

    assert obs is None


# ---------------------------------------------------------------------------
# ASOS fetch logic (Istanbul / NOAA oracle)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_noaa_daily_high_via_asos():
    """_fetch_noaa_daily_high parses CSV and returns daily max."""
    asos_csv = (
        "station,valid,tmpc\n"
        "LTFM,2026-04-20 00:20,8.00\n"
        "LTFM,2026-04-20 06:20,13.00\n"
        "LTFM,2026-04-20 12:20,18.00\n"
        "LTFM,2026-04-20 14:50,20.00\n"
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = asos_csv

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        obs = await mod._fetch_noaa_daily_high(
            city="Istanbul",
            station="LTFM",
            target_date=date(2026, 4, 20),
        )

    assert obs is not None
    assert obs.daily_max == 20.0
    assert obs.unit == "C"
    assert obs.obs_count == 4
    assert obs.oracle_type == "noaa"
    assert obs.fetch_source == "asos_csv"


# ---------------------------------------------------------------------------
# High-level fetch_daily_high dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_daily_high_unknown_city():
    result = await fetch_daily_high("Atlantis")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_daily_high_routes_noaa_to_asos():
    """Istanbul (oracle=noaa) should call _fetch_noaa_daily_high."""
    _cache.clear()

    mock_obs = StationObs(
        city="Istanbul",
        station="LTFM",
        daily_max=20.0,
        unit="C",
        obs_count=30,
        as_of_utc=datetime(2026, 4, 20, 14, 50, tzinfo=UTC),
        oracle_type="noaa",
        fetch_source="asos_csv",
    )

    with patch.object(mod, "_fetch_noaa_daily_high", new_callable=AsyncMock) as mock_noaa:
        with patch.object(mod, "_fetch_twc_daily_high", new_callable=AsyncMock) as mock_twc:
            mock_noaa.return_value = mock_obs
            result = await fetch_daily_high("Istanbul", bypass_cache=True)

    mock_noaa.assert_called_once()
    mock_twc.assert_not_called()
    assert result == mock_obs
    _cache.clear()


@pytest.mark.asyncio
async def test_fetch_daily_high_routes_hko_to_twc():
    """Hong Kong (oracle=hko) should use TWC as live proxy."""
    _cache.clear()

    mock_obs = StationObs(
        city="Hong Kong",
        station="VHHH",
        daily_max=29.0,
        unit="C",
        obs_count=47,
        as_of_utc=datetime(2026, 4, 20, 14, 0, tzinfo=UTC),
        oracle_type="hko",
        fetch_source="twc_historical",
    )

    with patch.object(mod, "_fetch_twc_daily_high", new_callable=AsyncMock) as mock_twc:
        mock_twc.return_value = mock_obs
        result = await fetch_daily_high("Hong Kong", bypass_cache=True)

    mock_twc.assert_called_once()
    assert result is not None
    assert result.oracle_type == "hko"
    _cache.clear()


@pytest.mark.asyncio
async def test_fetch_all_cities_returns_dict():
    """fetch_all_cities returns a mapping for all 50 cities."""
    _cache.clear()

    mock_obs = StationObs(
        city="NYC",
        station="KLGA",
        daily_max=47.0,
        unit="F",
        obs_count=10,
        as_of_utc=datetime(2026, 4, 20, 13, 51, tzinfo=UTC),
        oracle_type="wu",
        fetch_source="twc_historical",
    )

    async def _mock_fetch(city, **kwargs):
        return mock_obs if city == "NYC" else None

    with patch.object(mod, "fetch_daily_high", side_effect=_mock_fetch):
        results = await fetch_all_cities(bypass_cache=True)

    assert len(results) == 50
    assert results["NYC"] == mock_obs
    assert results["Tokyo"] is None  # all others return None in mock
    _cache.clear()
