"""
Price-arena strategy library for Polymarket sports markets (moneyline, spreads, totals).

Defines which price arenas to trade and pure entry decision logic.
Time-based gates (e.g. game-time proximity) are handled by the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SportsStrategyPreset:
    name: str
    arena: str
    min_ask: float
    max_ask: float
    max_spread: float = 0.02       # sports markets are liquid; tight spread required
    min_ask_size: float = 50.0     # require decent liquidity
    order_qty: float = 10.0
    mode: str = "band_only"        # "band_only" or "basic"
    allowed_sports: frozenset[str] | None = None        # None = all sports
    allowed_market_types: frozenset[str] | None = None  # None = all market types


def band_only_presets() -> tuple[SportsStrategyPreset, ...]:
    """Return band-only presets: one per arena, NO spread/liquidity filters."""
    return (
        SportsStrategyPreset(
            name="sports_50c_band_only",
            arena="sports_50c",
            min_ask=0.50,
            max_ask=0.60,
            mode="band_only",
        ),
        SportsStrategyPreset(
            name="sports_60c_band_only",
            arena="sports_60c",
            min_ask=0.60,
            max_ask=0.70,
            mode="band_only",
        ),
        SportsStrategyPreset(
            name="sports_70c_band_only",
            arena="sports_70c",
            min_ask=0.70,
            max_ask=0.80,
            mode="band_only",
        ),
        SportsStrategyPreset(
            name="sports_80c_band_only",
            arena="sports_80c",
            min_ask=0.80,
            max_ask=0.90,
            mode="band_only",
        ),
        SportsStrategyPreset(
            name="sports_90c_band_only",
            arena="sports_90c",
            min_ask=0.90,
            max_ask=0.981,
            mode="band_only",
        ),
    )


def basic_presets() -> tuple[SportsStrategyPreset, ...]:
    """Return basic presets: same bands as band_only, but with spread + liquidity filters."""
    return (
        SportsStrategyPreset(
            name="sports_50c_basic",
            arena="sports_50c",
            min_ask=0.50,
            max_ask=0.60,
            mode="basic",
        ),
        SportsStrategyPreset(
            name="sports_60c_basic",
            arena="sports_60c",
            min_ask=0.60,
            max_ask=0.70,
            mode="basic",
        ),
        SportsStrategyPreset(
            name="sports_70c_basic",
            arena="sports_70c",
            min_ask=0.70,
            max_ask=0.80,
            mode="basic",
        ),
        SportsStrategyPreset(
            name="sports_80c_basic",
            arena="sports_80c",
            min_ask=0.80,
            max_ask=0.90,
            mode="basic",
        ),
        SportsStrategyPreset(
            name="sports_90c_basic",
            arena="sports_90c",
            min_ask=0.90,
            max_ask=0.981,
            mode="basic",
        ),
    )


def all_sports_presets() -> tuple[SportsStrategyPreset, ...]:
    """Return all sports presets: band_only + basic combined."""
    return band_only_presets() + basic_presets()


def should_enter_sports_market(
    *,
    preset: SportsStrategyPreset,
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
    sport: str = "",
    market_type: str = "",
) -> bool:
    """
    Pure entry decision function.

    Checks sport and market type whitelists first, then price band.

    For "band_only" mode:
      - ask must be >= preset.min_ask and < preset.max_ask
      - ignores spread and liquidity gates for measurement-only baseline runs

    For "basic" mode:
      - ask must be >= preset.min_ask and < preset.max_ask
      - spread (ask - bid) must be <= preset.max_spread
      - ask_size must be >= preset.min_ask_size

    Note: if ``sport`` or ``market_type`` is ``""`` (the default) and the
    corresponding whitelist is non-None, the gate will always block.
    Callers must supply the actual sport/market_type string.
    """
    # Sport whitelist
    if preset.allowed_sports is not None and sport not in preset.allowed_sports:
        return False
    # Market type whitelist
    if preset.allowed_market_types is not None and market_type not in preset.allowed_market_types:
        return False

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

    return True
