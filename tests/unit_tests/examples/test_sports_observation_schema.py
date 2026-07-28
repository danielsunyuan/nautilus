from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


mod = _load_module(
    "sports_observation_schema",
    ROOT / "examples" / "live" / "polymarket" / "sports_observation_schema.py",
)

BookmakerQuote = mod.BookmakerQuote
SourceFreshness = mod.SourceFreshness
SportsForwardObservation = mod.SportsForwardObservation


def _observation(**overrides):
    values = {
        "event_id": "event-123",
        "condition_id": "0xabc",
        "token_id": "token-456",
        "outcome_name": "New York Yankees",
        "sport": "mlb",
        "competition": "MLB",
        "start_time": "2026-07-29T23:00:00Z",
        "market_type": "moneyline",
        "resolution_source": "https://www.mlb.com/",
        "checkpoint": "t_minus_60m",
        "intended_decision_time": "2026-07-29T22:00:00Z",
        "venue_timestamp": "2026-07-29T22:00:01Z",
        "best_bid": 0.59,
        "bid_size": 80.0,
        "best_ask": 0.60,
        "ask_size": 45.0,
        "walked_ask": 0.61,
        "tick_size": 0.01,
        "minimum_order_size": 5.0,
        "fee_rate": 0.03,
        "bookmaker_quotes": (
            BookmakerQuote(
                bookmaker="book-a",
                observed_at="2026-07-29T21:59:50Z",
                outcome_name="New York Yankees",
                decimal_odds=1.70,
            ),
            BookmakerQuote(
                bookmaker="book-b",
                observed_at="2026-07-29T21:59:55Z",
                outcome_name="New York Yankees",
                decimal_odds=1.72,
            ),
        ),
        "devig_consensus_probability": 0.59,
        "source_freshness": (
            SourceFreshness(source="polymarket", age_seconds=0.0),
            SourceFreshness(source="book-a", age_seconds=11.0),
            SourceFreshness(source="book-b", age_seconds=6.0),
        ),
        "missing_data_reasons": (),
        "clob_winner_evidence": None,
        "named_source_outcome": None,
    }
    values.update(overrides)
    return SportsForwardObservation(**values)


def test_observation_serializes_all_forward_research_fields():
    payload = _observation().to_dict()

    assert payload["schema_version"] == "sports_forward_observation.v1"
    assert payload["event_id"] == "event-123"
    assert payload["condition_id"] == "0xabc"
    assert payload["token_id"] == "token-456"
    assert payload["checkpoint"] == "t_minus_60m"
    assert payload["best_bid"] == 0.59
    assert payload["ask_size"] == 45.0
    assert payload["fee_rate"] == 0.03
    assert payload["bookmaker_quotes"][0]["bookmaker"] == "book-a"
    assert payload["devig_consensus_probability"] == 0.59
    assert payload["source_freshness"][1]["age_seconds"] == 11.0
    assert payload["clob_winner_evidence"] is None
    assert payload["named_source_outcome"] is None
    json.dumps(payload)


def test_missing_bookmaker_consensus_requires_explicit_reason():
    with pytest.raises(ValueError, match="missing_data_reasons"):
        _observation(
            bookmaker_quotes=(),
            devig_consensus_probability=None,
            missing_data_reasons=(),
        )

    observation = _observation(
        bookmaker_quotes=(),
        devig_consensus_probability=None,
        missing_data_reasons=("bookmaker_quotes_unavailable",),
    )

    assert observation.to_dict()["missing_data_reasons"] == [
        "bookmaker_quotes_unavailable",
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", ""),
        ("condition_id", ""),
        ("token_id", ""),
        ("checkpoint", ""),
        ("best_bid", 1.01),
        ("best_ask", -0.01),
        ("tick_size", 0.0),
        ("minimum_order_size", 0.0),
        ("fee_rate", -0.01),
        ("devig_consensus_probability", 1.01),
    ],
)
def test_observation_rejects_invalid_required_values(field: str, value):
    with pytest.raises(ValueError):
        _observation(**{field: value})


def test_observation_can_record_eventual_resolution_evidence():
    payload = _observation(
        clob_winner_evidence="token token-456 settled at 1.0",
        named_source_outcome="New York Yankees",
    ).to_dict()

    assert payload["clob_winner_evidence"] == "token token-456 settled at 1.0"
    assert payload["named_source_outcome"] == "New York Yankees"
