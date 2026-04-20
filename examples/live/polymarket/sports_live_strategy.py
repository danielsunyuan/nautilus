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
    from examples.live.polymarket.sports_strategy_library import (
        SportsStrategyPreset,
        should_enter_sports_market,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.sports_strategy_library"
    module_path = Path(__file__).resolve().with_name("sports_strategy_library.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    SportsStrategyPreset = module.SportsStrategyPreset
    should_enter_sports_market = module.should_enter_sports_market


class SportsPaperStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    preset: SportsStrategyPreset
    order_qty: Decimal
    sport: str = ""
    market_type: str = ""
    game_time: str = ""
    family_instrument_ids: tuple[InstrumentId, ...] = ()
    target_usd_per_market: Decimal | None = None
    min_order_size_shares: Decimal = Decimal("0")
    max_stake_per_market: Decimal | None = None
    max_open_positions: int | None = None
    max_total_open_stake: Decimal | None = None


class SportsPaperStrategy(Strategy):
    """Simple sports paper strategy.

    Subscribes to quote ticks for the instrument. On each quote tick, checks
    whether entry conditions are met using ``should_enter_sports_market``.
    If conditions are satisfied and no position is open, submits a BUY market
    order. Holds to resolution (no exit logic in first release). One entry per
    strategy instance per market.
    """

    def __init__(self, config: SportsPaperStrategyConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self._entry_submitted = False

    def _iter_open_positions(self):
        try:
            return list(self.cache.positions_open())
        except TypeError:
            return []
        except Exception:
            return []

    def _iter_inflight_orders(self):
        try:
            return list(self.cache.orders_inflight())
        except TypeError:
            return []
        except Exception:
            return []

    @staticmethod
    def _position_instrument_id(position) -> str | None:
        instrument_id = getattr(position, "instrument_id", None)
        if instrument_id is None:
            return None
        return str(instrument_id)

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
        if instrument_id is None:
            return None
        return str(instrument_id)

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
        target_usd = self.config.target_usd_per_market
        if target_usd is None or target_usd <= 0:
            return self.config.order_qty

        ask_decimal = Decimal(str(ask))
        if ask_decimal <= 0:
            return Decimal("0")

        min_order_size = self.config.min_order_size_shares
        desired_qty = target_usd / ask_decimal
        quantity = desired_qty if desired_qty >= min_order_size else min_order_size

        max_stake = self.config.max_stake_per_market
        if max_stake is not None and quantity * ask_decimal > max_stake:
            if min_order_size > 0 and min_order_size * ask_decimal <= max_stake:
                quantity = min_order_size
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

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self.instrument is None:
            return
        if self._entry_submitted:
            return

        bid = float(tick.bid_price.as_double())
        ask = float(tick.ask_price.as_double())
        bid_size = float(tick.bid_size.as_double())
        ask_size = float(tick.ask_size.as_double())

        if not should_enter_sports_market(
            preset=self.config.preset,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            sport=self.config.sport,
            market_type=self.config.market_type,
            game_time=self.config.game_time,
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

        if self._family_has_existing_risk():
            return

        if self._portfolio_limit_reached():
            return

        order_qty = self._compute_order_quantity(ask)
        if order_qty <= 0:
            return

        self._entry_submitted = True
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(order_qty),
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        if self.instrument is None:
            return
        self.cancel_all_orders(self.instrument.id)
