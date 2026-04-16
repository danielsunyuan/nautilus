from __future__ import annotations

from typing import Literal


PriceArena = Literal["temp_50c", "temp_60c", "temp_70c", "temp_80c", "temp_90c"]

TradeOutcome = Literal["win", "loss", "unresolved"]


def classify_price_arena(price: float) -> PriceArena | None:
    """Classify a price into a 10-cent temperature arena, or None if out of range."""
    value = float(price)
    if 0.50 <= value < 0.60:
        return "temp_50c"
    if 0.60 <= value < 0.70:
        return "temp_60c"
    if 0.70 <= value < 0.80:
        return "temp_70c"
    if 0.80 <= value < 0.90:
        return "temp_80c"
    if 0.90 <= value <= 0.98:
        return "temp_90c"
    return None


def classify_resolved_trade(
    *,
    resolved: bool,
    settlement_price: float | None,
    pnl: float | None,
) -> TradeOutcome:
    """Classify a resolved trade as win, loss, or unresolved."""
    if not resolved:
        return "unresolved"
    if settlement_price == 1.0 and pnl is not None and pnl > 0:
        return "win"
    return "loss"


def breakeven_win_rate(entry_price: float) -> float:
    """Return the minimum win rate needed to break even at this entry price."""
    return float(entry_price)  # breakeven = cost / 1.00 payout
