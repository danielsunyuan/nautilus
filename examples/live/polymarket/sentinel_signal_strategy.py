"""
Nautilus Strategy that enters a Polymarket position when a Sentinel news signal
exists for the instrument. Reads signals from a shared JSONL file.

Follows the exact pattern of SportsPaperStrategy.
One entry per strategy instance, no exit logic (holds to market resolution).
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy


def load_signals_for_instrument(
    *,
    signal_path: str | Path,
    instrument_id: str,
) -> list[dict[str, Any]]:
    """Read JSONL signal file and return entries for the given instrument_id."""
    path = Path(signal_path)
    if not path.exists():
        return []
    signals = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    entry.get("event") == "sentinel_news_signal"
                    and str(entry.get("instrument_id") or "") == str(instrument_id)
                ):
                    signals.append(entry)
    except OSError:
        pass
    return signals


def should_enter_sentinel_market(
    *,
    signal: dict[str, Any],
    ask: float,
    min_ask: float,
    max_ask: float,
    min_relevance: float,
    entry_submitted: bool,
) -> bool:
    """Pure entry predicate — no side effects."""
    if entry_submitted:
        return False
    relevance = float(signal.get("relevance_score") or 0.0)
    if relevance < float(min_relevance):
        return False
    ask_f = float(ask)
    return float(min_ask) <= ask_f <= float(max_ask)


class SentinelSignalStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    signal_path: str
    order_qty: Decimal = Decimal("10")
    min_ask: float = 0.30
    max_ask: float = 0.85
    min_relevance: float = 0.25
    close_positions_on_stop: bool = False


class SentinelSignalStrategy(Strategy):
    """
    Enters a single paper trade when a Sentinel news signal exists for this instrument
    and the ask price is within the configured band.

    Holds to resolution — no exit logic. One entry per strategy instance.
    """

    def __init__(self, config: SentinelSignalStrategyConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self._signals: list[dict[str, Any]] = []
        self._entry_submitted = False

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self.config.instrument_id}")
            self.stop()
            return
        self._signals = load_signals_for_instrument(
            signal_path=self.config.signal_path,
            instrument_id=str(self.config.instrument_id),
        )
        if self._signals:
            self.log.info(
                f"Loaded {len(self._signals)} signal(s) for {self.config.instrument_id} "
                f"(best relevance: {max(s.get('relevance_score', 0) for s in self._signals):.3f})"
            )
        else:
            self.log.warning(f"No signals found for {self.config.instrument_id} — will not enter")
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self.instrument is None or self._entry_submitted:
            return
        if not self._signals:
            return

        ask = float(tick.ask_price.as_double())
        best_signal = max(self._signals, key=lambda s: float(s.get("relevance_score") or 0.0))

        if not should_enter_sentinel_market(
            signal=best_signal,
            ask=ask,
            min_ask=self.config.min_ask,
            max_ask=self.config.max_ask,
            min_relevance=self.config.min_relevance,
            entry_submitted=self._entry_submitted,
        ):
            return

        self._entry_submitted = True
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(self.config.order_qty),
        )
        self.log.info(
            f"Entering {self.config.instrument_id} at ask={ask:.4f} "
            f"(signal relevance={best_signal.get('relevance_score'):.3f}, "
            f"headline={best_signal.get('headline', '')[:60]!r})"
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        if self.instrument is None:
            return
        self.cancel_all_orders(self.instrument.id)
        if self.config.close_positions_on_stop:
            self.close_all_positions(self.instrument.id)
