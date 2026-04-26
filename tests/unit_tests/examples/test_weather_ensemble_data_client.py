#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------
"""
Unit tests for Open-Meteo ensemble forecast data client.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

# Stub Nautilus imports BEFORE any imports from weather_ensemble_data_client
import types

# Create mock modules
for module_path in [
    "nautilus_trader",
    "nautilus_trader.cache",
    "nautilus_trader.cache.cache",
    "nautilus_trader.common",
    "nautilus_trader.common.config",
    "nautilus_trader.common.component",
    "nautilus_trader.core",
    "nautilus_trader.core.data",
    "nautilus_trader.data",
    "nautilus_trader.data.messages",
    "nautilus_trader.live",
    "nautilus_trader.live.data_client",
    "nautilus_trader.live.factories",
    "nautilus_trader.model",
    "nautilus_trader.model.identifiers",
]:
    if module_path not in sys.modules:
        sys.modules[module_path] = types.ModuleType(module_path)

# Mock base classes
class MockData:
    pass

from dataclasses import dataclass

class MockNautilusConfig:
    def __init_subclass__(cls, frozen=False, **kwargs):
        # Apply dataclass decorator to the subclass
        dataclass(frozen=frozen)(cls)
        return super().__init_subclass__(**kwargs)

class MockLiveDataClient:
    pass

class MockLiveDataClientFactory:
    pass

class MockClientId:
    def __init__(self, value: str):
        self.value = value

class MockVenue:
    pass

class MockCache:
    pass

class MockLiveClock:
    pass

class MockMessageBus:
    pass

# Assign to modules
sys.modules["nautilus_trader.core.data"].Data = MockData
sys.modules["nautilus_trader.common.config"].NautilusConfig = MockNautilusConfig
sys.modules["nautilus_trader.live.data_client"].LiveDataClient = MockLiveDataClient
sys.modules["nautilus_trader.live.factories"].LiveDataClientFactory = MockLiveDataClientFactory
sys.modules["nautilus_trader.model.identifiers"].ClientId = MockClientId
sys.modules["nautilus_trader.model.identifiers"].Venue = MockVenue
sys.modules["nautilus_trader.cache.cache"].Cache = MockCache
sys.modules["nautilus_trader.common.component"].LiveClock = MockLiveClock
sys.modules["nautilus_trader.common.component"].MessageBus = MockMessageBus
sys.modules["nautilus_trader.data.messages"].SubscribeData = type("SubscribeData", (), {})
sys.modules["nautilus_trader.data.messages"].UnsubscribeData = type("UnsubscribeData", (), {})

# Now import the module under test
from examples.live.polymarket.weather_ensemble_data_client import (
    EnsembleForecastData,
    OpenMeteoEnsembleDataClientConfig,
)


class TestEnsembleForecastData:
    def test_ensemble_forecast_data_ts_properties(self):
        """Test ts_event and ts_init properties."""
        ts_event = 1000000000
        ts_init = 1000000001
        data = EnsembleForecastData(
            city="NYC",
            latitude=40.7128,
            longitude=-74.0060,
            target_date="2026-04-27",
            member_highs=(20.0, 21.0, 22.0),
            member_lows=(15.0, 16.0, 17.0),
            ensemble_high=22.5,
            ensemble_low=14.5,
            model_name="icon_seamless_eps",
            source="open_meteo_ensemble",
            temperature_unit="celsius",
            ts_event=ts_event,
            ts_init=ts_init,
        )
        assert data.ts_event == ts_event
        assert data.ts_init == ts_init

    def test_ensemble_forecast_data_fields(self):
        """Test all fields are accessible."""
        member_highs = (20.0, 21.0, 22.0)
        member_lows = (15.0, 16.0, 17.0)
        data = EnsembleForecastData(
            city="Chicago",
            latitude=41.8781,
            longitude=-87.6298,
            target_date="2026-04-28",
            member_highs=member_highs,
            member_lows=member_lows,
            ensemble_high=22.5,
            ensemble_low=14.5,
            model_name="icon_seamless_eps",
            source="open_meteo_ensemble",
            temperature_unit="celsius",
            ts_event=1000000000,
            ts_init=1000000000,
        )
        assert data.city == "Chicago"
        assert data.latitude == 41.8781
        assert data.longitude == -87.6298
        assert data.target_date == "2026-04-28"
        assert data.member_highs == member_highs
        assert data.member_lows == member_lows
        assert data.ensemble_high == 22.5
        assert data.ensemble_low == 14.5
        assert data.model_name == "icon_seamless_eps"
        assert data.source == "open_meteo_ensemble"
        assert data.temperature_unit == "celsius"

    def test_ensemble_forecast_data_member_count(self):
        """Test member_count property."""
        data = EnsembleForecastData(
            city="Miami",
            latitude=25.7617,
            longitude=-80.1918,
            target_date="2026-04-27",
            member_highs=(20.0, 21.0, 22.0, 23.0),
            member_lows=(15.0, 16.0, 17.0),
            ensemble_high=23.5,
            ensemble_low=14.5,
            model_name="icon_seamless_eps",
            source="open_meteo_ensemble",
            temperature_unit="celsius",
            ts_event=1000000000,
            ts_init=1000000000,
        )
        # member_count = min(4, 3) = 3
        assert data.member_count == 3

    def test_ensemble_forecast_data_member_count_equal(self):
        """Test member_count when highs and lows have equal length."""
        data = EnsembleForecastData(
            city="LA",
            latitude=34.0522,
            longitude=-118.2437,
            target_date="2026-04-27",
            member_highs=(20.0, 21.0, 22.0),
            member_lows=(15.0, 16.0, 17.0),
            ensemble_high=22.5,
            ensemble_low=14.5,
            model_name="icon_seamless_eps",
            source="open_meteo_ensemble",
            temperature_unit="celsius",
            ts_event=1000000000,
            ts_init=1000000000,
        )
        assert data.member_count == 3


class TestOpenMeteoEnsembleDataClientConfig:
    def test_config_defaults(self):
        """Test default configuration values."""
        config = OpenMeteoEnsembleDataClientConfig()
        assert config.poll_interval_secs == 300
        assert config.base_url == "https://ensemble-api.open-meteo.com/v1/ensemble"
        assert config.model_name == "icon_seamless_eps"
        assert config.temperature_unit == "celsius"
        assert config.timezone == "GMT"
        assert config.timeout_seconds == 15.0
        assert config.forecast_days == 2

    def test_config_custom_values(self):
        """Test custom configuration values."""
        config = OpenMeteoEnsembleDataClientConfig(
            poll_interval_secs=600,
            base_url="https://custom.example.com",
            model_name="ifs_seamless",
            temperature_unit="fahrenheit",
            timezone="UTC",
            timeout_seconds=30.0,
            forecast_days=7,
        )
        assert config.poll_interval_secs == 600
        assert config.base_url == "https://custom.example.com"
        assert config.model_name == "ifs_seamless"
        assert config.temperature_unit == "fahrenheit"
        assert config.timezone == "UTC"
        assert config.timeout_seconds == 30.0
        assert config.forecast_days == 7

    def test_config_partial_override(self):
        """Test partial configuration override."""
        config = OpenMeteoEnsembleDataClientConfig(
            poll_interval_secs=450,
        )
        assert config.poll_interval_secs == 450
        assert config.base_url == "https://ensemble-api.open-meteo.com/v1/ensemble"
        assert config.model_name == "icon_seamless_eps"
