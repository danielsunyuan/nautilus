from __future__ import annotations

from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
from typing import Any

try:
    from nautilus_trader.config import StrategyConfig
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.model.instruments import Instrument
    from nautilus_trader.trading.strategy import Strategy
except (ImportError, ModuleNotFoundError):
    from dataclasses import dataclass as _dataclass

    class _FrozenBase:
        def __init_subclass__(cls, frozen: bool = False, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)
            if frozen:
                _dataclass(frozen=True)(cls)

    StrategyConfig = _FrozenBase  # type: ignore[misc,assignment]
    OrderSide = type("OrderSide", (), {"BUY": "BUY"})  # type: ignore[assignment,misc]
    InstrumentId = None  # type: ignore[assignment,misc]
    Instrument = None  # type: ignore[assignment,misc]
    Strategy = object  # type: ignore[assignment,misc]

try:
    from examples.live.polymarket.weather_ensemble_strategy_library import (
        WeatherEnsembleCandidate,
        WeatherEnsembleStrategyPreset,
        should_enter_weather_ensemble_market,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_ensemble_strategy_library"
    module_path = Path(__file__).resolve().with_name("weather_ensemble_strategy_library.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    WeatherEnsembleCandidate = module.WeatherEnsembleCandidate
    WeatherEnsembleStrategyPreset = module.WeatherEnsembleStrategyPreset
    should_enter_weather_ensemble_market = module.should_enter_weather_ensemble_market


class WeatherEnsemblePaperStrategyConfig(StrategyConfig, frozen=True):
    condition_id: str | None = None
    yes_token_id: str | None = None
    no_token_id: str | None = None
    model_yes_probability: float | None = None
    market_yes_price: float | None = None
    edge: float | None = None
    selected_side: str | None = None
    confidence: float | None = None
    close_positions_on_stop: bool = True


class WeatherEnsemblePaperStrategy(Strategy):
    def __init__(self, config: WeatherEnsemblePaperStrategyConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self._entry_submitted = False

    def _iter_open_positions(self):
        try:
            return list(self.cache.positions_open())
        except Exception:
            return []

    def _iter_inflight_orders(self):
        try:
            return list(self.cache.orders_inflight())
        except Exception:
            return []

    @staticmethod
    def _position_instrument_id(position) -> str | None:
        instrument_id = getattr(position, "instrument_id", None)
        return None if instrument_id is None else str(instrument_id)

    @staticmethod
    def _position_stake(position) -> Decimal:
        avg_px_open = getattr(position, "avg_px_open", None)
        quantity = getattr(position, "peak_qty", None) or getattr(position, "quantity", None)
        if avg_px_open is None or quantity is None:
            return Decimal("0")
        return Decimal(str(avg_px_open)) * Decimal(str(quantity))

    @staticmethod
    def _order_instrument_id(order) -> str | None:
        instrument_id = getattr(order, "instrument_id", None)
        return None if instrument_id is None else str(instrument_id)

    def _family_has_existing_risk(self) -> bool:
        family_ids = {str(i) for i in self.config.family_instrument_ids}
        family_ids.add(str(self.config.instrument_id))
        for position in self._iter_open_positions():
            if self._position_instrument_id(position) in family_ids:
                return True
        for order in self._iter_inflight_orders():
            if self._order_instrument_id(order) in family_ids:
                return True
        return False

    def _portfolio_limit_reached(self) -> bool:
        max_open_positions = self.config.max_open_positions
        if max_open_positions is not None and len(self._iter_open_positions()) >= max_open_positions:
            return True

        max_total_open_stake = self.config.max_total_open_stake
        if max_total_open_stake is not None:
            total_stake = sum(self._position_stake(position) for position in self._iter_open_positions())
            if total_stake >= max_total_open_stake:
                return True

        return False

    def _compute_order_quantity(self, ask: float) -> Decimal:
        ask_decimal = Decimal(str(ask))
        if ask_decimal <= 0:
            return Decimal("0")

        desired_qty = self.config.target_usd_per_market / ask_decimal
        quantity = max(desired_qty, self.config.min_order_size_shares)

        max_stake = self.config.max_stake_per_market
        if max_stake is not None and quantity * ask_decimal > max_stake:
            if self.config.min_order_size_shares > 0 and self.config.min_order_size_shares * ask_decimal <= max_stake:
                quantity = self.config.min_order_size_shares
            else:
                return Decimal("0")

        return quantity

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument for {self.config.instrument_id}")
            self.stop()
            return
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_quote_tick(self, tick) -> None:
        if self.instrument is None or self._entry_submitted:
            return

        bid = float(tick.bid_price.as_double())
        ask = float(tick.ask_price.as_double())
        bid_size = float(tick.bid_size.as_double())
        ask_size = float(tick.ask_size.as_double())

        if not should_enter_weather_ensemble_market(
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

        if self.config.preset.one_position_per_family and self._family_has_existing_risk():
            return

        if self._portfolio_limit_reached():
            return

        order_qty = self._compute_order_quantity(ask)
        if order_qty <= 0:
            return

        usd_stake = order_qty * Decimal(str(ask))
        self._entry_submitted = True
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(usd_stake),
            quote_quantity=True,
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        if self.instrument is None:
            return
        self.cancel_all_orders(self.instrument.id)
