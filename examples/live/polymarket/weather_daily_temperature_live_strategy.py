from __future__ import annotations

from decimal import Decimal
import importlib.util
from pathlib import Path
import sys

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.data import DataType
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

try:
    from examples.live.polymarket.weather_temperature_data_client import (
        TemperatureUpdate,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.weather_temperature_data_client"
    module_path = Path(__file__).resolve().with_name("weather_temperature_data_client.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    TemperatureUpdate = module.TemperatureUpdate


class WeatherDailyTemperaturePaperStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    preset: WeatherTemperatureStrategyPreset
    order_qty: Decimal
    token_side: str = "yes"
    family_instrument_ids: tuple[InstrumentId, ...] = ()
    target_usd_per_market: Decimal | None = None
    min_order_size_shares: Decimal = Decimal("0")
    max_stake_per_market: Decimal | None = None
    max_open_positions: int | None = None
    max_total_open_stake: Decimal | None = None
    skip_entry: bool = False
    city: str = ""
    threshold: float | None = None
    threshold_unit: str = "C"
    band_type: str = "or_higher"  # "exact" | "or_higher" | "or_lower"


class WeatherDailyTemperaturePaperStrategy(Strategy):
    """Simple weather daily-temperature paper strategy.

    Subscribes to quote ticks for the instrument. On each quote tick, checks
    whether entry conditions are met using ``should_enter_temperature_market``.
    If conditions are satisfied and no position is open, submits a BUY market
    order.  If ``preset.take_profit_price`` is set, watches for the bid to
    reach that level after entry and submits a SELL limit order at that price.
    One entry and one exit per strategy instance per market.
    """

    def __init__(self, config: WeatherDailyTemperaturePaperStrategyConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self._entry_submitted = False
        self._exit_submitted = False
        self._latest_temp: TemperatureUpdate | None = None

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
        if self.config.city:
            self.subscribe_data(DataType(TemperatureUpdate))
        if self.config.skip_entry:
            self._entry_submitted = True

    def on_data(self, data) -> None:
        """Handle incoming TemperatureUpdate events."""
        if isinstance(data, TemperatureUpdate) and data.city == self.config.city:
            self._latest_temp = data
            self.log.debug(
                f"Temp update {data.city}: {data.daily_max}{data.unit} "
                f"({data.obs_count} obs, max so far)"
            )

    def _temperature_gate(self) -> bool:
        """Return False to block entry based on live temperature signal.

        Logic varies by band_type:

        or_higher  — YES resolves if daily high >= threshold.
                     Block YES if high is impossibly far below threshold late in day.
                     Block NO  if high has already crossed threshold (certain YES = certain NO loss).

        or_lower   — YES resolves if daily high <= threshold.
                     Block YES if high has already exceeded threshold (certain NO).
                     Block NO  if high is well below threshold late in day (certain YES = certain NO loss).

        exact      — YES resolves if daily high lands in this specific band.
                     Wunderground rounds to whole degrees, so the band is ±0.5 of threshold.
                     Block YES if running high has already risen past this band (certain miss).
                     Block YES if running high is more than 2 degrees below band late in day.
                     Block NO  if running high is currently sitting in the band (likely to resolve YES).
        """
        obs = self._latest_temp
        threshold = self.config.threshold
        if obs is None or threshold is None:
            return True  # no data available → don't block

        side = self.config.token_side       # "yes" or "no"
        band = self.config.band_type        # "exact" | "or_higher" | "or_lower"
        hi = obs.daily_max
        unit = obs.unit
        # "past peak" heuristic: 20+ observations means we're well into the afternoon
        past_peak = obs.obs_count >= 20

        if band == "or_higher":
            if side == "yes":
                gap = threshold - hi        # positive = still below threshold
                far_miss = gap > (8 if unit == "F" else 5)
                if far_miss and past_peak:
                    self.log.info(
                        f"[TEMP GATE] Block YES or_higher: {obs.city} hi={hi}{unit} "
                        f"threshold={threshold}{unit} gap={gap:.1f} obs={obs.obs_count}"
                    )
                    return False
            elif side == "no":
                if hi >= threshold:
                    self.log.info(
                        f"[TEMP GATE] Block NO or_higher: {obs.city} hi={hi}{unit} "
                        f"already >= threshold={threshold}{unit}"
                    )
                    return False

        elif band == "or_lower":
            if side == "yes":
                if hi > threshold:
                    self.log.info(
                        f"[TEMP GATE] Block YES or_lower: {obs.city} hi={hi}{unit} "
                        f"already > threshold={threshold}{unit}"
                    )
                    return False
            elif side == "no":
                gap = threshold - hi        # positive = still below threshold
                far_below = gap > (8 if unit == "F" else 5)
                if far_below and past_peak:
                    self.log.info(
                        f"[TEMP GATE] Block NO or_lower: {obs.city} hi={hi}{unit} "
                        f"threshold={threshold}{unit} gap={gap:.1f} obs={obs.obs_count}"
                    )
                    return False

        elif band == "exact":
            # Wunderground resolution is whole degrees, so band window is [threshold-0.5, threshold+0.5)
            in_band = abs(hi - threshold) < 0.5
            above_band = hi > threshold + 0.5
            gap_below = threshold - hi      # positive = still below band

            if side == "yes":
                # Temp has already risen past this band → certain NO for this market
                if above_band:
                    self.log.info(
                        f"[TEMP GATE] Block YES exact: {obs.city} hi={hi}{unit} "
                        f"already above band={threshold}{unit}"
                    )
                    return False
                # Temp is more than 2 degrees below band late in day → unlikely to reach it
                if gap_below > 2 and past_peak:
                    self.log.info(
                        f"[TEMP GATE] Block YES exact: {obs.city} hi={hi}{unit} "
                        f"band={threshold}{unit} gap={gap_below:.1f} obs={obs.obs_count}"
                    )
                    return False
            elif side == "no":
                # Temp is sitting in this band → strong signal it resolves YES → block NO
                if in_band and past_peak:
                    self.log.info(
                        f"[TEMP GATE] Block NO exact: {obs.city} hi={hi}{unit} "
                        f"currently in band={threshold}{unit} obs={obs.obs_count}"
                    )
                    return False

        return True

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self.instrument is None:
            return

        bid = float(tick.bid_price.as_double())
        ask = float(tick.ask_price.as_double())

        # --- Entry path ---
        if not self._entry_submitted:
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

            if not self._temperature_gate():
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

            # Polymarket market BUY orders must be quote-denominated (USDC).
            # order_qty is in shares; multiply by ask to get the USDC stake.
            usd_stake = order_qty * Decimal(str(ask))

            self._entry_submitted = True
            order = self.order_factory.market(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.BUY,
                quantity=self.instrument.make_qty(usd_stake),
                quote_quantity=True,
            )
            self.submit_order(order)
            return

        # --- Exit path (take-profit or stop-loss) ---
        if self._exit_submitted:
            return

        take_profit = self.config.preset.take_profit_price
        stop_loss = self.config.preset.stop_loss_price

        if take_profit is None and stop_loss is None:
            return

        hit_take_profit = take_profit is not None and bid >= take_profit
        hit_stop_loss = stop_loss is not None and bid <= stop_loss

        if not hit_take_profit and not hit_stop_loss:
            return

        open_positions = list(
            self.cache.positions_open(
                instrument_id=self.config.instrument_id,
                strategy_id=self.id,
            ),
        )
        if not open_positions:
            return

        qty = open_positions[0].quantity
        buffer = self.instrument.make_qty(Decimal("0.02"))
        qty = qty - buffer
        if float(qty) <= 0.0:
            return

        self._exit_submitted = True
        if hit_take_profit and take_profit is not None:
            exit_order = self.order_factory.limit(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.SELL,
                quantity=qty,
                price=self.instrument.make_price(Decimal(str(take_profit))),
            )
        else:
            # Stop loss: sell at market. Sell token-denominated (qty already buffered)
            # to avoid CLOB balance rejection — Polymarket sells are share-denominated.
            exit_order = self.order_factory.market(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.SELL,
                quantity=qty,
            )
        self.submit_order(exit_order)

    def on_stop(self) -> None:
        if self.instrument is None:
            return
        self.cancel_all_orders(self.instrument.id)
