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
Wunderground temperature data client for Nautilus TradingNode.

Polls Weather Underground daily high temperatures for all configured cities
and publishes TemperatureUpdate events to the message bus.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

try:
    from examples.live.polymarket.weather_wunderground_fetcher import (
        CITY_STATIONS,
        fetch_daily_high,
        StationObs,
    )
except ModuleNotFoundError:
    import importlib.util
    module_name = "examples.live.polymarket.weather_wunderground_fetcher"
    module_path = Path(__file__).resolve().with_name("weather_wunderground_fetcher.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    CITY_STATIONS = module.CITY_STATIONS
    fetch_daily_high = module.fetch_daily_high
    StationObs = module.StationObs

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


class TemperatureUpdate(Data):
    """
    Custom data event for daily temperature observations.

    Publishes the running calendar-day high temperature for a given city.
    Implements the abstract ts_event / ts_init properties required by the
    Nautilus Data base class.
    """

    def __init__(
        self,
        city: str,
        station: str,
        daily_max: float,
        unit: str,
        obs_count: int,
        ts_event: int,
        ts_init: int,
    ) -> None:
        self.city = city
        self.station = station
        self.daily_max = daily_max
        self.unit = unit
        self.obs_count = obs_count
        self._ts_event = ts_event
        self._ts_init = ts_init

    @property
    def ts_event(self) -> int:
        """UNIX timestamp (ns) when the observation was taken."""
        return self._ts_event

    @property
    def ts_init(self) -> int:
        """UNIX timestamp (ns) when this instance was created."""
        return self._ts_init


class WundergroundDataClientConfig(NautilusConfig, frozen=True):
    """Configuration for Wunderground data client."""

    poll_interval_secs: int = 900  # 15 minutes
    cities: tuple[str, ...] = ()  # Empty = poll all 50 cities in CITY_STATIONS
    api_key: str = ""  # Empty = use fetcher's default


class WundergroundDataClient(LiveDataClient):
    """
    Live data client for Wunderground temperature observations.

    Polls Weather Underground at configured intervals, fetches running daily
    highs for specified cities, and publishes TemperatureUpdate events to
    the message bus.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        venue: Venue | None,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        config: WundergroundDataClientConfig | None = None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=venue,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config or WundergroundDataClientConfig(),
        )
        self._config = config or WundergroundDataClientConfig()
        self._poll_task: asyncio.Task | None = None

    async def _connect(self) -> None:
        """Start the polling loop."""
        self._poll_task = self.create_task(
            self._poll_loop(),
            log_msg="Wunderground polling loop",
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
        """Poll all configured cities at poll_interval_secs."""
        cities_to_poll = (
            tuple(self._config.cities)
            if self._config.cities
            else tuple(CITY_STATIONS.keys())
        )

        try:
            while True:
                for city in cities_to_poll:
                    try:
                        obs = await fetch_daily_high(
                            city,
                            api_key=self._config.api_key if self._config.api_key else None,
                        )
                        if obs is not None:
                            update = TemperatureUpdate(
                                city=obs.city,
                                station=obs.station,
                                daily_max=obs.daily_max,
                                unit=obs.unit,
                                obs_count=obs.obs_count,
                                ts_event=self._clock.timestamp_ns(),
                                ts_init=self._clock.timestamp_ns(),
                            )
                            self._handle_data(update)
                            self._log.debug(
                                f"{city}: {obs.daily_max}{obs.unit} "
                                f"({obs.obs_count} obs)"
                            )
                    except Exception as e:
                        self._log.error(
                            f"Failed to fetch temperature for {city}: {e!r}"
                        )
                        continue

                # Sleep between full sweeps
                await asyncio.sleep(self._config.poll_interval_secs)

        except asyncio.CancelledError:
            self._log.debug("Polling loop cancelled")
            raise


class WundergroundDataClientFactory(LiveDataClientFactory):
    """Factory for creating Wunderground data clients."""

    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: WundergroundDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> WundergroundDataClient:
        """Create a new Wunderground data client."""
        return WundergroundDataClient(
            loop=loop,
            client_id=ClientId("WEATHER"),
            venue=None,  # Multi-venue
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
