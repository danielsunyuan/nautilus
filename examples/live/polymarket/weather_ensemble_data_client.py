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
Open-Meteo ensemble forecast data client for Nautilus TradingNode.

Polls Open-Meteo ensemble forecasts for all configured cities
and publishes EnsembleForecastData events to the message bus.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

try:
    from examples.live.polymarket.weather_ensemble_forecast import (
        parse_open_meteo_daily_payload,
    )
except ModuleNotFoundError:
    import importlib.util
    module_name = "examples.live.polymarket.weather_ensemble_forecast"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_forecast.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    parse_open_meteo_daily_payload = module.parse_open_meteo_daily_payload

import httpx
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.core.data import Data
from nautilus_trader.data.messages import SubscribeData
from nautilus_trader.data.messages import UnsubscribeData
from nautilus_trader.live.data_client import LiveDataClient
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue
from datetime import date


# City coordinates for Open-Meteo ensemble forecast (50 cities)
CITY_COORDS = {
    "NYC": (40.7128, -74.0060),
    "Chicago": (41.8781, -87.6298),
    "Miami": (25.7617, -80.1918),
    "Los Angeles": (34.0522, -118.2437),
    "San Francisco": (37.7749, -122.4194),
    "Seattle": (47.6062, -122.3321),
    "Denver": (39.7392, -104.9903),
    "Houston": (29.7604, -95.3698),
    "Dallas": (32.7767, -96.7970),
    "Austin": (30.2672, -97.7431),
    "Atlanta": (33.7490, -84.3880),
    "London": (51.5074, -0.1278),
    "Paris": (48.8566, 2.3522),
    "Madrid": (40.4168, -3.7038),
    "Amsterdam": (52.3676, 4.9041),
    "Munich": (48.1351, 11.5820),
    "Milan": (45.4642, 9.1900),
    "Warsaw": (52.2297, 21.0122),
    "Helsinki": (60.1695, 24.9354),
    "Ankara": (39.9334, 32.8597),
    "Tokyo": (35.6762, 139.6503),
    "Seoul": (37.5665, 126.9780),
    "Busan": (35.1796, 129.0756),
    "Taipei": (25.0330, 121.5654),
    "Singapore": (1.3521, 103.8198),
    "Kuala Lumpur": (3.1390, 101.6869),
    "Jakarta": (-6.2088, 106.8456),
    "Manila": (14.5994, 120.9842),
    "Beijing": (39.9042, 116.4074),
    "Shanghai": (31.2304, 121.4737),
    "Shenzhen": (22.5431, 114.0579),
    "Guangzhou": (23.1291, 113.2644),
    "Chongqing": (29.4316, 106.9123),
    "Chengdu": (30.5728, 104.0668),
    "Wuhan": (30.5928, 114.3055),
    "Lucknow": (26.8467, 80.9462),
    "Karachi": (24.8607, 67.0011),
    "Jeddah": (21.5433, 39.1728),
    "Lagos": (6.5244, 3.3792),
    "Cape Town": (-33.9249, 18.4241),
    "Buenos Aires": (-34.6037, -58.3816),
    "Sao Paulo": (-23.5505, -46.6333),
    "Mexico City": (19.4326, -99.1332),
    "Toronto": (43.6532, -79.3832),
    "Panama City": (8.9824, -79.5199),
    "Wellington": (-41.2865, 174.7762),
    "Istanbul": (41.0082, 28.9784),
    "Moscow": (55.7558, 37.6173),
    "Tel Aviv": (32.0853, 34.7818),
    "Hong Kong": (22.3193, 114.1694),
}


class EnsembleForecastData(Data):
    """
    Custom data event for Open-Meteo ensemble forecast data.

    Publishes ensemble forecast snapshot for a given city on a target date.
    Implements the abstract ts_event / ts_init properties required by the
    Nautilus Data base class.
    """

    def __init__(
        self,
        city: str,
        latitude: float,
        longitude: float,
        target_date: str,
        member_highs: tuple[float, ...],
        member_lows: tuple[float, ...],
        ensemble_high: float | None,
        ensemble_low: float | None,
        model_name: str,
        source: str,
        temperature_unit: str,
        ts_event: int,
        ts_init: int,
    ) -> None:
        self.city = city
        self.latitude = latitude
        self.longitude = longitude
        self.target_date = target_date
        self.member_highs = member_highs
        self.member_lows = member_lows
        self.ensemble_high = ensemble_high
        self.ensemble_low = ensemble_low
        self.model_name = model_name
        self.source = source
        self.temperature_unit = temperature_unit
        self._ts_event = ts_event
        self._ts_init = ts_init

    @property
    def ts_event(self) -> int:
        """UNIX timestamp (ns) when the forecast was generated."""
        return self._ts_event

    @property
    def ts_init(self) -> int:
        """UNIX timestamp (ns) when this instance was created."""
        return self._ts_init

    @property
    def member_count(self) -> int:
        """Number of ensemble members."""
        return min(len(self.member_highs), len(self.member_lows))


class OpenMeteoEnsembleDataClientConfig(NautilusConfig, frozen=True):
    """Configuration for Open-Meteo ensemble forecast data client."""

    poll_interval_secs: int = 300  # 5 minutes
    base_url: str = "https://ensemble-api.open-meteo.com/v1/ensemble"
    model_name: str = "icon_seamless_eps"
    temperature_unit: str = "celsius"
    timezone: str = "GMT"
    timeout_seconds: float = 15.0
    forecast_days: int = 2


class OpenMeteoEnsembleDataClient(LiveDataClient):
    """
    Live data client for Open-Meteo ensemble forecasts.

    Polls Open-Meteo at configured intervals, fetches ensemble forecasts
    for all configured cities, and publishes EnsembleForecastData events
    to the message bus.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        venue: Venue | None,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        config: OpenMeteoEnsembleDataClientConfig | None = None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=venue,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config or OpenMeteoEnsembleDataClientConfig(),
        )
        self._config = config or OpenMeteoEnsembleDataClientConfig()
        self._poll_task: asyncio.Task | None = None

    async def _connect(self) -> None:
        """Start the polling loop."""
        self._poll_task = self.create_task(
            self._poll_loop(),
            log_msg="Open-Meteo ensemble polling loop",
        )

    async def _disconnect(self) -> None:
        """Stop the polling loop."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def _subscribe(self, command: SubscribeData) -> None:
        """No per-subscription logic; we push continuously to all subscribers."""

    async def _unsubscribe(self, command: UnsubscribeData) -> None:
        """No per-subscription logic."""

    async def _poll_loop(self) -> None:
        """Poll all cities at poll_interval_secs."""
        try:
            while True:
                async with httpx.AsyncClient() as http_client:
                    for city, (latitude, longitude) in CITY_COORDS.items():
                        try:
                            today = date.today()
                            params = {
                                "latitude": f"{latitude:.4f}",
                                "longitude": f"{longitude:.4f}",
                                "models": self._config.model_name,
                                "daily": "temperature_2m_max,temperature_2m_min",
                                "temperature_unit": self._config.temperature_unit,
                                "timezone": self._config.timezone,
                                "forecast_days": str(self._config.forecast_days),
                                "past_days": "0",
                            }

                            response = await http_client.get(
                                self._config.base_url,
                                params=params,
                                timeout=self._config.timeout_seconds,
                                headers={"User-Agent": "NautilusWeatherEnsemble/1.0"},
                            )

                            if response.status_code >= 400:
                                self._log.error(
                                    f"Failed to fetch forecast for {city}: "
                                    f"HTTP {response.status_code}"
                                )
                                continue

                            payload = response.json()
                            snapshot = parse_open_meteo_daily_payload(
                                payload,
                                target_date=today,
                                source="open_meteo_ensemble",
                                model_name=self._config.model_name,
                            )

                            if snapshot is not None:
                                data = EnsembleForecastData(
                                    city=city,
                                    latitude=latitude,
                                    longitude=longitude,
                                    target_date=str(today),
                                    member_highs=snapshot.member_highs,
                                    member_lows=snapshot.member_lows,
                                    ensemble_high=snapshot.ensemble_high,
                                    ensemble_low=snapshot.ensemble_low,
                                    model_name=snapshot.model_name,
                                    source=snapshot.source,
                                    temperature_unit=snapshot.temperature_unit,
                                    ts_event=self._clock.timestamp_ns(),
                                    ts_init=self._clock.timestamp_ns(),
                                )
                                self._handle_data(data)
                                self._log.debug(
                                    f"{city}: high={snapshot.ensemble_high}, "
                                    f"low={snapshot.ensemble_low}, "
                                    f"members={len(snapshot.member_highs)}"
                                )
                        except Exception as e:
                            self._log.error(
                                f"Failed to fetch forecast for {city}: {e!r}"
                            )
                            continue

                # Sleep between full sweeps
                await asyncio.sleep(self._config.poll_interval_secs)

        except asyncio.CancelledError:
            self._log.debug("Polling loop cancelled")
            raise


class OpenMeteoEnsembleDataClientFactory(LiveDataClientFactory):
    """Factory for creating Open-Meteo ensemble forecast data clients."""

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: OpenMeteoEnsembleDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> OpenMeteoEnsembleDataClient:
        """Create a new Open-Meteo ensemble forecast data client."""
        return OpenMeteoEnsembleDataClient(
            loop=loop,
            client_id=ClientId("OPEN_METEO"),
            venue=None,  # Multi-venue
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
