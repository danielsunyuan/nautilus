from __future__ import annotations

from decimal import Decimal
import importlib.util
from pathlib import Path
import sys

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy

try:
    from examples.live.polymarket.weather_daily_temperature_strategy_library import (
        WeatherTemperatureStrategyPreset,
        should_enter_temperature_market,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_daily_temperature_strategy_library"
    module_path = Path(__file__).resolve().with_name("weather_daily_temperature_strategy_library.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    WeatherTemperatureStrategyPreset = module.WeatherTemperatureStrategyPreset
    should_enter_temperature_market = module.should_enter_temperature_market


class WeatherDailyTemperaturePaperStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    preset: WeatherTemperatureStrategyPreset
    order_qty: Decimal
    token_side: str = "yes"


class WeatherDailyTemperaturePaperStrategy(Strategy):
    """Simple weather daily-temperature paper strategy.

    Subscribes to quote ticks for the instrument. On each quote tick, checks
    whether entry conditions are met using ``should_enter_temperature_market``.
    If conditions are satisfied and no position is open, submits a BUY market
    order.  Holds to resolution (no exit logic in first release). One entry per
    strategy instance per market.
    """

    def __init__(self, config: WeatherDailyTemperaturePaperStrategyConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self._entry_submitted = False

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument for {self.config.instrument_id}")
            self.stop()
            return
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self.instrument is None:
            return
        if self._entry_submitted:
            return

        bid = float(tick.bid_price.as_double())
        ask = float(tick.ask_price.as_double())
        bid_size = float(tick.bid_size.as_double())
        ask_size = float(tick.ask_size.as_double())

        if not should_enter_temperature_market(
            preset=self.config.preset,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
        ):
            return

        open_positions = list(
            self.cache.positions_open(
                instrument_id=self.config.instrument_id,
                strategy_id=self.id,
            ),
        )
        if open_positions:
            return

        inflight_orders = list(
            self.cache.orders_inflight(
                instrument_id=self.config.instrument_id,
                strategy_id=self.id,
            ),
        )
        if inflight_orders:
            return

        self._entry_submitted = True
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(self.config.order_qty),
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        if self.instrument is None:
            return
        self.cancel_all_orders(self.instrument.id)
