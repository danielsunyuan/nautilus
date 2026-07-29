from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

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
    "sports_forward_research",
    ROOT / "examples" / "live" / "polymarket" / "sports_forward_research.py",
)


def _observation(**overrides) -> dict:
    start = datetime(2026, 8, 1, 12, tzinfo=UTC)
    values = {
        "schema_version": "sports_forward_observation.v1",
        "collected_at": (start - timedelta(hours=1)).isoformat(),
        "event_id": "event-1",
        "condition_id": "condition-1",
        "token_id": "token-a",
        "outcome_name": "Player A",
        "sport": "atp",
        "competition": "ATP Washington",
        "start_time": start.isoformat(),
        "market_type": "moneyline",
        "resolution_source": "https://www.atptour.com/en/scores/current",
        "checkpoint": "t_minus_60m",
        "intended_decision_time": (start - timedelta(hours=1)).isoformat(),
        "checkpoint_lag_seconds": 1.0,
        "venue_timestamp": (start - timedelta(hours=1, seconds=1)).isoformat(),
        "best_bid": 0.59,
        "bid_size": 100.0,
        "best_ask": 0.60,
        "ask_size": 50.0,
        "walked_ask": 0.60,
        "research_share_quantity": 5.0,
        "tick_size": 0.01,
        "minimum_order_size": 5.0,
        "fee_rate": 0.05,
        "fee_exponent": 1.0,
        "fee_taker_only": True,
        "bookmaker_quotes": [
            {
                "bookmaker": bookmaker,
                "observed_at": (start - timedelta(hours=1, seconds=age)).isoformat(),
                "outcome_name": outcome,
                "decimal_odds": 1.5 if outcome == "Player A" else 3.0,
                "implied_probability": 2 / 3 if outcome == "Player A" else 1 / 3,
                "devig_probability": 0.70 if outcome == "Player A" else 0.30,
                "age_seconds": age,
            }
            for bookmaker, age in (("book-a", 10.0), ("book-b", 20.0), ("book-c", 30.0))
            for outcome in ("Player A", "Player B")
        ],
        "devig_consensus_probability": 0.70,
        "source_freshness": [
            {
                "source": "polymarket_clob",
                "observed_at": (start - timedelta(hours=1, seconds=1)).isoformat(),
                "age_seconds": 1.0,
            },
            *[
                {
                    "source": f"bookmaker:{bookmaker}",
                    "observed_at": (start - timedelta(hours=1, seconds=age)).isoformat(),
                    "age_seconds": age,
                }
                for bookmaker, age in (("book-a", 10.0), ("book-b", 20.0), ("book-c", 30.0))
            ],
        ],
        "missing_data_reasons": [],
        "is_complete": True,
    }
    values.update(overrides)
    return values


def _resolution(**overrides) -> dict:
    values = {
        "schema_version": "sports_forward_resolution.v1",
        "condition_id": "condition-1",
        "winning_token_id": "token-a",
        "winning_outcome": "Player A",
        "resolution_source": "https://clob.test/markets/condition-1",
        "resolution_observed_at": "2026-08-02T12:00:00+00:00",
        "named_source": "https://www.atptour.com/en/scores/current",
        "named_source_outcome": "Player A",
        "reconciliation_status": "matched",
    }
    values.update(overrides)
    return values


def test_entry_fee_uses_captured_curve_and_five_decimal_rounding():
    assert mod.calculate_entry_fee(
        shares=5.0,
        price=0.60,
        fee_rate=0.05,
        fee_exponent=1.0,
    ) == 0.06
    assert mod.calculate_entry_fee(
        shares=0.001,
        price=0.01,
        fee_rate=0.05,
        fee_exponent=1.0,
    ) == 0.0


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"is_complete": False}, "observation_incomplete"),
        ({"checkpoint_lag_seconds": 121.0}, "checkpoint_late"),
        ({"walked_ask": None}, "venue_data_invalid"),
        ({"devig_consensus_probability": None}, "bookmaker_consensus_invalid"),
        ({"bookmaker_quotes": []}, "bookmaker_count_below_3"),
        (
            {
                "source_freshness": [
                    {
                        "source": "polymarket_clob",
                        "observed_at": "2026-08-01T11:00:00+00:00",
                        "age_seconds": 181.0,
                    },
                ],
            },
            "source_stale",
        ),
    ],
)
def test_eligibility_fails_closed(overrides: dict, reason: str):
    assert reason in mod.observation_exclusion_reasons(_observation(**overrides))


def test_exact_winner_token_enrichment_ignores_outcome_label_drift():
    row = _observation(outcome_name="Persisted Label")
    enriched = mod.enrich_observation(
        row,
        _resolution(winning_outcome="Official Player A"),
    )

    assert enriched["resolved"] is True
    assert enriched["settlement_price"] == 1.0
    assert enriched["winning_token_id"] == "token-a"


def test_registered_candidate_is_deterministic_and_fee_complete():
    token_a = _observation()
    token_b = _observation(
        token_id="token-b",
        outcome_name="Player B",
        best_bid=0.38,
        best_ask=0.40,
        walked_ask=0.40,
        devig_consensus_probability=0.30,
    )
    enriched = [
        mod.enrich_observation(token_a, _resolution()),
        mod.enrich_observation(token_b, _resolution()),
    ]

    candidates = mod.select_registered_candidates(enriched)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["token_id"] == "token-a"
    assert candidate["entry_fee"] == 0.06
    assert candidate["gross_pnl"] == 2.0
    assert candidate["net_pnl"] == 1.94
    assert candidate["fee_adjusted_expected_edge"] == pytest.approx(0.088)


def test_resolution_record_requires_exact_authoritative_evidence():
    market_resolution = SimpleNamespace(
        condition_id="condition-1",
        resolved=True,
        winning_token_id="token-a",
        winning_outcome="Player A",
        resolution_source="https://clob.test/markets/condition-1",
        observed_at="2026-08-02T12:00:00+00:00",
    )

    result = mod.resolution_record_from_market_resolution(market_resolution)

    assert result == {
        "schema_version": "sports_forward_resolution.v1",
        "condition_id": "condition-1",
        "winning_token_id": "token-a",
        "winning_outcome": "Player A",
        "resolution_source": "https://clob.test/markets/condition-1",
        "resolution_observed_at": "2026-08-02T12:00:00+00:00",
        "named_source": None,
        "named_source_outcome": None,
        "reconciliation_status": "unavailable",
    }

    market_resolution.winning_token_id = None
    assert mod.resolution_record_from_market_resolution(market_resolution) is None


def test_evaluation_passes_only_when_every_registered_gate_passes():
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    observations = []
    resolutions = {}
    for condition_index in range(80):
        sport = "atp" if condition_index < 40 else "wta"
        collected_at = now - timedelta(days=31 - (condition_index % 28))
        condition_id = f"condition-{condition_index}"
        competition = f"{sport.upper()} Tournament {condition_index % 4}"
        winning_token = f"{condition_id}-a"
        resolutions[condition_id] = _resolution(
            condition_id=condition_id,
            winning_token_id=winning_token,
            resolution_source=f"https://clob.test/markets/{condition_id}",
            named_source_outcome="Player A",
        )
        for checkpoint in ("t_minus_60m", "t_minus_15m"):
            for side in ("a", "b"):
                token_id = f"{condition_id}-{side}"
                observations.append(
                    _observation(
                        event_id=f"event-{condition_index}",
                        condition_id=condition_id,
                        token_id=token_id,
                        outcome_name=f"Player {side.upper()}",
                        sport=sport,
                        competition=competition,
                        checkpoint=checkpoint,
                        collected_at=collected_at.isoformat(),
                        best_bid=0.59 if side == "a" else 0.38,
                        best_ask=0.60 if side == "a" else 0.40,
                        walked_ask=0.60 if side == "a" else 0.40,
                        devig_consensus_probability=0.70 if side == "a" else 0.30,
                    ),
                )

    report = mod.evaluate_forward_window(
        observations=observations,
        resolutions=resolutions,
        now=now,
    )

    assert report["counts"]["complete_rows"] == 320
    assert report["counts"]["resolved_complete_rows"] == 320
    assert report["counts"]["atp_resolved_complete_rows"] == 160
    assert report["counts"]["wta_resolved_complete_rows"] == 160
    assert report["counts"]["registered_entries"] == 160
    assert report["performance"]["net_pnl"] > 0
    assert report["gates"]["elapsed_30_days"] is True
    assert report["gates"]["resolved_300"] is True
    assert report["gates"]["atp_100"] is True
    assert report["gates"]["wta_100"] is True
    assert report["gates"]["both_checkpoints_positive"] is True
    assert report["gates"]["tournament_concentration"] is True
    assert report["gates"]["week_concentration"] is True
    assert report["gates"]["all_pass"] is True


def test_jsonl_loader_reads_plain_and_gzip_and_flags_duplicate_keys(tmp_path: Path):
    first = _observation()
    duplicate = _observation(collected_at="2026-08-01T11:00:01+00:00")
    plain = tmp_path / "sports_forward_2026-08-01.jsonl"
    plain.write_text(json.dumps(first) + "\n", encoding="utf-8")

    import gzip

    compressed = tmp_path / "sports_forward_2026-08-02.jsonl.gz"
    with gzip.open(compressed, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(duplicate) + "\n")

    rows = mod.load_observations([tmp_path])
    report = mod.evaluate_forward_window(
        observations=rows,
        resolutions={},
        now=datetime(2026, 8, 2, tzinfo=UTC),
    )

    assert len(rows) == 2
    assert report["exclusions"]["duplicate_observation_key"] == 2


def test_resolve_once_appends_one_exact_record_and_is_restart_safe(tmp_path: Path):
    resolution_path = tmp_path / "resolutions.jsonl"
    observations = [
        _observation(),
        _observation(token_id="token-b", outcome_name="Player B"),
    ]
    calls = []

    async def fetch_resolution(condition_id: str):
        calls.append(condition_id)
        return SimpleNamespace(
            condition_id=condition_id,
            resolved=True,
            winning_token_id="token-a",
            winning_outcome="Player A",
            resolution_source=f"https://clob.test/markets/{condition_id}",
            observed_at="2026-08-02T12:00:00+00:00",
        )

    first = asyncio.run(
        mod.resolve_once(
            observations=observations,
            resolution_path=resolution_path,
            clob_host="https://clob.test",
            fetch_resolution=fetch_resolution,
        ),
    )
    second = asyncio.run(
        mod.resolve_once(
            observations=observations,
            resolution_path=resolution_path,
            clob_host="https://clob.test",
            fetch_resolution=fetch_resolution,
        ),
    )

    assert first == 1
    assert second == 0
    assert calls == ["condition-1"]
    assert list(mod.load_resolution_records(resolution_path)) == ["condition-1"]
    assert len(resolution_path.read_text().splitlines()) == 1
