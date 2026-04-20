"""
Reusable Polymarket 5-minute strategy presets and signal evaluation helpers.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from typing import Literal


StrategyMode = Literal[
    "basic",
    "microprice",
    "support_ratio",
    "quote_stability",
    "flow_imbalance",
    "microprice_support",
    "spread_switch",
    "binance_momentum",
    "microprice_momentum",
    "adaptive_stop",
    "trailing_stop",
]


@dataclass(frozen=True, slots=True)
class PolymarketCrypto5mStrategyPreset:
    name: str
    rationale: str
    entry_price: float
    exit_price: float
    stop_loss_price: float | None
    min_seconds_before_close: float
    max_spread: float
    min_threshold_seconds: float
    min_supported_bid_price: float
    min_best_bid_size: float
    mode: StrategyMode = "basic"
    min_seconds_after_open: float = 0.0
    min_total_top_size: float = 0.0
    support_ratio: float = 0.0
    microprice_epsilon: float = 0.0
    stability_seconds: float = 0.0
    entry_price_tight: float = 0.0
    max_entry_price: float = 0.0
    spread_tight: float = 0.0
    flow_window_seconds: float = 10.0
    flow_min_samples: int = 3
    flow_min_imbalance: float = 0.0
    max_drawdown_frac: float = 0.0
    trail_frac: float = 0.0
    momentum_window_seconds: float = 30.0
    momentum_min_samples: int = 3


@dataclass(slots=True)
class EntrySignal:
    token_id: str
    side: Literal["up", "down"]
    ask_price: float


@dataclass(slots=True)
class TokenSignalState:
    token_id: str
    side: Literal["up", "down"]
    best_bid: float | None = None
    best_ask: float | None = None
    best_bid_size: float | None = None
    best_ask_size: float | None = None
    threshold_since: datetime | None = None
    stable_since: datetime | None = None
    last_bid: float | None = None
    last_ask: float | None = None


@dataclass(frozen=True, slots=True)
class FlowSample:
    timestamp: float
    bid_size: float
    ask_size: float


@dataclass(frozen=True, slots=True)
class FlowImbalanceSignal:
    imbalance: float
    mean_bid_size: float
    mean_ask_size: float
    samples: int
    sufficient: bool


@dataclass(slots=True)
class FlowImbalanceTracker:
    window_seconds: float = 10.0
    min_samples: int = 3
    _samples: deque[FlowSample] = field(default_factory=deque)

    def add_sample(self, *, bid_size: float, ask_size: float, timestamp: float) -> None:
        self._samples.append(
            FlowSample(
                timestamp=timestamp,
                bid_size=max(0.0, float(bid_size)),
                ask_size=max(0.0, float(ask_size)),
            ),
        )
        self._evict(timestamp)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

    def signal(self, *, now: float) -> FlowImbalanceSignal:
        self._evict(now)
        samples = len(self._samples)
        if samples < self.min_samples:
            return FlowImbalanceSignal(
                imbalance=0.0,
                mean_bid_size=0.0,
                mean_ask_size=0.0,
                samples=samples,
                sufficient=False,
            )

        total_bid = sum(sample.bid_size for sample in self._samples)
        total_ask = sum(sample.ask_size for sample in self._samples)
        denom = total_bid + total_ask
        imbalance = 0.0 if denom == 0 else (total_bid - total_ask) / denom
        return FlowImbalanceSignal(
            imbalance=round(imbalance, 6),
            mean_bid_size=round(total_bid / samples, 6),
            mean_ask_size=round(total_ask / samples, 6),
            samples=samples,
            sufficient=True,
        )


@dataclass(frozen=True, slots=True)
class PriceSample:
    timestamp: float
    mid_price: float


@dataclass(frozen=True, slots=True)
class MomentumSignal:
    direction: Literal["up", "down", "flat", "insufficient_data"]
    mid_price: float
    oldest_mid_price: float | None
    price_change_pct: float
    samples: int


@dataclass(slots=True)
class MomentumTracker:
    window_seconds: float = 30.0
    min_samples: int = 3
    _samples: deque[PriceSample] = field(default_factory=deque)

    def add_sample(self, *, mid_price: float, timestamp: float) -> None:
        self._samples.append(PriceSample(timestamp=timestamp, mid_price=float(mid_price)))
        self._evict(timestamp)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

    def signal(self, *, now: float) -> MomentumSignal:
        self._evict(now)
        if len(self._samples) < self.min_samples:
            latest_mid = self._samples[-1].mid_price if self._samples else 0.0
            return MomentumSignal(
                direction="insufficient_data",
                mid_price=latest_mid,
                oldest_mid_price=None,
                price_change_pct=0.0,
                samples=len(self._samples),
            )

        oldest = self._samples[0]
        newest = self._samples[-1]
        pct = 0.0 if oldest.mid_price == 0 else ((newest.mid_price - oldest.mid_price) / oldest.mid_price) * 100.0
        if pct > 0:
            direction: Literal["up", "down", "flat", "insufficient_data"] = "up"
        elif pct < 0:
            direction = "down"
        else:
            direction = "flat"
        return MomentumSignal(
            direction=direction,
            mid_price=newest.mid_price,
            oldest_mid_price=oldest.mid_price,
            price_change_pct=round(pct, 6),
            samples=len(self._samples),
        )


def entry_grid_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    return tuple(
        PolymarketCrypto5mStrategyPreset(
            name=f"entry_{int(price * 100):02d}",
            rationale=f"Baseline {int(price * 100)}c entry for grid comparison.",
            entry_price=price,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=5.0,
        )
        for price in (0.89, 0.90, 0.91, 0.92, 0.93, 0.94, 0.95)
    )


def smoke_test_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    return (
        PolymarketCrypto5mStrategyPreset(
            name="smoke_entry",
            rationale="Smoke test preset: enter on the first valid live quote.",
            entry_price=0.01,
            exit_price=0.99,
            stop_loss_price=None,
            min_seconds_before_close=15.0,
            max_spread=1.0,
            min_threshold_seconds=0.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=0.0,
        ),
    )


def first_wave_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    return (
        PolymarketCrypto5mStrategyPreset(
            name="entry_95",
            rationale="Baseline 95c entry with shared gates.",
            entry_price=0.95,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=5.0,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="entry_90",
            rationale="Baseline 90c entry with shared gates.",
            entry_price=0.90,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=5.0,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="microprice_95",
            rationale="Require imbalance-weighted microprice support near 95c.",
            entry_price=0.95,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=0.0,
            mode="microprice",
            min_total_top_size=10.0,
            microprice_epsilon=0.002,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="support_ratio_95",
            rationale="Require bid-side size dominance near 95c.",
            entry_price=0.95,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.94,
            min_best_bid_size=0.0,
            mode="support_ratio",
            min_total_top_size=10.0,
            support_ratio=1.5,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="stable_quotes_95",
            rationale="Require stable quotes for 2 seconds before 95c entry.",
            entry_price=0.95,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=0.0,
            mode="quote_stability",
            stability_seconds=2.0,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="late_half_95",
            rationale="Only enter after 150 seconds into the round.",
            entry_price=0.95,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=5.0,
            min_seconds_after_open=150.0,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="flow_bullish_90",
            rationale="Require distinctly bid-heavy rolling flow at 90c.",
            entry_price=0.90,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=5.0,
            mode="flow_imbalance",
            flow_window_seconds=10.0,
            flow_min_samples=3,
            flow_min_imbalance=0.2,
        ),
    )


def advanced_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    return (
        PolymarketCrypto5mStrategyPreset(
            name="microprice_support_90",
            rationale="Require both microprice support and bid/ask size dominance at 90c.",
            entry_price=0.90,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.89,
            min_best_bid_size=0.0,
            mode="microprice_support",
            min_total_top_size=10.0,
            microprice_epsilon=0.002,
            support_ratio=1.5,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="spread_switch_90",
            rationale="Use a tighter entry threshold when the spread is already tight.",
            entry_price=0.90,
            entry_price_tight=0.95,
            spread_tight=0.01,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=5.0,
            mode="spread_switch",
        ),
        PolymarketCrypto5mStrategyPreset(
            name="momentum_95",
            rationale="Require supportive reference momentum before entering at 95c.",
            entry_price=0.95,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=5.0,
            mode="binance_momentum",
            momentum_window_seconds=30.0,
            momentum_min_samples=3,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="microprice_momentum_90",
            rationale="Combine microprice and reference momentum at 90c.",
            entry_price=0.90,
            exit_price=0.99,
            stop_loss_price=0.50,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=0.0,
            mode="microprice_momentum",
            min_total_top_size=10.0,
            microprice_epsilon=0.002,
            momentum_window_seconds=30.0,
            momentum_min_samples=3,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="adaptive_10pct_90",
            rationale="Adaptive stop at 10 percent below entry.",
            entry_price=0.90,
            exit_price=0.99,
            stop_loss_price=None,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=5.0,
            mode="adaptive_stop",
            max_drawdown_frac=0.10,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="trailing_10pct_90",
            rationale="Trailing stop at 10 percent below max bid seen.",
            entry_price=0.90,
            exit_price=0.99,
            stop_loss_price=None,
            min_seconds_before_close=15.0,
            max_spread=0.02,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.0,
            min_best_bid_size=5.0,
            mode="trailing_stop",
            trail_frac=0.10,
        ),
    )


def ninety_microstructure_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    return (
        PolymarketCrypto5mStrategyPreset(
            name="ninety_microprice_support",
            rationale="90c entry gated by microprice support, depth balance, and a fixed stop loss.",
            entry_price=0.90,
            max_entry_price=0.94,
            exit_price=0.98,
            stop_loss_price=0.84,
            min_seconds_before_close=20.0,
            max_spread=0.012,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.89,
            min_best_bid_size=0.0,
            mode="microprice_support",
            min_seconds_after_open=0.0,
            min_total_top_size=20.0,
            microprice_epsilon=0.0015,
            support_ratio=1.4,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="ninety_flow_imbalance",
            rationale="90c entry only when recent order-flow imbalance is bid-heavy enough to justify the risk.",
            entry_price=0.90,
            max_entry_price=0.95,
            exit_price=0.98,
            stop_loss_price=0.84,
            min_seconds_before_close=20.0,
            max_spread=0.012,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.89,
            min_best_bid_size=10.0,
            mode="flow_imbalance",
            min_seconds_after_open=0.0,
            flow_window_seconds=15.0,
            flow_min_samples=4,
            flow_min_imbalance=0.25,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="ninety_trend_confirmed",
            rationale="Late 90c entry that requires trend confirmation, tight spread, and a fixed stop loss.",
            entry_price=0.90,
            max_entry_price=0.95,
            exit_price=0.98,
            stop_loss_price=0.85,
            min_seconds_before_close=20.0,
            max_spread=0.01,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.89,
            min_best_bid_size=10.0,
            mode="binance_momentum",
            min_seconds_after_open=120.0,
            momentum_window_seconds=30.0,
            momentum_min_samples=3,
        ),
    )


def profitability_candidate_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    return (
        PolymarketCrypto5mStrategyPreset(
            name="edge_pullback_70",
            rationale="Enter only in the 70-78c band to require enough upside after crypto taker fees.",
            entry_price=0.70,
            max_entry_price=0.78,
            exit_price=0.96,
            stop_loss_price=0.62,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.68,
            min_best_bid_size=10.0,
            min_seconds_after_open=90.0,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="edge_pullback_80",
            rationale="Enter only in the 80-86c band with a tighter stop and enough room to exit before 99c.",
            entry_price=0.80,
            max_entry_price=0.86,
            exit_price=0.97,
            stop_loss_price=0.72,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.78,
            min_best_bid_size=10.0,
            min_seconds_after_open=90.0,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="edge_pullback_85",
            rationale="Enter only in the 85-90c band after early round noise, targeting a wider move than 95c scalps.",
            entry_price=0.85,
            max_entry_price=0.90,
            exit_price=0.98,
            stop_loss_price=0.77,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.83,
            min_best_bid_size=10.0,
            min_seconds_after_open=100.0,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="edge_support_80",
            rationale="Require bid-side depth dominance in the 80-86c band before entering.",
            entry_price=0.80,
            max_entry_price=0.86,
            exit_price=0.97,
            stop_loss_price=0.72,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.78,
            min_best_bid_size=0.0,
            mode="support_ratio",
            min_seconds_after_open=90.0,
            min_total_top_size=20.0,
            support_ratio=1.8,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="edge_microprice_85",
            rationale="Require microprice and depth support in the 85-90c band.",
            entry_price=0.85,
            max_entry_price=0.90,
            exit_price=0.98,
            stop_loss_price=0.77,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.83,
            min_best_bid_size=0.0,
            mode="microprice_support",
            min_seconds_after_open=100.0,
            min_total_top_size=20.0,
            microprice_epsilon=0.001,
            support_ratio=1.6,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="edge_flow_80",
            rationale="Require sustained bid-heavy flow in the 80-86c band.",
            entry_price=0.80,
            max_entry_price=0.86,
            exit_price=0.97,
            stop_loss_price=0.72,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.78,
            min_best_bid_size=10.0,
            mode="flow_imbalance",
            min_seconds_after_open=90.0,
            flow_window_seconds=15.0,
            flow_min_samples=4,
            flow_min_imbalance=0.3,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="edge_late_85",
            rationale="Late-round 85-91c entry that avoids early whipsaw and exits before expiry.",
            entry_price=0.85,
            max_entry_price=0.91,
            exit_price=0.98,
            stop_loss_price=0.78,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.83,
            min_best_bid_size=10.0,
            min_seconds_after_open=180.0,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="edge_trailing_75",
            rationale="Enter 75-82c only, then trail 8 percent below the best bid seen.",
            entry_price=0.75,
            max_entry_price=0.82,
            exit_price=0.97,
            stop_loss_price=None,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.73,
            min_best_bid_size=10.0,
            mode="trailing_stop",
            min_seconds_after_open=90.0,
            trail_frac=0.08,
        ),
    )


def live_edge_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    return (
        PolymarketCrypto5mStrategyPreset(
            name="edge_pullback_70_tight",
            rationale="Live edge test: enter 70-74c only after deeper confirmation.",
            entry_price=0.70,
            max_entry_price=0.74,
            exit_price=0.96,
            stop_loss_price=0.66,
            min_seconds_before_close=20.0,
            max_spread=0.01,
            min_threshold_seconds=3.0,
            min_supported_bid_price=0.69,
            min_best_bid_size=15.0,
            min_seconds_after_open=120.0,
        ),
        PolymarketCrypto5mStrategyPreset(
            name="edge_pullback_75_tight",
            rationale="Live edge test: enter 75-79c only with tight spread and higher bid support.",
            entry_price=0.75,
            max_entry_price=0.79,
            exit_price=0.97,
            stop_loss_price=0.70,
            min_seconds_before_close=20.0,
            max_spread=0.01,
            min_threshold_seconds=3.0,
            min_supported_bid_price=0.74,
            min_best_bid_size=15.0,
            min_seconds_after_open=120.0,
        ),
    )


def creative_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    """Novel strategy ideas — added 2026-04-17.

    Eight strategies exploring untested regions of the entry-price / signal-mode space.
    """
    return (
        # 1. Deep value entry at 65-70c.
        #    Breakeven at ~37% win rate — huge margin vs the 87%+ needed at 95c.
        #    Requires strong bid support ($15, bid ≥ 0.63) and 120s patience.
        PolymarketCrypto5mStrategyPreset(
            name="deep_value_65",
            rationale="Enter the 65-70c band for maximum upside; low breakeven WR of ~37%.",
            entry_price=0.65,
            max_entry_price=0.70,
            exit_price=0.93,
            stop_loss_price=0.54,
            min_seconds_before_close=20.0,
            max_spread=0.012,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.63,
            min_best_bid_size=15.0,
            min_seconds_after_open=120.0,
        ),
        # 2. Flow imbalance in the 75-82c band.
        #    Tests whether the bid/ask flow signal generalises below 90c.
        #    Requires 15s of bid-heavy flow (imbalance ≥ 0.20) before entry.
        PolymarketCrypto5mStrategyPreset(
            name="flow_reversal_75",
            rationale="Buy 75-82c only when sustained order-flow shows bid-side dominance.",
            entry_price=0.75,
            max_entry_price=0.82,
            exit_price=0.97,
            stop_loss_price=0.67,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.73,
            min_best_bid_size=10.0,
            mode="flow_imbalance",
            min_seconds_after_open=90.0,
            flow_window_seconds=15.0,
            flow_min_samples=4,
            flow_min_imbalance=0.20,
        ),
        # 3. Microprice signal at 75-82c.
        #    Currently microprice is only used at 85c+. Tests whether the
        #    weighted-mid signal adds value at lower prices too.
        PolymarketCrypto5mStrategyPreset(
            name="microprice_mid_75",
            rationale="Microprice support applied to the 75-82c band — lower than current usage.",
            entry_price=0.75,
            max_entry_price=0.82,
            exit_price=0.97,
            stop_loss_price=0.67,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.73,
            min_best_bid_size=0.0,
            mode="microprice",
            min_seconds_after_open=90.0,
            min_total_top_size=15.0,
            microprice_epsilon=0.002,
        ),
        # 4. Patience filter at 90c — wait 3 full minutes before firing.
        #    A market still sitting at 90c+ with 2 minutes to close has survived
        #    early mean-reversion pressure. Expected win rate boost through timing.
        PolymarketCrypto5mStrategyPreset(
            name="patience_long_90",
            rationale="Wait 180s into the round; a 90c market at T+3min is highly committed.",
            entry_price=0.90,
            max_entry_price=0.96,
            exit_price=0.99,
            stop_loss_price=0.86,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.89,
            min_best_bid_size=5.0,
            min_seconds_after_open=180.0,
        ),
        # 5. Quote stability gate at 85-91c.
        #    Both bid AND ask must hold still for 5 seconds before entry.
        #    A frozen quote at 85c means sellers are not pressing down — likely
        #    to resolve upward rather than reverse.
        PolymarketCrypto5mStrategyPreset(
            name="stability_85",
            rationale="Enter 85-91c only after 5s of frozen bid/ask — signals seller exhaustion.",
            entry_price=0.85,
            max_entry_price=0.91,
            exit_price=0.98,
            stop_loss_price=0.78,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.83,
            min_best_bid_size=5.0,
            mode="quote_stability",
            min_seconds_after_open=90.0,
            stability_seconds=5.0,
        ),
        # 6. Spread-switch sniper at 70-78c.
        #    Ultra-tight spread (≤ 0.006) → accept any 70c+ entry.
        #    Wider spread → floor rises to 0.74. Very tight spread at 70c
        #    means strong two-sided commitment — market isn't going lower.
        PolymarketCrypto5mStrategyPreset(
            name="spread_sniper_70",
            rationale="At 70c, ultra-tight spread signals high conviction; relax floor to 0.70.",
            entry_price=0.74,
            entry_price_tight=0.70,
            spread_tight=0.006,
            max_entry_price=0.78,
            exit_price=0.96,
            stop_loss_price=0.62,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.68,
            min_best_bid_size=10.0,
            mode="spread_switch",
            min_seconds_after_open=90.0,
        ),
        # 7. Bid-dominance at 70-78c with a high support ratio.
        #    Requires bid_size ≥ 2.0 × ask_size AND total book ≥ $20.
        #    Aggressive buyers at 70c likely to push the market toward resolution.
        PolymarketCrypto5mStrategyPreset(
            name="support_deep_70",
            rationale="Strong bid dominance (2x) in the 70-78c band signals accumulation.",
            entry_price=0.70,
            max_entry_price=0.78,
            exit_price=0.96,
            stop_loss_price=0.62,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=2.0,
            min_supported_bid_price=0.68,
            min_best_bid_size=0.0,
            mode="support_ratio",
            min_seconds_after_open=90.0,
            min_total_top_size=20.0,
            support_ratio=2.0,
        ),
        # 8. Late momentum entry at 88-93c after 150s.
        #    Combines timing patience with reference-price momentum check.
        #    If BTC mid-price is still trending upward at 2.5 min in, and the
        #    market is sitting at 88c, that's a convergent signal to buy.
        PolymarketCrypto5mStrategyPreset(
            name="late_momentum_88",
            rationale="88-93c entry after 150s with upward BTC momentum — two converging signals.",
            entry_price=0.88,
            max_entry_price=0.93,
            exit_price=0.98,
            stop_loss_price=0.82,
            min_seconds_before_close=20.0,
            max_spread=0.015,
            min_threshold_seconds=1.0,
            min_supported_bid_price=0.86,
            min_best_bid_size=5.0,
            mode="binance_momentum",
            min_seconds_after_open=150.0,
            momentum_window_seconds=30.0,
            momentum_min_samples=3,
        ),
    )


def ninety_only_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    """Single-preset set for focused ninety_microprice_support edge research."""
    return tuple(
        preset
        for preset in ninety_microstructure_strategy_presets()
        if preset.name == "ninety_microprice_support"
    )


def all_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    return (
        *entry_grid_strategy_presets(),
        *first_wave_strategy_presets(),
        *advanced_strategy_presets(),
    )


def research_strategy_presets() -> tuple[PolymarketCrypto5mStrategyPreset, ...]:
    presets = (
        *all_strategy_presets(),
        *ninety_microstructure_strategy_presets(),
        *profitability_candidate_strategy_presets(),
        *live_edge_strategy_presets(),
        *creative_strategy_presets(),
    )
    unique: dict[str, PolymarketCrypto5mStrategyPreset] = {}
    for preset in presets:
        unique.setdefault(preset.name, preset)
    return tuple(unique.values())


def effective_stop_loss_price(
    *,
    preset: PolymarketCrypto5mStrategyPreset,
    entry_price: float,
    max_bid_seen: float,
) -> float | None:
    if preset.mode == "adaptive_stop" and preset.max_drawdown_frac > 0:
        return round(float(entry_price) * (1.0 - preset.max_drawdown_frac), 3)
    if preset.mode == "trailing_stop" and preset.trail_frac > 0:
        return round(float(max_bid_seen) * (1.0 - preset.trail_frac), 3)
    if preset.stop_loss_price is not None:
        return float(preset.stop_loss_price)
    return None


class PolymarketCrypto5mSignalEngine:
    def __init__(
        self,
        *,
        preset: PolymarketCrypto5mStrategyPreset,
        token_sides: dict[str, Literal["up", "down"]],
    ) -> None:
        self.preset = preset
        self._states = {
            token_id: TokenSignalState(token_id=token_id, side=side)
            for token_id, side in token_sides.items()
        }
        self._flow_trackers = (
            {
                token_id: FlowImbalanceTracker(
                    window_seconds=preset.flow_window_seconds,
                    min_samples=preset.flow_min_samples,
                )
                for token_id in token_sides
            }
            if preset.mode == "flow_imbalance"
            else {}
        )
        self._momentum_tracker = (
            MomentumTracker(
                window_seconds=preset.momentum_window_seconds,
                min_samples=preset.momentum_min_samples,
            )
            if preset.mode in ("binance_momentum", "microprice_momentum")
            else None
        )

    def record_top_of_book(
        self,
        *,
        token_id: str,
        best_bid: float | None,
        best_ask: float | None,
        best_bid_size: float | None,
        best_ask_size: float | None,
        now: datetime,
    ) -> None:
        state = self._states[token_id]
        state.best_bid = None if best_bid is None else float(best_bid)
        state.best_ask = None if best_ask is None else float(best_ask)
        state.best_bid_size = None if best_bid_size is None else float(best_bid_size)
        state.best_ask_size = None if best_ask_size is None else float(best_ask_size)

        in_threshold_zone = (
            state.best_ask is not None
            and state.best_ask >= self._entry_floor_price(state)
            and state.best_ask < self.preset.exit_price
            and state.best_ask < 1.0
        )
        if in_threshold_zone:
            state.threshold_since = state.threshold_since or now
        else:
            state.threshold_since = None

        if state.stable_since is None:
            state.stable_since = now
        if state.best_bid is not None and state.last_bid is not None and state.best_bid < state.last_bid:
            state.stable_since = now
        if state.best_ask is not None and state.last_ask is not None and state.best_ask > state.last_ask:
            state.stable_since = now
        state.last_bid = state.best_bid
        state.last_ask = state.best_ask

        tracker = self._flow_trackers.get(token_id)
        if tracker is not None and state.best_bid_size is not None and state.best_ask_size is not None:
            tracker.add_sample(
                bid_size=state.best_bid_size,
                ask_size=state.best_ask_size,
                timestamp=now.timestamp(),
            )

    def record_reference_mid_price(self, *, mid_price: float, now: datetime) -> None:
        if self._momentum_tracker is None:
            return
        self._momentum_tracker.add_sample(mid_price=mid_price, timestamp=now.timestamp())

    def entry_signal(
        self,
        *,
        now: datetime,
        market_end: datetime,
        side: Literal["up", "down", "both"] = "both",
    ) -> EntrySignal | None:
        candidates: list[EntrySignal] = []
        round_start = market_end - timedelta(minutes=5)

        for token_id, state in self._states.items():
            if side != "both" and state.side != side:
                continue
            if not self._passes_shared_gates(state=state, now=now, market_end=market_end, round_start=round_start):
                continue
            if not self._passes_mode_gate(token_id=token_id, state=state, now=now):
                continue
            candidates.append(
                EntrySignal(
                    token_id=token_id,
                    side=state.side,
                    ask_price=float(state.best_ask),
                ),
            )

        if not candidates:
            return None
        return min(candidates, key=lambda candidate: (candidate.ask_price, candidate.token_id))

    def _passes_shared_gates(
        self,
        *,
        state: TokenSignalState,
        now: datetime,
        market_end: datetime,
        round_start: datetime,
    ) -> bool:
        ask = state.best_ask
        bid = state.best_bid
        if ask is None:
            return False
        entry_floor = self._entry_floor_price(state)
        max_entry_price = self.preset.max_entry_price or self.preset.exit_price
        if ask < entry_floor or ask > max_entry_price or ask >= self.preset.exit_price or ask >= 1.0:
            return False
        if (now - round_start).total_seconds() < self.preset.min_seconds_after_open:
            return False
        if (market_end - now).total_seconds() <= self.preset.min_seconds_before_close:
            return False
        if bid is None:
            return False
        if (ask - bid) > self.preset.max_spread:
            return False
        if self.preset.min_supported_bid_price > 0 and bid < self.preset.min_supported_bid_price:
            return False
        if self.preset.min_best_bid_size > 0:
            if state.best_bid_size is None or state.best_bid_size < self.preset.min_best_bid_size:
                return False
        if state.threshold_since is None:
            return False
        if (now - state.threshold_since).total_seconds() < self.preset.min_threshold_seconds:
            return False
        return True

    def _passes_mode_gate(
        self,
        *,
        token_id: str,
        state: TokenSignalState,
        now: datetime,
    ) -> bool:
        match self.preset.mode:
            case "basic":
                return True
            case "microprice":
                return self._microprice_supports_entry(state)
            case "support_ratio":
                return self._support_ratio_supports_entry(state)
            case "quote_stability":
                return state.stable_since is not None and (
                    now - state.stable_since
                ).total_seconds() >= self.preset.stability_seconds
            case "microprice_support":
                return self._microprice_supports_entry(state) and self._support_ratio_supports_entry(state)
            case "flow_imbalance":
                tracker = self._flow_trackers.get(token_id)
                if tracker is None:
                    return False
                signal = tracker.signal(now=now.timestamp())
                if not signal.sufficient:
                    return False
                if state.side == "up":
                    return signal.imbalance >= self.preset.flow_min_imbalance
                return signal.imbalance <= -self.preset.flow_min_imbalance
            case "binance_momentum":
                return self._momentum_supports_entry(state=state, now=now)
            case "microprice_momentum":
                return self._microprice_supports_entry(state) and self._momentum_supports_entry(
                    state=state,
                    now=now,
                )
            case "spread_switch" | "adaptive_stop" | "trailing_stop":
                return True
        return False

    def _entry_floor_price(self, state: TokenSignalState) -> float:
        if self.preset.mode != "spread_switch":
            return self.preset.entry_price
        if state.best_bid is not None and state.best_ask is not None:
            spread = state.best_ask - state.best_bid
            if spread <= self.preset.spread_tight and self.preset.entry_price_tight > 0:
                return self.preset.entry_price_tight
        return self.preset.entry_price

    def _microprice_supports_entry(self, state: TokenSignalState) -> bool:
        if (
            state.best_bid is None
            or state.best_ask is None
            or state.best_bid_size is None
            or state.best_ask_size is None
        ):
            return False
        total = state.best_bid_size + state.best_ask_size
        if total < self.preset.min_total_top_size:
            return False
        midpoint = (state.best_bid + state.best_ask) / 2.0
        imbalance = (state.best_bid_size - state.best_ask_size) / total
        microprice = midpoint + 0.5 * imbalance * (state.best_ask - state.best_bid)
        return microprice >= state.best_ask - self.preset.microprice_epsilon

    def _support_ratio_supports_entry(self, state: TokenSignalState) -> bool:
        if state.best_bid_size is None or state.best_ask_size is None:
            return False
        total = state.best_bid_size + state.best_ask_size
        if total < self.preset.min_total_top_size:
            return False
        denom = max(1e-9, state.best_ask_size)
        return (state.best_bid_size / denom) >= self.preset.support_ratio

    def _momentum_supports_entry(self, *, state: TokenSignalState, now: datetime) -> bool:
        if self._momentum_tracker is None:
            return False
        signal = self._momentum_tracker.signal(now=now.timestamp())
        if signal.direction == "insufficient_data":
            return False
        if state.side == "up":
            return signal.direction == "up"
        return signal.direction == "down"
