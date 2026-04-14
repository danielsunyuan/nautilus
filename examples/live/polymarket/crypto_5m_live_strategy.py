from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
from typing import Literal
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy

try:
    from examples.live.polymarket.crypto_5m_strategy_library import (
        PolymarketCrypto5mSignalEngine,
        PolymarketCrypto5mStrategyPreset,
        effective_stop_loss_price,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.crypto_5m_strategy_library"
    module_path = Path(__file__).resolve().with_name("crypto_5m_strategy_library.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    PolymarketCrypto5mSignalEngine = module.PolymarketCrypto5mSignalEngine
    PolymarketCrypto5mStrategyPreset = module.PolymarketCrypto5mStrategyPreset
    effective_stop_loss_price = module.effective_stop_loss_price


class PolymarketCrypto5mPaperStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    preset: PolymarketCrypto5mStrategyPreset
    market_end_time: datetime
    order_qty: Decimal
    token_side: Literal["up", "down"] = "up"
    close_positions_on_stop: bool = True


def _quote_components(tick: QuoteTick) -> tuple[datetime, float, float, float, float]:
    return (
        unix_nanos_to_dt(tick.ts_event),
        float(tick.bid_price.as_double()),
        float(tick.ask_price.as_double()),
        float(tick.bid_size.as_double()),
        float(tick.ask_size.as_double()),
    )


def plan_quote_action(
    *,
    engine: PolymarketCrypto5mSignalEngine,
    preset: PolymarketCrypto5mStrategyPreset,
    instrument_id: str,
    token_side: Literal["up", "down"],
    market_end_time: datetime,
    now: datetime,
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
    open_position: Any | None,
    has_inflight_orders: bool,
    max_bid_seen: float,
) -> tuple[dict[str, Any] | None, float]:
    engine.record_top_of_book(
        token_id=instrument_id,
        best_bid=bid,
        best_ask=ask,
        best_bid_size=bid_size,
        best_ask_size=ask_size,
        now=now,
    )

    if has_inflight_orders:
        return None, max_bid_seen

    if open_position is not None:
        max_bid_seen = max(max_bid_seen, bid)
        stop_price = effective_stop_loss_price(
            preset=preset,
            entry_price=float(open_position.avg_px_open),
            max_bid_seen=max_bid_seen,
        )
        if bid >= float(preset.exit_price):
            return {"kind": "exit", "reason": "target", "position": open_position}, max_bid_seen
        if stop_price is not None and bid <= float(stop_price):
            return {"kind": "exit", "reason": "stop_loss", "position": open_position}, max_bid_seen
        return None, max_bid_seen

    signal = engine.entry_signal(
        now=now,
        market_end=market_end_time,
        side=token_side,
    )
    if signal is None:
        return None, max_bid_seen
    return {"kind": "enter", "reason": "signal", "signal": signal}, bid


class PolymarketCrypto5mPaperStrategy(Strategy):
    def __init__(self, config: PolymarketCrypto5mPaperStrategyConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self.engine = PolymarketCrypto5mSignalEngine(
            preset=config.preset,
            token_sides={str(config.instrument_id): config.token_side},
        )
        self._max_bid_seen = 0.0

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

        now, bid, ask, bid_size, ask_size = _quote_components(tick)
        open_positions = list(self.cache.positions_open(instrument_id=self.config.instrument_id, strategy_id=self.id))
        inflight_orders = list(self.cache.orders_inflight(instrument_id=self.config.instrument_id, strategy_id=self.id))
        action, self._max_bid_seen = plan_quote_action(
            engine=self.engine,
            preset=self.config.preset,
            instrument_id=str(self.config.instrument_id),
            token_side=self.config.token_side,
            market_end_time=self.config.market_end_time,
            now=now,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            open_position=open_positions[-1] if open_positions else None,
            has_inflight_orders=bool(inflight_orders),
            max_bid_seen=self._max_bid_seen,
        )
        if action is None:
            return

        if action["kind"] == "exit":
            self.close_position(action["position"])
            return

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
        if self.config.close_positions_on_stop:
            self.close_all_positions(self.instrument.id)
