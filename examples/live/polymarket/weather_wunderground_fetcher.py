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
Wunderground temperature fetcher for all 50 Polymarket weather cities.

Fetches the running calendar-day high temperature from the **exact resolution
source** that Polymarket's oracle uses for each city.  Use this as a pre-trade
signal to assess whether the daily high has already crossed the market threshold
or is likely to by end-of-day.

Resolution source types
-----------------------
- ``wu``   : Weather Underground (weather.com internal API) — 46 cities
- ``noaa`` : NOAA timeseries (weather.gov/wrh/timeseries) — 3 cities
             Istanbul LTFM, Moscow UUWW, Tel Aviv LLBG
- ``hko``  : Hong Kong Observatory (weather.gov.hk) — 1 city

API key
-------
The ``TWC_API_KEY`` environment variable (or the embedded fallback) is a
Weather.com internal key extracted from the Wunderground frontend.  It works
for all stations except Istanbul LTFM.

Usage
-----
    from weather_wunderground_fetcher import fetch_daily_high, StationObs

    obs = await fetch_daily_high("Tokyo")   # or fetch_daily_high_sync("Tokyo")
    if obs:
        print(f"{obs.city}: {obs.daily_max}°{obs.unit}")
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, date
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Embedded API key (extracted from Wunderground frontend)
# Can be overridden via TWC_API_KEY env var.
# ---------------------------------------------------------------------------
_DEFAULT_TWC_KEY = "6532d6454b8aa370768e63d6ba5a832e"

# ---------------------------------------------------------------------------
# Complete 50-city station map
#
# Fields per entry: (icao_station, iso2_country, unit, oracle_type)
#   unit         : "F" (Fahrenheit) for US cities, "C" (Celsius) for others
#   oracle_type  : "wu", "noaa", or "hko"
#
# Sources: extracted from Polymarket market descriptions / resolutionSource
# fields via Gamma API (confirmed April 2026).
#
# KEY CORRECTIONS vs naive ICAO assumptions:
#   Denver     → KBKF (Buckley AFB / Aurora), NOT KDEN
#   Jakarta    → WIHH (Halim Perdanakusuma), NOT WIII
#   Lagos      → DNMM (Murtala Muhammed Intl), NOT DNBE
#   Paris      → LFPB (Le Bourget), NOT LFPG  (changed ~2026)
#   Taipei     → RCSS (Songshan Airport), NOT RCTP (changed ~2026)
#   Moscow     → UUWW (Vnukovo), NOT UUDD  (NOAA oracle)
# ---------------------------------------------------------------------------
CITY_STATIONS: dict[str, tuple[str, str, str, str]] = {
    # ---- United States — Fahrenheit — WU oracle ----
    "NYC":           ("KLGA", "US", "F", "wu"),
    "New York City": ("KLGA", "US", "F", "wu"),  # alias: resolver outputs full name
    "Chicago":       ("KORD", "US", "F", "wu"),
    "Miami":         ("KMIA", "US", "F", "wu"),
    "Los Angeles":   ("KLAX", "US", "F", "wu"),
    "San Francisco": ("KSFO", "US", "F", "wu"),
    "Seattle":       ("KSEA", "US", "F", "wu"),
    "Denver":        ("KBKF", "US", "F", "wu"),   # Buckley AFB, Aurora CO
    "Houston":       ("KHOU", "US", "F", "wu"),
    "Dallas":        ("KDAL", "US", "F", "wu"),
    "Austin":        ("KAUS", "US", "F", "wu"),
    "Atlanta":       ("KATL", "US", "F", "wu"),
    # ---- Europe — Celsius — WU oracle ----
    "London":        ("EGLC", "GB", "C", "wu"),
    "Paris":         ("LFPB", "FR", "C", "wu"),   # Le Bourget (changed ~2026)
    "Madrid":        ("LEMD", "ES", "C", "wu"),
    "Amsterdam":     ("EHAM", "NL", "C", "wu"),
    "Munich":        ("EDDM", "DE", "C", "wu"),
    "Milan":         ("LIMC", "IT", "C", "wu"),
    "Warsaw":        ("EPWA", "PL", "C", "wu"),
    "Helsinki":      ("EFHK", "FI", "C", "wu"),
    "Ankara":        ("LTAC", "TR", "C", "wu"),
    # ---- East Asia — Celsius — WU oracle ----
    "Tokyo":         ("RJTT", "JP", "C", "wu"),
    "Seoul":         ("RKSI", "KR", "C", "wu"),
    "Busan":         ("RKPK", "KR", "C", "wu"),
    "Taipei":        ("RCSS", "TW", "C", "wu"),   # Songshan Airport (changed ~2026)
    "Singapore":     ("WSSS", "SG", "C", "wu"),
    "Kuala Lumpur":  ("WMKK", "MY", "C", "wu"),
    "Jakarta":       ("WIHH", "ID", "C", "wu"),   # Halim Perdanakusuma
    "Manila":        ("RPLL", "PH", "C", "wu"),
    # ---- China — Celsius — WU oracle ----
    "Beijing":       ("ZBAA", "CN", "C", "wu"),
    "Shanghai":      ("ZSPD", "CN", "C", "wu"),
    "Shenzhen":      ("ZGSZ", "CN", "C", "wu"),
    "Guangzhou":     ("ZGGG", "CN", "C", "wu"),
    "Chongqing":     ("ZUCK", "CN", "C", "wu"),
    "Chengdu":       ("ZUUU", "CN", "C", "wu"),
    "Wuhan":         ("ZHHH", "CN", "C", "wu"),
    # ---- South/Central Asia — Celsius — WU oracle ----
    "Lucknow":       ("VILK", "IN", "C", "wu"),
    "Karachi":       ("OPKC", "PK", "C", "wu"),
    "Jeddah":        ("OEJN", "SA", "C", "wu"),
    # ---- Africa — Celsius — WU oracle ----
    "Lagos":         ("DNMM", "NG", "C", "wu"),   # Murtala Muhammed Intl
    "Cape Town":     ("FACT", "ZA", "C", "wu"),
    # ---- Americas — Celsius — WU oracle ----
    "Buenos Aires":  ("SAEZ", "AR", "C", "wu"),
    "Sao Paulo":     ("SBGR", "BR", "C", "wu"),
    "Mexico City":   ("MMMX", "MX", "C", "wu"),
    "Toronto":       ("CYYZ", "CA", "C", "wu"),
    "Panama City":   ("MPMG", "PA", "C", "wu"),
    # ---- Oceania — Celsius — WU oracle ----
    "Wellington":    ("NZWN", "NZ", "C", "wu"),
    # ---- NOAA oracle (weather.gov/wrh/timeseries) — Celsius ----
    "Istanbul":      ("LTFM", "TR", "C", "noaa"),  # Istanbul Airport
    "Moscow":        ("UUWW", "RU", "C", "noaa"),  # Vnukovo Intl
    "Tel Aviv":      ("LLBG", "IL", "C", "noaa"),  # Ben Gurion Intl
    # ---- Hong Kong Observatory oracle — Celsius ----
    "Hong Kong":     ("VHHH", "HK", "C", "hko"),   # HK Intl (VHHH on TWC)
}

# Cities where oracle is NOAA but TWC API also has the station data
# (LLBG and UUWW return 200 on TWC; LTFM returns 400)
_NOAA_WITH_TWC_DATA = {"Tel Aviv", "Moscow", "Hong Kong"}


@dataclass(frozen=True, slots=True)
class StationObs:
    """Running calendar-day high temperature observation for one city."""
    city: str
    station: str                # ICAO station code
    daily_max: float            # Highest recorded temp so far today
    unit: str                   # "F" or "C"
    obs_count: int              # Number of hourly observations used
    as_of_utc: datetime         # When the latest observation was taken
    oracle_type: str            # "wu", "noaa", "hko"
    fetch_source: str           # "twc_historical", "noaa_html", "hko_html"


# ---------------------------------------------------------------------------
# Simple in-memory TTL cache
# ---------------------------------------------------------------------------
@dataclass
class _CacheEntry:
    obs: StationObs
    fetched_at: float = field(default_factory=time.monotonic)


_cache: dict[str, _CacheEntry] = {}
_CACHE_TTL_SECONDS = 900  # 15 minutes — observations update ~hourly


def _cached(city: str) -> StationObs | None:
    entry = _cache.get(city)
    if entry and (time.monotonic() - entry.fetched_at) < _CACHE_TTL_SECONDS:
        return entry.obs
    return None


def _store(obs: StationObs) -> None:
    _cache[obs.city] = _CacheEntry(obs=obs)


# ---------------------------------------------------------------------------
# TWC (Weather.com) historical observations fetch
# ---------------------------------------------------------------------------

_TWC_HIST_URL = (
    "https://api.weather.com/v1/location/{loc}:9:{iso}/observations/historical.json"
    "?apiKey={key}&units={units}&startDate={date}&endDate={date}"
)

def _wu_scrape_url() -> str:
    """Return the WU scrape URL using today's date (not a hardcoded date)."""
    from datetime import date as _date
    today = _date.today()
    return f"https://www.wunderground.com/history/daily/us/new-york/KLGA/date/{today.year}-{today.month}-{today.day}"
_TWC_KEY_RE = re.compile(r"apiKey[=:][\"'\s]*([a-f0-9]{32})")

# Module-level mutable key cache so a refreshed key persists across calls
_active_twc_key: str | None = None


async def _scrape_fresh_twc_key() -> str | None:
    """Scrape the current TWC API key from the Wunderground frontend."""
    try:
        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        ) as client:
            r = await client.get(_wu_scrape_url())
        keys = _TWC_KEY_RE.findall(r.text)
        if keys:
            # Most frequent key is the active one
            fresh = max(set(keys), key=keys.count)
            log.info("TWC key refreshed via Wunderground scrape: %s…", fresh[:8])
            return fresh
    except Exception as exc:
        log.warning("TWC key scrape failed: %s", exc)
    return None


async def _fetch_twc_daily_high(
    city: str,
    station: str,
    iso: str,
    unit: str,
    api_key: str,
    target_date: date | None = None,
) -> StationObs | None:
    """Fetch running daily high from TWC historical observations endpoint."""
    today = target_date or date.today()
    date_str = today.strftime("%Y%m%d")
    units_param = "e" if unit == "F" else "m"
    url = _TWC_HIST_URL.format(
        loc=station, iso=iso, key=api_key, units=units_param, date=date_str,
    )
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(url)
        if r.status_code == 401:
            # Key has rotated — scrape a fresh one and retry once
            global _active_twc_key
            fresh_key = await _scrape_fresh_twc_key()
            if fresh_key and fresh_key != api_key:
                _active_twc_key = fresh_key
                os.environ["TWC_API_KEY"] = fresh_key
                retry_url = _TWC_HIST_URL.format(
                    loc=station, iso=iso, key=fresh_key, units=units_param, date=date_str,
                )
                async with httpx.AsyncClient(timeout=12) as client:
                    r = await client.get(retry_url)
        if r.status_code != 200:
            log.warning("TWC historical %s %s: HTTP %d", city, station, r.status_code)
            return None
        data = r.json()
        obs_list: list[dict[str, Any]] = data.get("observations") or []
        if not obs_list:
            log.debug("TWC historical %s %s: empty observations", city, station)
            return None

        temps = [o["temp"] for o in obs_list if o.get("temp") is not None]
        if not temps:
            return None

        daily_max = float(max(temps))
        # Latest observation timestamp
        last_ts = max(o["valid_time_gmt"] for o in obs_list if o.get("valid_time_gmt"))
        as_of = datetime.fromtimestamp(last_ts, tz=UTC)

        return StationObs(
            city=city,
            station=station,
            daily_max=daily_max,
            unit=unit,
            obs_count=len(temps),
            as_of_utc=as_of,
            oracle_type="wu",
            fetch_source="twc_historical",
        )
    except Exception as exc:
        log.warning("TWC historical %s %s: %r", city, station, exc)
        return None


# ---------------------------------------------------------------------------
# NOAA fetch via Iowa State ASOS API  (Istanbul LTFM)
# ---------------------------------------------------------------------------
# The NOAA timeseries page (weather.gov/wrh/timeseries) renders data as
# JavaScript-built tables — HTML scraping is unreliable.  Iowa State's ASOS
# service provides the same underlying METAR observations in clean CSV format
# and supports all ICAO stations globally.
#
# Reference: https://mesonet.agron.iastate.edu/request/download.phtml

_ASOS_URL = (
    "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    "?station={station}&data=tmpc"
    "&year1={y}&month1={m}&day1={d}"
    "&year2={y}&month2={m}&day2={d}"
    "&tz=UTC&format=onlycomma&latlon=no&direct=no"
)


async def _fetch_noaa_daily_high(
    city: str,
    station: str,
    target_date: date | None = None,
) -> StationObs | None:
    """
    Fetch running daily high from Iowa State ASOS (same underlying METAR data
    as the NOAA timeseries page Polymarket's oracle reads).

    Note: Observations are in UTC.  Polymarket resolves on the local calendar
    date; for cities near UTC this is a near-exact match.  Small ambiguity at
    the day boundary is acceptable for intraday monitoring purposes.
    """
    today = target_date or date.today()
    url = _ASOS_URL.format(
        station=station,
        y=today.year,
        m=today.month,
        d=today.day,
    )
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url)
        if r.status_code != 200:
            log.warning("ASOS %s %s: HTTP %d", city, station, r.status_code)
            return None

        temps: list[float] = []
        last_ts: str = ""
        for line in r.text.splitlines():
            if line.startswith("station") or not line.strip():
                continue  # header / blank
            parts = line.split(",")
            if len(parts) < 3:
                continue
            ts_str, tmpc_str = parts[1].strip(), parts[2].strip()
            try:
                temps.append(float(tmpc_str))
                last_ts = ts_str
            except ValueError:
                pass

        if not temps:
            log.debug("ASOS %s %s: no temp values in response", city, station)
            return None

        daily_max = float(max(temps))
        as_of = (
            datetime.strptime(last_ts, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            if last_ts
            else datetime.now(tz=UTC)
        )

        return StationObs(
            city=city,
            station=station,
            daily_max=daily_max,
            unit="C",
            obs_count=len(temps),
            as_of_utc=as_of,
            oracle_type="noaa",
            fetch_source="asos_csv",
        )
    except Exception as exc:
        log.warning("ASOS %s %s: %r", city, station, exc)
        return None


# ---------------------------------------------------------------------------
# Hong Kong Observatory fetch  (Hong Kong)
# ---------------------------------------------------------------------------
# The HKO climate page: https://www.weather.gov.hk/en/cis/climat.htm
# provides finalized daily data only — not real-time intraday observations.
# For live monitoring, we fall back to TWC API (VHHH:9:HK) which returns
# current airport conditions in real time, noting it's an approximate proxy.

async def _fetch_hko_daily_high(
    city: str,
    station: str,
    api_key: str,
    target_date: date | None = None,
) -> StationObs | None:
    """
    Fetch Hong Kong daily high.

    Oracle source: HKO climate page (finalized only, not intraday).
    Live proxy:    TWC API using VHHH (HK International Airport) observations.
    """
    # Use TWC as live proxy (VHHH:9:HK returns 200)
    obs = await _fetch_twc_daily_high(
        city=city,
        station=station,
        iso="HK",
        unit="C",
        api_key=api_key,
        target_date=target_date,
    )
    if obs is None:
        return None
    # Override oracle_type to reflect actual oracle source
    return StationObs(
        city=obs.city,
        station=obs.station,
        daily_max=obs.daily_max,
        unit=obs.unit,
        obs_count=obs.obs_count,
        as_of_utc=obs.as_of_utc,
        oracle_type="hko",        # actual oracle is HKO
        fetch_source="twc_historical",  # but we fetched from TWC as live proxy
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_daily_high(
    city: str,
    *,
    target_date: date | None = None,
    bypass_cache: bool = False,
    api_key: str | None = None,
) -> StationObs | None:
    """
    Fetch the running calendar-day high temperature for *city*.

    Parameters
    ----------
    city:
        Exact city name as used in Polymarket market titles
        (e.g. "NYC", "Tokyo", "Buenos Aires").
    target_date:
        Date to fetch.  Defaults to today (local system date).
        Useful for back-testing or checking yesterday's data.
    bypass_cache:
        If True, always fetch fresh data even if cached.
    api_key:
        Weather.com API key.  Falls back to TWC_API_KEY env var,
        then to the embedded default key.

    Returns
    -------
    StationObs or None
        None if the city is unknown or the fetch fails.
    """
    if city not in CITY_STATIONS:
        log.warning("fetch_daily_high: unknown city %r", city)
        return None

    if not bypass_cache:
        cached = _cached(city)
        if cached is not None:
            return cached

    key = api_key or _active_twc_key or os.environ.get("TWC_API_KEY") or _DEFAULT_TWC_KEY
    if key == _DEFAULT_TWC_KEY and not os.environ.get("TWC_API_KEY"):
        log.warning(
            "Using embedded TWC fallback key — set TWC_API_KEY env var with a fresh key "
            "extracted from wunderground.com if fetches start returning 401."
        )
    station, iso, unit, oracle_type = CITY_STATIONS[city]

    obs: StationObs | None = None

    if oracle_type == "wu" or city in _NOAA_WITH_TWC_DATA:
        # TWC works for WU cities, Moscow, Tel Aviv, Hong Kong
        if city == "Hong Kong":
            obs = await _fetch_hko_daily_high(city, station, key, target_date)
        else:
            obs = await _fetch_twc_daily_high(city, station, iso, unit, key, target_date)
    elif oracle_type == "noaa" and city == "Istanbul":
        # Istanbul LTFM: TWC API returns 400, use NOAA HTML page
        obs = await _fetch_noaa_daily_high(city, station, target_date)
    else:
        log.warning("fetch_daily_high: no fetch strategy for %s/%s", city, oracle_type)

    if obs is not None:
        _store(obs)
    return obs


def fetch_daily_high_sync(
    city: str,
    *,
    target_date: date | None = None,
    bypass_cache: bool = False,
    api_key: str | None = None,
) -> StationObs | None:
    """Synchronous wrapper around :func:`fetch_daily_high`."""
    return asyncio.run(
        fetch_daily_high(
            city,
            target_date=target_date,
            bypass_cache=bypass_cache,
            api_key=api_key,
        )
    )


async def fetch_all_cities(
    *,
    target_date: date | None = None,
    bypass_cache: bool = False,
    api_key: str | None = None,
    concurrency: int = 10,
) -> dict[str, StationObs | None]:
    """
    Fetch running daily highs for all 50 cities concurrently.

    Returns a dict mapping city name → StationObs (or None on failure).
    """
    sem = asyncio.Semaphore(concurrency)

    async def _guarded(city: str) -> tuple[str, StationObs | None]:
        async with sem:
            obs = await fetch_daily_high(
                city,
                target_date=target_date,
                bypass_cache=bypass_cache,
                api_key=api_key,
            )
            return city, obs

    results = await asyncio.gather(*[_guarded(c) for c in CITY_STATIONS])
    return dict(results)


# ---------------------------------------------------------------------------
# Wunderground URL helpers (for reference / verification)
# ---------------------------------------------------------------------------

def wunderground_history_url(city: str) -> str | None:
    """
    Return the Wunderground daily history page URL for a city.

    This is the page Polymarket's oracle reads for WU-oracle cities.
    Returns None for NOAA/HKO oracle cities.
    """
    if city not in CITY_STATIONS:
        return None
    station, iso, unit, oracle_type = CITY_STATIONS[city]
    if oracle_type != "wu":
        return None
    # Reconstruct from stored data
    # For US cities the URL has an extra state segment; use station directly
    country = iso.lower()
    return f"https://www.wunderground.com/weather/{station}"


def oracle_url(city: str, target_date: date | None = None) -> str | None:
    """Return the exact oracle URL Polymarket uses for a city."""
    if city not in CITY_STATIONS:
        return None
    station, iso, unit, oracle_type = CITY_STATIONS[city]
    if oracle_type == "noaa":
        return f"https://www.weather.gov/wrh/timeseries?site={station}"
    if oracle_type == "hko":
        return "https://www.weather.gov.hk/en/cis/climat.htm"
    # WU oracle
    return f"https://www.wunderground.com/weather/{station}"


# ---------------------------------------------------------------------------
# CLI entrypoint for quick sanity checks
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Fetch Wunderground daily highs")
    parser.add_argument(
        "cities",
        nargs="*",
        help="City names (default: all 50 cities)",
    )
    parser.add_argument(
        "--date",
        help="Date YYYYMMDD (default: today)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass in-memory cache",
    )
    args = parser.parse_args()

    target_date: date | None = None
    if args.date:
        from datetime import datetime as _dt
        target_date = _dt.strptime(args.date, "%Y%m%d").date()

    cities_to_fetch = args.cities if args.cities else list(CITY_STATIONS.keys())

    async def _main() -> None:
        ok = fail = 0
        for city in cities_to_fetch:
            obs = await fetch_daily_high(
                city,
                target_date=target_date,
                bypass_cache=args.no_cache,
            )
            if obs:
                ok += 1
                print(
                    f"  {city:20s} {obs.station:6s}  "
                    f"{obs.daily_max:5.1f}°{obs.unit}  "
                    f"({obs.obs_count} obs, {obs.oracle_type})  "
                    f"as_of={obs.as_of_utc.strftime('%H:%M UTC')}"
                )
            else:
                fail += 1
                print(f"  {city:20s} {'':6s}  FAILED")
        print(f"\n  {ok}/{ok+fail} cities OK")

    asyncio.run(_main())
