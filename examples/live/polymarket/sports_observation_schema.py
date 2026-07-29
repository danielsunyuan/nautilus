#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------
"""Versioned, JSON-serializable schema for sports forward observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = "sports_forward_observation.v1"


def _require_text(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_probability(name: str, value: float | None) -> None:
    if value is not None and not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class BookmakerQuote:
    """One raw bookmaker quote retained before consensus calculation."""

    bookmaker: str
    observed_at: str
    outcome_name: str
    decimal_odds: float

    def __post_init__(self) -> None:
        for name in ("bookmaker", "observed_at", "outcome_name"):
            _require_text(name, getattr(self, name))
        if self.decimal_odds <= 1.0:
            raise ValueError("decimal_odds must be greater than 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "bookmaker": self.bookmaker,
            "observed_at": self.observed_at,
            "outcome_name": self.outcome_name,
            "decimal_odds": self.decimal_odds,
        }


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    """Age of one source at the intended decision time."""

    source: str
    age_seconds: float | None

    def __post_init__(self) -> None:
        _require_text("source", self.source)
        if self.age_seconds is not None and self.age_seconds < 0:
            raise ValueError("age_seconds must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True, slots=True)
class SportsForwardObservation:
    """One append-only market observation at one predetermined checkpoint."""

    event_id: str
    condition_id: str
    token_id: str
    outcome_name: str
    sport: str
    competition: str
    start_time: str
    market_type: str
    resolution_source: str
    checkpoint: str
    intended_decision_time: str
    venue_timestamp: str
    best_bid: float | None
    bid_size: float | None
    best_ask: float | None
    ask_size: float | None
    walked_ask: float | None
    tick_size: float
    minimum_order_size: float
    fee_rate: float
    bookmaker_quotes: tuple[BookmakerQuote, ...]
    devig_consensus_probability: float | None
    source_freshness: tuple[SourceFreshness, ...]
    missing_data_reasons: tuple[str, ...]
    clob_winner_evidence: str | None
    named_source_outcome: str | None

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "condition_id",
            "token_id",
            "outcome_name",
            "sport",
            "competition",
            "start_time",
            "market_type",
            "resolution_source",
            "checkpoint",
            "intended_decision_time",
            "venue_timestamp",
        ):
            _require_text(name, getattr(self, name))

        for name in ("best_bid", "best_ask", "walked_ask"):
            _require_probability(name, getattr(self, name))
        _require_probability(
            "devig_consensus_probability",
            self.devig_consensus_probability,
        )

        for name in ("bid_size", "ask_size"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")
        if self.minimum_order_size <= 0:
            raise ValueError("minimum_order_size must be positive")
        if self.fee_rate < 0:
            raise ValueError("fee_rate must be non-negative")
        if (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid > self.best_ask
        ):
            raise ValueError("best_bid must not exceed best_ask")
        if (
            self.walked_ask is not None
            and self.best_ask is not None
            and self.walked_ask < self.best_ask
        ):
            raise ValueError("walked_ask must not be below best_ask")

        missing_market_data = any(
            value is None
            for value in (
                self.best_bid,
                self.bid_size,
                self.best_ask,
                self.ask_size,
                self.walked_ask,
            )
        )
        missing_bookmaker_data = (
            not self.bookmaker_quotes
            or self.devig_consensus_probability is None
        )
        if (missing_market_data or missing_bookmaker_data) and not self.missing_data_reasons:
            raise ValueError("missing_data_reasons required when observation data is missing")

        for reason in self.missing_data_reasons:
            _require_text("missing_data_reasons item", reason)
        if bool(self.clob_winner_evidence) != bool(self.named_source_outcome):
            raise ValueError(
                "clob_winner_evidence and named_source_outcome must be supplied together",
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a stable payload suitable for append-only JSONL storage."""
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": self.event_id,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "outcome_name": self.outcome_name,
            "sport": self.sport,
            "competition": self.competition,
            "start_time": self.start_time,
            "market_type": self.market_type,
            "resolution_source": self.resolution_source,
            "checkpoint": self.checkpoint,
            "intended_decision_time": self.intended_decision_time,
            "venue_timestamp": self.venue_timestamp,
            "best_bid": self.best_bid,
            "bid_size": self.bid_size,
            "best_ask": self.best_ask,
            "ask_size": self.ask_size,
            "walked_ask": self.walked_ask,
            "tick_size": self.tick_size,
            "minimum_order_size": self.minimum_order_size,
            "fee_rate": self.fee_rate,
            "bookmaker_quotes": [quote.to_dict() for quote in self.bookmaker_quotes],
            "devig_consensus_probability": self.devig_consensus_probability,
            "source_freshness": [
                freshness.to_dict()
                for freshness in self.source_freshness
            ],
            "missing_data_reasons": list(self.missing_data_reasons),
            "clob_winner_evidence": self.clob_winner_evidence,
            "named_source_outcome": self.named_source_outcome,
        }
