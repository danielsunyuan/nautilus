# Alt Data Clients — Nautilus Native Data Integration Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
>
> **For Claude:** This is a manager handoff. Execute with parallel subagents only on disjoint write sets.

**Goal:** Replace daemon-level httpx data fetching with proper Nautilus `LiveDataClient` integrations so that external data (weather forecasts, exchange candles) flows through the engine's message bus alongside Polymarket CLOB quotes.

**Architecture:** Each external data source gets its own `LiveDataClient` subclass that polls or streams data, wraps it in a typed `Data` event, and publishes via `self._handle_data()`. Strategies subscribe to these events alongside quote ticks. The daemon no longer fetches data itself — it just wires up the clients and starts the node.

**Tech Stack:** Python 3.12+, NautilusTrader `LiveDataClient` / `LiveDataClientFactory`, `httpx` for HTTP polling, existing `Data` base class pattern from `weather_temperature_data_client.py`.

---

## Manager Intent

- Follow the exact pattern already proven in `examples/live/polymarket/weather_temperature_data_client.py` (Wunderground poller).
- Each data client is a standalone module: config dataclass, custom `Data` event type, `LiveDataClient` subclass, factory.
- Strategies receive data via `on_data()` callback instead of `strategy.set_candles()` or daemon-injected snapshots.
- Start with the weather ensemble client (active strategy). The exchange candle client pattern is documented for future use if a BTC/crypto strategy is revived.
- Do NOT modify the Polymarket adapter or core NautilusTrader code.

## Scope

**Included:**

- Open-Meteo ensemble forecast data client (polling, 5-minute interval)
- Custom `EnsembleForecastData` event type
- Factory for node registration
- Update `weather_ensemble_live_strategy.py` to consume forecast data via `on_data()` instead of `set_candles()`
- Update `polymarket_weather_ensemble_paper_daemon.py` to register the data client instead of fetching forecasts itself
- Unit tests for data client, event type, and factory

**Excluded:**

- Exchange candle data client (documented pattern only — no active strategy uses it)
- Modifications to NautilusTrader core
- Backfill / historical data replay (future work)

## Reference Implementation

The existing `weather_temperature_data_client.py` demonstrates the complete pattern:

```
WundergroundDataClientConfig(NautilusConfig, frozen=True)
  ├── poll_interval_secs: int = 900
  ├── cities: tuple[str, ...] = ()
  └── api_key: str = ""

TemperatureUpdate(Data)
  ├── city, station, daily_max, unit, obs_count
  ├── ts_event: int  (UNIX ns)
  └── ts_init: int   (UNIX ns)

WundergroundDataClient(LiveDataClient)
  ├── _connect()    → starts poll loop task
  ├── _disconnect() → cancels poll loop
  ├── _subscribe()  → no-op (continuous push)
  └── _poll_loop()  → fetch_daily_high() → TemperatureUpdate → _handle_data()

WundergroundDataClientFactory(LiveDataClientFactory)
  └── create() → WundergroundDataClient(client_id=ClientId("WEATHER"), ...)
```

**Key imports:**
```python
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.core.data import Data
from nautilus_trader.data.messages import SubscribeData, UnsubscribeData
from nautilus_trader.live.data_client import LiveDataClient
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.model.identifiers import ClientId, Venue
```

## Proposed File Surface

Create:

- `examples/live/polymarket/weather_ensemble_data_client.py`
- `tests/unit_tests/examples/test_weather_ensemble_data_client.py`

Modify:

- `examples/live/polymarket/weather_ensemble_live_strategy.py`
- `examples/live/polymarket/polymarket_weather_ensemble_paper_daemon.py`

---

## Fleet Dispatch

### Worker A: Open-Meteo Ensemble Data Client

**Ownership:**
- Create: `examples/live/polymarket/weather_ensemble_data_client.py`
- Test: `tests/unit_tests/examples/test_weather_ensemble_data_client.py`

**Objective:** Build a Nautilus `LiveDataClient` that polls Open-Meteo ensemble forecasts and publishes `EnsembleForecastData` events to the message bus.

**Success Criteria**
- [ ] `EnsembleForecastData(Data)` event type with all forecast fields + required `ts_event`/`ts_init`
- [ ] `OpenMeteoEnsembleDataClient(LiveDataClient)` with poll loop
- [ ] `OpenMeteoEnsembleDataClientFactory(LiveDataClientFactory)` for TradingNode registration
- [ ] Unit tests for event construction, config validation, and factory creation

**Implementation Steps**

1. Create `EnsembleForecastData(Data)` event type:

```python
from nautilus_trader.core.data import Data

class EnsembleForecastData(Data):
    """Ensemble weather forecast published to the Nautilus message bus."""

    def __init__(
        self,
        city: str,
        latitude: float,
        longitude: float,
        target_date: str,           # ISO date string
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
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

    @property
    def member_count(self) -> int:
        return min(len(self.member_highs), len(self.member_lows))
```

2. Create `OpenMeteoEnsembleDataClientConfig(NautilusConfig, frozen=True)`:

```python
class OpenMeteoEnsembleDataClientConfig(NautilusConfig, frozen=True):
    poll_interval_secs: int = 300           # 5 minutes
    base_url: str = "https://ensemble-api.open-meteo.com/v1/ensemble"
    model_name: str = "icon_seamless_eps"
    temperature_unit: str = "celsius"
    timezone: str = "GMT"
    timeout_seconds: float = 15.0
    forecast_days: int = 2                  # today + tomorrow
```

3. Create `OpenMeteoEnsembleDataClient(LiveDataClient)`:
   - `__init__`: accept config, store city coordinate mapping (reuse the 50-city mapping from the daemon)
   - `_connect()`: start poll loop via `self.create_task(self._poll_loop())`
   - `_disconnect()`: cancel poll loop
   - `_subscribe()` / `_unsubscribe()`: no-op (continuous push)
   - `_poll_loop()`:
     - For each city with known coordinates, call Open-Meteo API via `httpx`
     - Parse response using existing `parse_open_meteo_daily_payload()` from `weather_ensemble_forecast.py`
     - Convert `EnsembleForecastSnapshot` → `EnsembleForecastData`
     - Call `self._handle_data(data)` to publish to engine
     - Sleep `poll_interval_secs` between sweeps
     - Catch and log errors per-city (don't crash the loop)

4. Create `OpenMeteoEnsembleDataClientFactory(LiveDataClientFactory)`:

```python
class OpenMeteoEnsembleDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: OpenMeteoEnsembleDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> OpenMeteoEnsembleDataClient:
        return OpenMeteoEnsembleDataClient(
            loop=loop,
            client_id=ClientId("OPEN_METEO"),
            venue=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
```

5. Write unit tests:
   - `test_ensemble_forecast_data_has_required_properties` — verify `ts_event`, `ts_init`
   - `test_ensemble_forecast_data_fields` — verify all fields accessible
   - `test_config_defaults` — verify default poll interval, URL, etc.
   - `test_factory_creates_client` — mock dependencies, verify client type
   - `test_client_id_is_open_meteo` — verify ClientId

**Verification Commands**
```bash
cd /home/atlas/EL/nautilus
python -m pytest tests/unit_tests/examples/test_weather_ensemble_data_client.py --noconftest -q
```

**Commit**
```bash
git add examples/live/polymarket/weather_ensemble_data_client.py \
  tests/unit_tests/examples/test_weather_ensemble_data_client.py
git commit -m "feat: add Open-Meteo ensemble data client for Nautilus"
```

### Worker B: Wire Data Client Into Daemon And Strategy

**Ownership:**
- Modify: `examples/live/polymarket/polymarket_weather_ensemble_paper_daemon.py`
- Modify: `examples/live/polymarket/weather_ensemble_live_strategy.py`

**Objective:** Replace the daemon's direct httpx forecast fetching with the Nautilus data client, and update the strategy to receive forecast data via `on_data()`.

**Success Criteria**
- [ ] Daemon registers `OpenMeteoEnsembleDataClientFactory` with the TradingNode
- [ ] Strategy subscribes to `EnsembleForecastData` in `on_start()`
- [ ] Strategy receives forecast updates via `on_data()` and uses them for entry decisions
- [ ] Daemon no longer calls `_fetch_forecasts()` directly — the data client handles it
- [ ] Existing unit tests still pass

**Implementation Steps**

1. Update the daemon's node config to register the data client:

```python
# In _default_run_round or equivalent:
node.add_data_client_factory("OPEN_METEO", OpenMeteoEnsembleDataClientFactory)

# In TradingNodeConfig.data_clients:
data_clients={
    POLYMARKET: PolymarketDataClientConfig(...),
    "OPEN_METEO": OpenMeteoEnsembleDataClientConfig(
        poll_interval_secs=300,
        forecast_days=2,
    ),
},
```

2. Update the strategy to subscribe to forecast data:

```python
class WeatherEnsemblePaperStrategy(Strategy):
    def __init__(self, config):
        super().__init__(config)
        self._latest_forecast: EnsembleForecastData | None = None
        self._entry_submitted = False

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument:
            self.subscribe_quote_ticks(self.config.instrument_id)
        # Subscribe to forecast data
        self.subscribe_data(
            data_type=DataType(EnsembleForecastData),
        )

    def on_data(self, data: Data) -> None:
        if isinstance(data, EnsembleForecastData):
            if data.city == self.config.candidate.city:
                self._latest_forecast = data

    def on_quote_tick(self, tick) -> None:
        # Use self._latest_forecast for entry decisions
        # instead of daemon-injected data
        ...
```

3. Remove the daemon's `_fetch_forecasts()` function and the `_build_candidates()` pre-fetch logic. The daemon should:
   - Still discover markets via Gamma
   - Still compute candidates using the signal engine
   - But get forecast data from the data client via the strategy, not from direct API calls
   - The daemon's round still needs to build candidates BEFORE starting the node — this is the tricky part

**NOTE:** There's an architectural tension here. The daemon needs forecast data to decide WHICH markets to trade (candidate selection) BEFORE building the TradingNode. But the data client only runs inside the node. Two approaches:

**Option A (pragmatic):** Keep daemon-level forecast fetch for candidate selection, but also register the data client so the strategy gets streaming updates during the node runtime. The daemon fetch is a one-shot snapshot; the data client provides ongoing updates.

**Option B (pure):** Run a lightweight "discovery" TradingNode first with just the data client, collect forecasts, select candidates, stop that node, then build the real trading node with strategies attached. Adds complexity.

**Recommended: Option A.** The daemon-level fetch is fine for candidate selection (it's a one-shot decision). The data client adds value by providing updated forecasts during the node's runtime, allowing the strategy to react to forecast changes before entry.

**Verification Commands**
```bash
cd /home/atlas/EL/nautilus
python -m pytest \
  tests/unit_tests/examples/test_polymarket_weather_ensemble_daemon.py \
  tests/unit_tests/examples/test_weather_ensemble_data_client.py \
  --noconftest -q
```

**Commit**
```bash
git add examples/live/polymarket/polymarket_weather_ensemble_paper_daemon.py \
  examples/live/polymarket/weather_ensemble_live_strategy.py
git commit -m "feat: wire Open-Meteo data client into weather ensemble daemon"
```

---

## Future: Exchange Candle Data Client (Pattern Only)

If a BTC or crypto strategy is revived, build an exchange candle data client following the same pattern:

```python
class CandleData(Data):
    """1-minute OHLCV candle from external exchange."""
    # exchange, symbol, open, high, low, close, volume, ts_event, ts_init

class ExchangeCandleDataClientConfig(NautilusConfig, frozen=True):
    exchange: str = "binance"    # binance, coinbase, kraken, bybit
    symbol: str = "BTCUSDT"
    interval: str = "1m"
    poll_interval_secs: int = 60

class ExchangeCandleDataClient(LiveDataClient):
    # Same poll loop pattern, calls exchange REST API
    # Publishes CandleData events

class ExchangeCandleDataClientFactory(LiveDataClientFactory):
    # Factory for TradingNode registration
```

This is NOT being built now — just documented for future reference.

---

## Integration Pass

After Workers A and B:

1. Run full test suite:
   ```bash
   python -m pytest \
     tests/unit_tests/examples/test_weather_ensemble_data_client.py \
     tests/unit_tests/examples/test_polymarket_weather_ensemble_daemon.py \
     tests/unit_tests/examples/test_weather_ensemble_forecast.py \
     tests/unit_tests/examples/test_weather_ensemble_signal_engine.py \
     --noconftest -q
   ```

2. Restart the daemon:
   ```bash
   docker compose -f .docker/docker-compose.yml --profile vpn restart weather-ensemble-daemon-vpn
   ```

3. Verify logs show data client connecting and publishing forecast events:
   ```bash
   docker logs nautilus-weather-ensemble-daemon-vpn --tail 20
   ```

4. Verify JSONL output still contains forecast diagnostics (model_yes_probability, edge, etc.)
