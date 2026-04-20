# Weather Data Adapter — Integration Specification

**Adapter Name**: `WeatherObservationDataClient`
**Framework**: NautilusTrader `LiveDataClient`
**Status**: Specification — no implementation

> **CRITICAL — Resolution Source Alignment**
> Polymarket resolves weather markets from specific data sources per city.
> The adapter **must** pull from the same source Polymarket resolves against,
> or the signal will diverge from the actual settlement outcome.
>
> | Source | Cities |
> |---|---|
> | **Weather Underground** (`wunderground.com`) — airport daily history page | Austin (KAUS), Los Angeles (KLAX), San Francisco (KSFO), Madrid (LEMD), Milan (LIMC), Amsterdam (EHAM), Munich (EDDM), Warsaw (EPWA), London (EGLC), Paris (LFPG) |
> | **NOAA `weather.gov/wrh/timeseries`** | Istanbul only — `?site=LTFM`, "Temp" column daily max |
>
> Do **not** use AWC METAR, OpenWeatherMap, AccuWeather, or any other source.
> Those may read different values than what Polymarket settles against.

---

## Table of Contents

1. [Overview](#1-overview)
2. [API Validation Summary](#2-api-validation-summary)
3. [Recommended Data Sources](#3-recommended-data-sources)
4. [MetarObservation Data Type](#4-metarobservation-data-type)
5. [Field Mapping Table](#5-field-mapping-table)
6. [Smart Polling Schedule](#6-smart-polling-schedule)
7. [Adapter File Structure](#7-adapter-file-structure)
8. [config.py Sketch](#8-configpy-sketch)
9. [data.py Sketch](#9-datapy-sketch)
10. [parsers.py Sketch](#10-parserspy-sketch)
11. [Failure Modes](#11-failure-modes)
12. [Historical Backtest Data](#12-historical-backtest-data)
13. [Blockers](#13-blockers)

---

## 1. Overview

### What This Adapter Does

The METAR adapter polls live aviation weather observations for LTFM (Istanbul Airport) from the Aviation Weather Center (AWC) JSON API and publishes them as `MetarObservation` custom data events onto the NautilusTrader message bus.

Downstream strategies subscribe to `MetarObservation` events and use temperature, wind, and visibility fields as signals for Polymarket weather-contingent prediction market trades (e.g., "Will Istanbul reach 35°C today?").

### Data Flow

```
AWC JSON API (primary)
  https://aviationweather.gov/api/data/metar?ids=LTFM&format=json
          |
          | HTTP GET, ~60s smart poll
          v
  MetarLiveDataClient._poll_loop()
          |
          | parse_awc_json_response()
          v
  MetarObservation (custom Data object)
          |
          | msgbus.publish(topic="data.MetarObservation.LTFM")
          v
  Strategy.on_data(MetarObservation)
          |
          v
  Polymarket trade signal evaluation
```

In degraded mode, when the primary AWC JSON endpoint returns 204 or an empty array, the adapter falls back to the AWC CSV bulk cache, decompresses the gzip stream, and extracts the LTFM row before constructing the same `MetarObservation` object.

### Polymarket Use Case

Polymarket weather markets resolve against official observation records. LTFM is the primary reporting station for Istanbul. The adapter provides the live signal layer; the strategy reads `temp_c` against configured threshold levels to open or close positions before the resolution window closes. Speed matters: METAR observations are published within minutes of the actual observation hour, and Polymarket contracts can resolve within 15 minutes of the observation window.

---

## 2. API Validation Summary

| Source | Endpoint | Auth Required | Response Time | Update Frequency | Viability |
|---|---|---|---|---|---|
| AWC JSON | `https://aviationweather.gov/api/data/metar?ids=LTFM&format=json` | None | ~0.8s | ~hourly (METAR) | **PRIMARY** |
| AWC CSV bulk | `https://aviationweather.gov/data/cache/metars.cache.csv.gz` | None | ~1–3s (gzip download) | ~1 min, max-age=30s | **FALLBACK** |
| NWS api.weather.gov | `https://api.weather.gov/stations/LTFM/observations/latest` | None | N/A | N/A | NOT VIABLE — HTTP 404, US stations only |
| WIFS OPMET | REST endpoint (undisclosed) | API key (registration broken) | 14–78s | No advantage over AWC | SKIP — auth blocked, latency unacceptable |
| NCEI CDO API | `https://www.ncei.noaa.gov/cdo-web/api/v2/data` | Free token (email) | N/A | Historical only | HISTORICAL PROXY via ISD flat files |

**Notes:**

- NWS `api.weather.gov` is a US-domestic API. LTFM is outside its coverage zone. No workaround exists.
- WIFS OPMET registration endpoint was non-functional at time of evaluation. Even if it becomes available, the 14–78s latency makes it unsuitable for the smart polling loop.
- NCEI CDO does not have LTFM in its station inventory (station opened October 2018, never onboarded). The historical path uses NOAA ISD flat files for the Ataturk Airport proxy (LTBA), which require no authentication.

---

## 3. Recommended Data Sources

### Primary: AWC JSON API

```
GET https://aviationweather.gov/api/data/metar?ids=LTFM&format=json
```

**Rationale**: No authentication, sub-second response, JSON format with explicit field names, single-station query eliminates filtering overhead. Cache-Control max-age=90 confirms the server's own update cadence aligns with the polling schedule.

**Preconditions**: None. The endpoint is publicly accessible without registration.

### Fallback: AWC CSV Bulk Cache

```
GET https://aviationweather.gov/data/cache/metars.cache.csv.gz
```

**Rationale**: Updated more frequently than the JSON endpoint (max-age=30s vs 90s), covers all global stations. The trade-off is payload size (~5,000 rows, gzip-compressed). The adapter must decompress in memory and filter to the LTFM row.

**When to use**: AWC JSON returns HTTP 204, an empty array `[]`, or fails to parse cleanly. Also acceptable as a secondary confirmation source during SPECI watch periods.

**Preconditions**: The deployment environment must have the Python `gzip` standard library available (it is in all CPython distributions). No additional dependencies.

### Historical: NOAA ISD Flat Files (LTBA Proxy)

```
GET https://www.ncei.noaa.gov/pub/data/noaa/{year}/170600-99999-{year}.gz
```

**Rationale**: LTFM is not in NOAA's station inventory. The closest proxy is Ataturk Airport (LTBA), 28 km from LTFM, USAF station ID 170600, WBAN 99999. ISD flat files are available without authentication back to 1945 in hourly resolution.

**Preconditions**: No token required for ISD flat files. A CDO API token (free, email-registration at `https://www.ncei.noaa.gov/cdo-web/token`) is only needed if accessing CDO API endpoints, which are not required for this workflow.

---

## 4. MetarObservation Data Type

Place this class in `nautilus_trader/adapters/metar/types.py`.

The canonical reference for `@customdataclass` usage is `nautilus_trader/model/greeks_data.py`. Follow that pattern exactly: inherit from `Data`, apply the decorator, define all fields with `msgspec.Struct`-compatible types, and implement `schema()` and `to_dict()` / `from_dict()`.

```python
# nautilus_trader/adapters/metar/types.py

from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass


@customdataclass
class MetarObservation(Data):
    """
    A single METAR weather observation for a named ICAO station.

    Field units
    -----------
    temp_c          : degrees Celsius (int or float from AWC JSON)
    dewpoint_c      : degrees Celsius (int or float from AWC JSON)
    wind_dir_deg    : degrees true (0–360), integer; None if variable (VRB) or calm
    wind_speed_kt   : knots, integer
    visibility_sm   : statute miles, string (AWC returns "6+" for 6+ SM; parse as float
                      with sentinel value 9999.0 for unlimited)
    altimeter_hpa   : hectopascals (QNH), float
    ts_event        : observation time as nanoseconds UTC (obsTime × 1_000_000_000)
    ts_init         : wall-clock time the MetarObservation object was constructed,
                      nanoseconds UTC (use nautilus_trader.common.clock.LiveClock.timestamp_ns())

    Fields marked Optional are populated when the AWC JSON response includes them
    but may be absent in degraded or special reports.
    """

    # --- Identity ---
    station_id: str                      # ICAO code, e.g. "LTFM"

    # --- Core observations ---
    temp_c: float                        # Air temperature, degrees Celsius
    dewpoint_c: float                    # Dew point temperature, degrees Celsius
    wind_dir_deg: int | None             # Wind direction, degrees true; None = variable/calm
    wind_speed_kt: int                   # Wind speed, knots
    visibility_sm: float                 # Visibility, statute miles; 9999.0 = unlimited
    altimeter_hpa: float                 # Altimeter setting, hPa (QNH)

    # --- Raw report ---
    raw_metar: str                       # Full raw METAR string from AWC `rawOb` field

    # --- Metadata ---
    metar_type: str                      # "METAR" or "SPECI"
    lat: float                           # Station latitude, decimal degrees
    lon: float                           # Station longitude, decimal degrees
    elev_m: int                          # Station elevation, metres

    # --- Nautilus timestamps (required by Data contract) ---
    ts_event: int                        # Observation time, nanoseconds UTC
    ts_init: int                         # Object construction time, nanoseconds UTC
```

**Constraints:**

- `wind_dir_deg` must be `None` when the raw METAR reports `VRB` (variable) or when wind speed is 0 (calm/`00000KT`). Do not store 0 for variable — the ambiguity matters downstream.
- `visibility_sm` must be a finite float. Map AWC's `"6+"` string to `6.0`; map `"P6"` (same meaning, alternate encoding) to `6.0`; store truly unlimited visibility (`9999` or `CAVOK`) as `9999.0`.
- `ts_event` and `ts_init` are `int`, not `float`. NautilusTrader's message bus and backtesting engine assume integer nanoseconds throughout.

---

## 5. Field Mapping Table

| AWC JSON Field | MetarObservation Field | Type | Conversion |
|---|---|---|---|
| `icaoId` | `station_id` | `str` | Direct copy |
| `temp` | `temp_c` | `float` | Cast to float (already Celsius) |
| `dewp` | `dewpoint_c` | `float` | Cast to float (already Celsius) |
| `wdir` | `wind_dir_deg` | `int \| None` | Cast to int; if value is `"VRB"` or wspd == 0, set `None` |
| `wspd` | `wind_speed_kt` | `int` | Cast to int |
| `visib` | `visibility_sm` | `float` | Strip `"+"` or `"P"` prefix; `"6+"` → `6.0`; `"9999"` → `9999.0`; `"CAVOK"` → `9999.0` |
| `altim` | `altimeter_hpa` | `float` | Cast to float (already hPa in JSON) |
| `rawOb` | `raw_metar` | `str` | Direct copy |
| `metarType` | `metar_type` | `str` | Direct copy (`"METAR"` or `"SPECI"`) |
| `lat` | `lat` | `float` | Direct copy |
| `lon` | `lon` | `float` | Direct copy |
| `elev` | `elev_m` | `int` | Cast to int (already metres) |
| `obsTime` | `ts_event` | `int` | `int(obsTime) * 1_000_000_000` (seconds → nanoseconds) |
| _(construction time)_ | `ts_init` | `int` | `clock.timestamp_ns()` at parse time |

**Unit conversion note — obsTime**: AWC returns `obsTime` as a Unix epoch in **seconds** (e.g., `1776608400`). NautilusTrader's entire timestamp system operates in nanoseconds. The conversion is: `ts_event = int(obs_time_seconds) * 1_000_000_000`. Do not use floating-point multiplication; integer arithmetic prevents nanosecond rounding errors.

**Unit conversion note — altimeter**: AWC JSON returns `altim` in hPa directly. No conversion required. If you encounter an inHg value (e.g., from the CSV source, field `altim_in_hg`), convert with: `hpa = round(float(inhg) * 33.8639, 1)`.

---

## 6. Smart Polling Schedule

### Rationale

METAR observations at LTFM are taken at approximately :53–:55 past each hour UTC. The AWC JSON API caches responses for 90 seconds (Cache-Control: max-age=90). Polling at 1-minute intervals during the :52–:02 window (10 minutes straddling the observation time) ensures the new observation is captured within 90 seconds of its publication. Outside this window, a heartbeat poll every 5 minutes serves as a SPECI (unscheduled special report) catch.

### Request Budget

| Window | Duration | Interval | Polls |
|---|---|---|---|
| Smart window (:52 to :02) | 10 minutes | 60s | 10 |
| Heartbeat (outside window) | 50 minutes | 300s | 10 |
| **Total per hour** | | | **20** |

Rate limit headroom: AWC allows up to 100 requests/minute. This schedule uses 20 requests/hour — well under limit.

### Deduplication

Compare `obsTime` (the raw epoch seconds integer from the JSON response) against the last published observation's `obsTime`. If equal, discard the response without constructing a `MetarObservation` or publishing to the bus.

### SPECI Watch Mode

When `temp_c > 38.0` or `temp_c < -5.0`, switch to SPECI watch mode: poll every 3 minutes regardless of window position. Exit SPECI watch mode when the condition no longer holds for two consecutive observations.

### Asyncio Pseudocode

```python
async def _poll_loop(self) -> None:
    last_obs_time: int | None = None

    while self._running:
        now_utc = datetime.utcnow()
        minute = now_utc.minute

        # Determine poll interval
        in_smart_window = (minute >= 52) or (minute <= 2)
        in_speci_watch = self._speci_watch_active

        if in_speci_watch:
            interval_secs = 180          # 3 minutes
        elif in_smart_window:
            interval_secs = 60           # 1 minute
        else:
            interval_secs = 300          # 5 minutes heartbeat

        # --- Poll ---
        raw = await self._fetch_awc_json()          # primary
        if raw is None or len(raw) == 0:
            raw = await self._fetch_awc_csv_row()   # fallback

        if raw is None:
            self._log.warning("Both primary and fallback sources returned no data.")
            await asyncio.sleep(interval_secs)
            continue

        obs = parse_awc_json_response(raw)          # or parse_awc_csv_row(raw)

        # --- Deduplication ---
        obs_time_secs = obs.ts_event // 1_000_000_000
        if obs_time_secs == last_obs_time:
            await asyncio.sleep(interval_secs)
            continue

        last_obs_time = obs_time_secs

        # --- SPECI watch update ---
        self._speci_watch_active = (obs.temp_c > 38.0 or obs.temp_c < -5.0)

        # --- Publish ---
        self._msgbus.publish(
            topic=f"data.MetarObservation.{obs.station_id}",
            msg=obs,
        )

        await asyncio.sleep(interval_secs)
```

**Precondition**: `self._running` must be set to `False` by `_disconnect()` to stop the loop cleanly. Use `asyncio.Task.cancel()` and handle `asyncio.CancelledError` in the loop body for graceful shutdown.

---

## 7. Adapter File Structure

```
nautilus_trader/adapters/metar/
├── __init__.py          # Exports: MetarDataClientConfig, MetarLiveDataClient, MetarObservation
├── config.py            # MetarDataClientConfig dataclass (Pydantic or msgspec)
├── data.py              # MetarLiveDataClient — extends LiveDataClient, owns the poll loop
├── parsers.py           # Stateless parse functions: AWC JSON → MetarObservation, AWC CSV row → MetarObservation
├── types.py             # MetarObservation @customdataclass definition
└── historical.py        # Utilities for fetching NOAA ISD flat files and building ParquetDataCatalog entries
```

**One-line descriptions:**

- `__init__.py` — Single import surface; re-exports the three public symbols so callers use `from nautilus_trader.adapters.metar import ...`
- `config.py` — All tunable parameters for the adapter instance; no logic
- `data.py` — Async poll loop, HTTP session lifecycle, fallback routing, msgbus publication
- `parsers.py` — Pure functions with no side effects; testable in isolation without a running event loop
- `types.py` — Data contract definition; only import here is `nautilus_trader.core.data.Data` and the decorator
- `historical.py` — One-shot CLI-style utilities for backfill; not imported by the live adapter at runtime

---

## 8. config.py Sketch

```python
# nautilus_trader/adapters/metar/config.py

from nautilus_trader.config import LiveDataClientConfig


class MetarDataClientConfig(LiveDataClientConfig, frozen=True):
    """
    Configuration for MetarLiveDataClient.

    Parameters
    ----------
    station_id : str
        ICAO station code to monitor. Default "LTFM".
    base_url_json : str
        AWC JSON API base URL. Override for testing against a mock server.
    base_url_csv : str
        AWC CSV bulk cache URL. Override for testing.
    smart_window_start_minute : int
        Minute-of-hour (0–59) at which the smart poll window begins. Default 52.
    smart_window_end_minute : int
        Minute-of-hour at which the smart window ends (inclusive). Default 2.
        Values less than smart_window_start_minute are interpreted as wrapping
        past the hour boundary (e.g., 52 to 2 = :52, :53, ..., :59, :00, :01, :02).
    smart_poll_interval_secs : int
        Poll interval in seconds during the smart window. Default 60.
    heartbeat_interval_secs : int
        Poll interval in seconds outside the smart window. Default 300.
    speci_poll_interval_secs : int
        Poll interval in seconds when SPECI watch mode is active. Default 180.
    speci_temp_high_c : float
        Temperature threshold above which SPECI watch activates. Default 38.0.
    speci_temp_low_c : float
        Temperature threshold below which SPECI watch activates. Default -5.0.
    http_timeout_secs : float
        Timeout for each HTTP request in seconds. Default 10.0.
    max_retries : int
        Maximum retry attempts before falling back to CSV source. Default 2.
    """

    station_id: str = "LTFM"
    base_url_json: str = "https://aviationweather.gov/api/data/metar"
    base_url_csv: str = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"
    smart_window_start_minute: int = 52
    smart_window_end_minute: int = 2
    smart_poll_interval_secs: int = 60
    heartbeat_interval_secs: int = 300
    speci_poll_interval_secs: int = 180
    speci_temp_high_c: float = 38.0
    speci_temp_low_c: float = -5.0
    http_timeout_secs: float = 10.0
    max_retries: int = 2
```

**Note**: `LiveDataClientConfig` uses `msgspec.Struct` with `frozen=True`. Do not subclass `pydantic.BaseModel` or use `dataclasses.dataclass` — the NautilusTrader config system expects the msgspec inheritance chain.

---

## 9. data.py Sketch

```python
# nautilus_trader/adapters/metar/data.py

import asyncio
import gzip
import io
from datetime import datetime, timezone

import aiohttp

from nautilus_trader.common.component import LiveClock, Logger
from nautilus_trader.live.data_client import LiveDataClient
from nautilus_trader.model.data import DataType
from nautilus_trader.msgbus.bus import MessageBus

from .config import MetarDataClientConfig
from .parsers import parse_awc_json_response, parse_awc_csv_row
from .types import MetarObservation


class MetarLiveDataClient(LiveDataClient):
    """
    Polls METAR observations from AWC and publishes MetarObservation events.

    Primary source: AWC JSON API
    Fallback source: AWC CSV bulk cache (gzip)
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: str,
        msgbus: MessageBus,
        cache,                           # nautilus_trader.cache.Cache
        clock: LiveClock,
        logger: Logger,
        config: MetarDataClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=None,                  # Weather data has no venue
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
            config=config,
        )
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task | None = None
        self._running: bool = False
        self._speci_watch_active: bool = False
        self._last_obs_time_secs: int | None = None

    async def _connect(self) -> None:
        """Open the HTTP session and start the poll loop."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._config.http_timeout_secs)
        )
        self._running = True
        self._poll_task = self._loop.create_task(self._poll_loop())
        self._log.info(f"MetarLiveDataClient connected for station {self._config.station_id}")

    async def _disconnect(self) -> None:
        """Cancel the poll loop and close the HTTP session."""
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._session is not None:
            await self._session.close()
        self._log.info("MetarLiveDataClient disconnected")

    async def _poll_loop(self) -> None:
        """
        Core polling loop.

        Determines the correct interval for the current time, fetches from
        primary or fallback source, deduplicates on obsTime, and publishes
        MetarObservation to the message bus.
        """
        while self._running:
            try:
                interval_secs = self._current_interval()
                raw = await self._fetch_awc_json()
                if raw is None:
                    raw = await self._fetch_awc_csv_row()
                if raw is None:
                    self._log.warning("All sources exhausted, skipping poll cycle")
                    await asyncio.sleep(interval_secs)
                    continue

                obs = self._parse_and_publish(raw)
                if obs is not None:
                    self._speci_watch_active = (
                        obs.temp_c > self._config.speci_temp_high_c
                        or obs.temp_c < self._config.speci_temp_low_c
                    )

                await asyncio.sleep(interval_secs)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error(f"Unhandled error in poll loop: {exc}")
                await asyncio.sleep(self._config.heartbeat_interval_secs)

    def _current_interval(self) -> int:
        """Return the appropriate sleep interval based on time and SPECI state."""
        if self._speci_watch_active:
            return self._config.speci_poll_interval_secs
        minute = datetime.now(tz=timezone.utc).minute
        start = self._config.smart_window_start_minute  # 52
        end = self._config.smart_window_end_minute       # 2
        # Window wraps around the hour boundary
        in_window = (minute >= start) or (minute <= end)
        return (
            self._config.smart_poll_interval_secs
            if in_window
            else self._config.heartbeat_interval_secs
        )

    async def _fetch_awc_json(self) -> list[dict] | None:
        """
        Fetch from AWC JSON endpoint.

        Returns the parsed JSON array on success, None on any failure.
        Retries up to config.max_retries times before returning None.
        """
        url = f"{self._config.base_url_json}?ids={self._config.station_id}&format=json"
        for attempt in range(self._config.max_retries + 1):
            try:
                async with self._session.get(url) as resp:
                    if resp.status == 204:
                        return None
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    if isinstance(data, list) and len(data) > 0:
                        return data
                    return None
            except Exception as exc:
                self._log.warning(f"AWC JSON fetch attempt {attempt + 1} failed: {exc}")
        return None

    async def _fetch_awc_csv_row(self) -> dict | None:
        """
        Fetch the AWC CSV bulk cache, decompress in memory, and extract the
        LTFM row.

        Returns a dict with CSV field names on success, None on failure.
        """
        try:
            async with self._session.get(self._config.base_url_csv) as resp:
                resp.raise_for_status()
                compressed = await resp.read()
            with gzip.open(io.BytesIO(compressed), "rt") as f:
                return parse_awc_csv_row(f, self._config.station_id)
        except Exception as exc:
            self._log.error(f"AWC CSV fallback failed: {exc}")
            return None

    def _parse_and_publish(self, raw) -> MetarObservation | None:
        """
        Parse raw data into MetarObservation, deduplicate, and publish.

        raw can be either:
        - list[dict] from the JSON endpoint
        - dict from the CSV row parser
        """
        try:
            if isinstance(raw, list):
                obs = parse_awc_json_response(raw, ts_init=self._clock.timestamp_ns())
            else:
                obs = parse_awc_csv_row_to_observation(raw, ts_init=self._clock.timestamp_ns())
        except Exception as exc:
            self._log.error(f"Parse error: {exc}")
            return None

        obs_time_secs = obs.ts_event // 1_000_000_000
        if obs_time_secs == self._last_obs_time_secs:
            return None  # duplicate; already published

        self._last_obs_time_secs = obs_time_secs
        self._msgbus.publish(
            topic=f"data.MetarObservation.{obs.station_id}",
            msg=obs,
        )
        self._log.debug(f"Published MetarObservation: {obs.raw_metar}")
        return obs

    def subscribe_data(self, data_type: DataType, **kwargs) -> None:
        """
        Register a subscription for MetarObservation data.

        NautilusTrader calls this when a strategy issues subscribe_data(DataType(MetarObservation)).
        For a polling adapter there is no server-side subscription to initiate; the
        poll loop runs continuously after _connect(). This method is a no-op but
        must be implemented to satisfy the LiveDataClient interface.
        """
        self._log.info(f"subscribe_data called for {data_type} — polling already active")

    def unsubscribe_data(self, data_type: DataType, **kwargs) -> None:
        """No-op. Polling continues until disconnect."""
        pass
```

---

## 10. parsers.py Sketch

```python
# nautilus_trader/adapters/metar/parsers.py

import csv
import io
from typing import TextIO


from .types import MetarObservation


def parse_awc_json_response(
    data: list[dict],
    ts_init: int,
) -> MetarObservation:
    """
    Parse the AWC JSON API response array into a MetarObservation.

    Parameters
    ----------
    data : list[dict]
        Raw parsed JSON from AWC. Expected to contain exactly one element for
        a single-station query. Element 0 is used.
    ts_init : int
        Nanosecond UTC timestamp at which the object is being constructed.
        Caller must supply this from LiveClock.timestamp_ns().

    Returns
    -------
    MetarObservation

    Raises
    ------
    ValueError
        If data is empty or a required field is missing.
    """
    if not data:
        raise ValueError("AWC JSON response array is empty")

    rec = data[0]

    # --- Timestamps ---
    obs_time_secs: int = int(rec["obsTime"])
    ts_event: int = obs_time_secs * 1_000_000_000

    # --- Wind direction: handle variable ("VRB") and calm (speed == 0) ---
    raw_wdir = rec.get("wdir")
    wspd = int(rec.get("wspd", 0))
    if raw_wdir is None or str(raw_wdir).upper() == "VRB" or wspd == 0:
        wind_dir_deg = None
    else:
        wind_dir_deg = int(raw_wdir)

    # --- Visibility: strip trailing "+" or leading "P", map CAVOK ---
    raw_vis = str(rec.get("visib", "9999"))
    raw_vis = raw_vis.strip().upper()
    if raw_vis in ("CAVOK", "9999"):
        visibility_sm = 9999.0
    else:
        raw_vis = raw_vis.lstrip("P").rstrip("+")
        visibility_sm = float(raw_vis)

    return MetarObservation(
        station_id=str(rec["icaoId"]),
        temp_c=float(rec["temp"]),
        dewpoint_c=float(rec["dewp"]),
        wind_dir_deg=wind_dir_deg,
        wind_speed_kt=wspd,
        visibility_sm=visibility_sm,
        altimeter_hpa=float(rec["altim"]),
        raw_metar=str(rec["rawOb"]),
        metar_type=str(rec.get("metarType", "METAR")),
        lat=float(rec["lat"]),
        lon=float(rec["lon"]),
        elev_m=int(rec["elev"]),
        ts_event=ts_event,
        ts_init=ts_init,
    )


def parse_awc_csv_row(
    fileobj: TextIO,
    station_id: str,
    ts_init: int,
) -> MetarObservation | None:
    """
    Scan an open CSV text stream (the decompressed AWC bulk cache) and return
    a MetarObservation for station_id, or None if the station is not found.

    CSV field names (from AWC header row):
      station_id, observation_time, temp_c, dewpoint_c, wind_dir_degrees,
      wind_speed_kt, visibility_statute_mi, altim_in_hg, raw_text, ...

    Parameters
    ----------
    fileobj : TextIO
        Open text stream of the decompressed metars.cache.csv content.
        The caller (data.py) is responsible for decompression.
    station_id : str
        ICAO station code to find (e.g. "LTFM").
    ts_init : int
        Nanoseconds UTC construction timestamp from LiveClock.

    Returns
    -------
    MetarObservation or None
    """
    # The AWC CSV has comment lines starting with '#' before the header.
    # Skip them, then feed the remainder to csv.DictReader.
    lines = (line for line in fileobj if not line.startswith("#"))
    reader = csv.DictReader(lines)

    for row in reader:
        if row.get("station_id", "").strip() != station_id:
            continue

        # observation_time is ISO-8601 e.g. "2026-04-19T14:20:00Z"
        from datetime import datetime, timezone
        obs_dt = datetime.fromisoformat(
            row["observation_time"].replace("Z", "+00:00")
        )
        ts_event = int(obs_dt.timestamp()) * 1_000_000_000

        # altim_in_hg → hPa
        altim_hpa = round(float(row["altim_in_hg"]) * 33.8639, 1)

        raw_wdir = row.get("wind_dir_degrees", "").strip()
        wspd = int(float(row.get("wind_speed_kt", 0) or 0))
        if not raw_wdir or raw_wdir.upper() == "VRB" or wspd == 0:
            wind_dir_deg = None
        else:
            wind_dir_deg = int(float(raw_wdir))

        raw_vis = str(row.get("visibility_statute_mi", "9999")).strip().upper()
        if raw_vis in ("CAVOK", "9999", ""):
            visibility_sm = 9999.0
        else:
            raw_vis = raw_vis.lstrip("P").rstrip("+")
            visibility_sm = float(raw_vis)

        # CSV does not have separate lat/lon/elev; use known LTFM constants
        # as fallback. A complete implementation should maintain a station
        # metadata table.
        LTFM_LAT, LTFM_LON, LTFM_ELEV = 41.262, 28.74, 99

        return MetarObservation(
            station_id=station_id,
            temp_c=float(row["temp_c"]),
            dewpoint_c=float(row["dewpoint_c"]),
            wind_dir_deg=wind_dir_deg,
            wind_speed_kt=wspd,
            visibility_sm=visibility_sm,
            altimeter_hpa=altim_hpa,
            raw_metar=str(row.get("raw_text", "")),
            metar_type="METAR",           # CSV does not distinguish SPECI
            lat=LTFM_LAT,
            lon=LTFM_LON,
            elev_m=LTFM_ELEV,
            ts_event=ts_event,
            ts_init=ts_init,
        )

    return None  # station not found in CSV
```

---

## 11. Failure Modes

### AWC JSON Returns HTTP 204 (No Content)

**Condition**: API returns 204, meaning no METAR data is available for the query station at this moment.
**Action**: Immediately fall through to the CSV fallback path. Do not log as an error; log at WARNING level.
**Risk**: If this occurs during the smart poll window, the CSV fallback has a shorter max-age (30s) and may have a fresher reading.

### AWC JSON Returns Empty Array `[]`

**Condition**: HTTP 200 with body `[]`. Happens when AWC has no recent observation indexed for LTFM (station temporarily offline, maintenance, etc.).
**Action**: Same as 204 — fall through to CSV fallback.
**Risk**: If both primary and CSV return no LTFM data, the adapter emits a WARNING and skips the cycle. The strategy must handle stale signals; the spec for that is outside this document.

### obsTime Is Stale Past :02

**Condition**: After :02 past the hour, the obsTime in the response still matches the *previous* hour's observation. This means AWC has not yet indexed the new METAR.
**Action**: The deduplication check silently skips publication. No action required. The heartbeat poll at :05, :10, :15 (etc.) will eventually pick up the new observation when AWC catches up.
**Do not**: raise an alert purely because the observation is from the previous hour. METAR issuance delays of 5–10 minutes past the nominal observation time are normal.

### JSON Parse Error

**Condition**: `aiohttp` or `json.loads` raises an exception because the response body is not valid JSON (e.g., AWC is serving an HTML error page during a deployment).
**Action**: Log at ERROR level with the raw response text (first 500 characters). Fall through to CSV fallback. If CSV also fails, skip the cycle.
**Backoff policy**: After 3 consecutive parse failures on the JSON endpoint, increase the primary poll interval to 5× the configured value until a successful parse is received. Reset on success. This prevents hammering a broken endpoint.

### HTTP Timeout or Connection Error

**Condition**: `aiohttp.ClientTimeout` or `aiohttp.ClientConnectionError` during fetch.
**Action**: Retry up to `config.max_retries` times with no additional delay (the retries are fast). If all retries fail, fall through to CSV fallback.
**Backoff policy**: Same consecutive-failure escalation as JSON parse errors.

### CSV Fallback: LTFM Row Missing

**Condition**: The AWC bulk CSV cache does not contain a row for `station_id == "LTFM"`. This should not happen under normal conditions; LTFM is an ICAO reporting station.
**Action**: Log at ERROR level. Skip the cycle. Do not construct a synthetic or estimated observation.

### CSV Fallback: Decompression Failure

**Condition**: The `.gz` payload is truncated or corrupt (possible if the request caught AWC mid-write).
**Action**: Log at WARNING. Skip the cycle. Retry on the next scheduled poll.

### Adapter Restart / Process Crash

**Condition**: The NautilusTrader node restarts.
**Action**: On `_connect()`, `self._last_obs_time_secs` is reset to `None`. The first successful poll will publish the most recent observation regardless of whether it was published before the crash. The downstream strategy must handle duplicate events gracefully (idempotent signal ingestion).

---

## 12. Historical Backtest Data

### Station Selection

LTFM (opened October 2018) is not in the NOAA ISD station inventory and cannot be queried from NCEI. Use **LTBA (Ataturk Airport)** as the historical proxy.

| Field | Value |
|---|---|
| ICAO | LTBA |
| NOAA USAF station ID | 170600 |
| NOAA WBAN | 99999 |
| Distance from LTFM | ~28 km |
| ISD record start | 1945 |
| Resolution | Hourly |

### Fetching ISD Flat Files

No authentication is required. Files are available at:

```
https://www.ncei.noaa.gov/pub/data/noaa/{year}/170600-99999-{year}.gz
```

Example for 2024:
```bash
curl -O https://www.ncei.noaa.gov/pub/data/noaa/2024/170600-99999-2024.gz
```

ISD flat files use a fixed-width format. Use the `isd-lite` simplified format for easier parsing:

```
https://www.ncei.noaa.gov/pub/data/noaa/isd-lite/{year}/170600-99999-{year}.gz
```

ISD-Lite fields (space-delimited): `year month day hour air_temp dew_point sea_level_pressure wind_direction wind_speed sky_condition precip_1h precip_6h`

- `air_temp` and `dew_point` are in tenths of degrees Celsius. Divide by 10 for `temp_c`.
- `wind_direction` is degrees; value 999 = variable/missing → map to `None`.
- `wind_speed` is tenths of m/s. Convert to knots: `kt = round(value_tenths_ms / 10 * 1.94384, 1)`.
- `sea_level_pressure` is tenths of hPa. Divide by 10 for `altimeter_hpa`.

### Converting to MetarObservation

Implement `historical.py` with a function:

```python
def isd_lite_row_to_metar_observation(
    row: dict,                  # parsed ISD-Lite row as a dict
    station_id: str = "LTFM",  # override station identity from LTBA to LTFM
) -> MetarObservation:
    ...
```

This function relabels the station_id from "LTBA" to "LTFM" when building the historical catalog. This is intentional: the strategy logic references "LTFM" throughout, and the proxy assumption (LTBA ≈ LTFM) is acknowledged in this spec. Add a comment in the code at the point of override.

`raw_metar` will be an empty string `""` for historical ISD-Lite records — there is no raw METAR string in ISD flat files.

### Populating ParquetDataCatalog

```python
from nautilus_trader.persistence.catalog import ParquetDataCatalog

catalog = ParquetDataCatalog("path/to/catalog")

observations: list[MetarObservation] = []
# ... build list from ISD-Lite rows via isd_lite_row_to_metar_observation()

catalog.write_data(observations)
```

NautilusTrader's `ParquetDataCatalog.write_data()` accepts a list of `Data` objects. It partitions them by type and timestamp automatically. Ensure `ts_event` values are strictly monotonically increasing within the list (sort by `ts_event` before calling `write_data()`).

### Data Availability

ISD-Lite hourly records exist for LTBA from 1990 onward with good coverage. For the Polymarket use case (summer 2026 temperature markets), recommend backtest window: 2010–2025 (15 years, same calendar months, using June–August for summer markets).

---

## 13. Blockers

The following items must be resolved before implementation begins. None are blockers in the sense of requiring external approval, but each requires a concrete decision.

### B1: NautilusTrader `@customdataclass` Exact Signature

**Status**: Canonical reference is `nautilus_trader/model/greeks_data.py`. The engineer must read that file before writing `types.py` to confirm the exact decorator import path and whether `schema()`, `to_dict()`, and `from_dict()` must be explicitly implemented or are generated by the decorator.

**Resolution**: Read `greeks_data.py` and the decorator source in `nautilus_trader/model/custom.py` before writing any code in `types.py`.

### B2: `LiveDataClient` Constructor Signature

**Status**: The constructor sketch in `data.py` above uses the signature inferred from the template adapter. The actual signature may differ between NautilusTrader versions.

**Resolution**: Read `nautilus_trader/live/data_client.py` `__init__` to confirm parameter names and types before wiring the constructor.

### B3: `venue=None` Acceptability

**Status**: Weather data has no financial venue. The sketch passes `venue=None` to `LiveDataClient.__init__`. If the base class requires a non-None `Venue` object, a synthetic venue (e.g., `Venue("WEATHER")`) must be created.

**Resolution**: Check base class source for `venue` parameter validation.

### B4: `msgbus.publish` Topic Naming Convention

**Status**: The sketch uses `"data.MetarObservation.LTFM"` as the topic string. NautilusTrader may have a required topic naming convention for custom data types.

**Resolution**: Search codebase for existing `msgbus.publish` calls in adapters to confirm the topic format used for custom data types.

### B5: aiohttp as Dependency

**Status**: The sketch uses `aiohttp`. NautilusTrader's built-in adapters use `aiohttp` for async HTTP, but if this adapter is maintained outside the main repo, `aiohttp` must be listed as an explicit dependency in `pyproject.toml`.

**Resolution**: Confirm `aiohttp` is already a declared dependency, or add it.

### B6: LTFM Hardcoded Coordinates in CSV Fallback

**Status**: The CSV fallback in `parsers.py` hardcodes LTFM coordinates (`lat=41.262, lon=28.74, elev_m=99`) because the CSV format does not include per-station location. This is a known limitation.

**Resolution**: Accept the hardcoded values for now. If multi-station support is added later, implement a station metadata table keyed by ICAO code.

### B7: ISD-Lite Missing Data Sentinel Values

**Status**: ISD-Lite uses `-9999` as the sentinel for missing measurements. The `isd_lite_row_to_metar_observation()` function must handle these gracefully. The spec does not define what the function should do when `air_temp == -9999` (raise? skip? substitute NaN?).

**Resolution**: Define the missing-data policy before implementing `historical.py`. Recommendation: raise `ValueError` and let the caller skip the row.

---

*Specification version: 1.0 — 2026-04-19*
*Station validated: LTFM (Istanbul Airport)*
*NautilusTrader target: LiveDataClient adapter pattern*
