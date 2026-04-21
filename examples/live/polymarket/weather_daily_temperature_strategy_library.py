"""
Price-arena strategy library for Polymarket daily-temperature weather markets.

Defines which price arenas to trade and pure entry decision logic.
Time-based gates (e.g. 30-minute close buffer) are handled by the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WeatherTemperatureStrategyPreset:
    name: str
    arena: str
    min_ask: float
    max_ask: float
    max_spread: float = 0.03
    min_ask_size: float = 5.0
    order_qty: float = 10.0
    mode: str = "basic"
    token_side: str = "yes"  # "yes" or "no"
    take_profit_price: float | None = None
    stop_loss_price: float | None = None


def daily_temperature_price_arena_presets() -> tuple[WeatherTemperatureStrategyPreset, ...]:
    """Return the canonical set of daily-temperature price-arena presets."""
    return (
        # --- band-only arenas ---
        WeatherTemperatureStrategyPreset(
            name="temp_50c_band_only",
            arena="temp_50c",
            min_ask=0.50,
            max_ask=0.60,
            mode="band_only",
        ),
        WeatherTemperatureStrategyPreset(
            name="temp_60c_band_only",
            arena="temp_60c",
            min_ask=0.60,
            max_ask=0.70,
            mode="band_only",
        ),
        WeatherTemperatureStrategyPreset(
            name="temp_70c_band_only",
            arena="temp_70c",
            min_ask=0.70,
            max_ask=0.80,
            mode="band_only",
        ),
        WeatherTemperatureStrategyPreset(
            name="temp_80c_band_only",
            arena="temp_80c",
            min_ask=0.80,
            max_ask=0.90,
            mode="band_only",
        ),
        WeatherTemperatureStrategyPreset(
            name="temp_90c_band_only",
            arena="temp_90c",
            min_ask=0.90,
            max_ask=0.981,
            mode="band_only",
        ),
        # --- basic arenas ---
        WeatherTemperatureStrategyPreset(
            name="temp_50c_basic",
            arena="temp_50c",
            min_ask=0.50,
            max_ask=0.60,
        ),
        WeatherTemperatureStrategyPreset(
            name="temp_60c_basic",
            arena="temp_60c",
            min_ask=0.60,
            max_ask=0.70,
        ),
        WeatherTemperatureStrategyPreset(
            name="temp_70c_basic",
            arena="temp_70c",
            min_ask=0.70,
            max_ask=0.80,
        ),
        WeatherTemperatureStrategyPreset(
            name="temp_80c_basic",
            arena="temp_80c",
            min_ask=0.80,
            max_ask=0.90,
        ),
        WeatherTemperatureStrategyPreset(
            name="temp_90c_basic",
            arena="temp_90c",
            min_ask=0.90,
            max_ask=0.98,
            take_profit_price=0.99,
            stop_loss_price=0.75,
        ),
        # --- NO-side arena: buy NO token when market is near-certain NO ---
        WeatherTemperatureStrategyPreset(
            name="temp_90c_no_basic",
            arena="temp_90c_no",
            min_ask=0.90,
            max_ask=0.98,
            token_side="no",
            take_profit_price=0.99,
            stop_loss_price=0.75,
        ),
        # --- support arenas (basic + bid-side liquidity dominance) ---
        WeatherTemperatureStrategyPreset(
            name="temp_70c_support",
            arena="temp_70c",
            min_ask=0.70,
            max_ask=0.80,
            mode="support",
        ),
        WeatherTemperatureStrategyPreset(
            name="temp_80c_support",
            arena="temp_80c",
            min_ask=0.80,
            max_ask=0.90,
            mode="support",
        ),
        WeatherTemperatureStrategyPreset(
            name="temp_90c_support",
            arena="temp_90c",
            min_ask=0.90,
            max_ask=0.981,
            mode="support",
        ),
    )


def should_enter_temperature_market(
    *,
    preset: WeatherTemperatureStrategyPreset,
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
) -> bool:
    """
    Pure entry decision function.

    For "band_only" mode:
      - ask must be >= preset.min_ask and < preset.max_ask
      - ignores spread and liquidity gates for measurement-only baseline runs

    For "basic" mode:
      - ask must be >= preset.min_ask and < preset.max_ask
        (for 90c arena, max_ask is 0.981 so ask <= 0.98 is captured)
      - spread (ask - bid) must be <= preset.max_spread
      - ask_size must be >= preset.min_ask_size

    For "support" mode:
      - All basic rules plus bid_size > ask_size (bid-side liquidity dominance)
    """
    # --- basic gates ---
    if ask < preset.min_ask:
        return False
    if ask >= preset.max_ask:
        return False

    # Measurement baseline: only the price band matters.
    if preset.mode == "band_only":
        return True

    if (ask - bid) > preset.max_spread:
        return False
    if ask_size < preset.min_ask_size:
        return False

    # --- support mode additional gate ---
    if preset.mode == "support":
        if bid_size <= ask_size:
            return False

    return True
