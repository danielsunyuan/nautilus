from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
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
    checkpoint = str(overrides.get("checkpoint", "t_minus_60m"))
    checkpoint_offset = timedelta(
        minutes=60 if checkpoint == "t_minus_60m" else 15,
    )
    if "start_time" in overrides:
        start = datetime.fromisoformat(str(overrides["start_time"]))
    elif "collected_at" in overrides:
        collected_override = datetime.fromisoformat(str(overrides["collected_at"]))
        start = collected_override + checkpoint_offset - timedelta(seconds=1)
    else:
        start = datetime(2026, 8, 1, 12, tzinfo=UTC)
    intended = start - checkpoint_offset
    collected = (
        datetime.fromisoformat(str(overrides["collected_at"]))
        if "collected_at" in overrides
        else intended + timedelta(seconds=1)
    )
    venue_observed = collected - timedelta(seconds=1)
    values = {
        "schema_version": "sports_forward_observation.v1",
        "collected_at": collected.isoformat(),
        "event_id": "event-1",
        "condition_id": "condition-1",
        "token_id": "token-a",
        "outcome_name": "Player A",
        "sport": "atp",
        "competition": "ATP Washington",
        "start_time": start.isoformat(),
        "market_type": "moneyline",
        "resolution_source": "https://www.atptour.com/en/scores/current",
        "checkpoint": checkpoint,
        "intended_decision_time": intended.isoformat(),
        "checkpoint_lag_seconds": 1.0,
        "venue_timestamp": venue_observed.isoformat(),
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
                "observed_at": (collected - timedelta(seconds=age)).isoformat(),
                "outcome_name": outcome,
                "decimal_odds": 1.5 if outcome == "Player A" else 3.0,
                "implied_probability": 2 / 3 if outcome == "Player A" else 1 / 3,
                "devig_probability": 0.70 if outcome == "Player A" else 0.30,
            }
            for bookmaker, age in (("book-a", 10.0), ("book-b", 20.0), ("book-c", 30.0))
            for outcome in ("Player A", "Player B")
        ],
        "devig_consensus_probability": 0.70,
        "source_freshness": [
            {
                "source": "polymarket_clob",
                "observed_at": venue_observed.isoformat(),
                "age_seconds": 1.0,
            },
            *[
                {
                    "source": f"bookmaker:{bookmaker}",
                    "observed_at": (collected - timedelta(seconds=age)).isoformat(),
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
        ({"minimum_order_size": 10.0}, "minimum_order_size_exceeds_quantity"),
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


def test_eligibility_recomputes_checkpoint_lag_instead_of_trusting_field():
    row = _observation()
    row["collected_at"] = "2026-08-01T11:15:00+00:00"
    row["checkpoint_lag_seconds"] = 1.0

    reasons = mod.observation_exclusion_reasons(row)

    assert "checkpoint_lag_mismatch" in reasons
    assert "checkpoint_late" in reasons


def test_eligibility_validates_quote_values_and_matching_freshness():
    malformed_quote = _observation()
    malformed_quote["bookmaker_quotes"][0]["decimal_odds"] = 1.0
    mismatched_freshness = _observation()
    mismatched_freshness["source_freshness"] = [
        source
        for source in mismatched_freshness["source_freshness"]
        if source["source"] != "bookmaker:book-c"
    ]

    assert "bookmaker_quote_invalid" in mod.observation_exclusion_reasons(
        malformed_quote,
    )
    assert "bookmaker_freshness_mismatch" in mod.observation_exclusion_reasons(
        mismatched_freshness,
    )


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


def _passing_window(
    now: datetime,
    *,
    same_competition: bool = False,
) -> tuple[list[dict], dict[str, dict]]:
    observations = []
    resolutions = {}
    for condition_index in range(80):
        sport = "atp" if condition_index < 40 else "wta"
        collected_at = now - timedelta(days=31 - (condition_index % 28))
        condition_id = f"condition-{condition_index}"
        competition = (
            "One Tournament"
            if same_competition
            else f"{sport.upper()} Tournament {condition_index % 4}"
        )
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
    return observations, resolutions


def test_evaluation_passes_only_when_every_registered_gate_passes():
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    observations, resolutions = _passing_window(now)

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
    assert report["performance"]["by_sport_net_pnl"]["atp"] > 0
    assert report["performance"]["by_price_band_net_pnl"]["0.60-0.70"] > 0
    assert report["calibration"]["consensus_brier_score"] is not None
    assert report["calibration"]["clob_mid_brier_score"] is not None
    assert report["sensitivity"]["minimum_bookmakers"]["3"]["entries"] == 160
    assert report["sensitivity"]["maximum_quote_age_seconds"]["180"]["entries"] == 160
    assert report["gates"]["elapsed_30_days"] is True
    assert report["gates"]["resolved_300"] is True
    assert report["gates"]["atp_100"] is True
    assert report["gates"]["wta_100"] is True
    assert report["gates"]["both_checkpoints_positive"] is True
    assert report["gates"]["tournament_concentration"] is True
    assert report["gates"]["week_concentration"] is True
    assert report["gates"]["all_pass"] is True


def test_elapsed_gate_fails_before_exact_30_day_boundary():
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    observations, resolutions = _passing_window(now)
    first_complete = min(
        datetime.fromisoformat(row["collected_at"])
        for row in observations
    )

    before = mod.evaluate_forward_window(
        observations=observations,
        resolutions=resolutions,
        now=first_complete + timedelta(days=30) - timedelta(microseconds=1),
    )
    at_boundary = mod.evaluate_forward_window(
        observations=observations,
        resolutions=resolutions,
        now=first_complete + timedelta(days=30),
    )

    assert before["gates"]["elapsed_30_days"] is False
    assert at_boundary["gates"]["elapsed_30_days"] is True


def test_sample_and_subgroup_gates_fail_at_registered_shortfalls():
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    observations, resolutions = _passing_window(now)
    only_299 = observations[:299]
    atp_short = [
        row
        for row in observations
        if row["sport"] == "wta"
        or int(row["condition_id"].split("-")[1]) < 24
    ]
    missing_checkpoint = [
        row
        for row in observations
        if row["checkpoint"] != "t_minus_15m"
    ]

    assert mod.evaluate_forward_window(
        observations=only_299,
        resolutions=resolutions,
        now=now,
    )["gates"]["resolved_300"] is False
    assert mod.evaluate_forward_window(
        observations=atp_short,
        resolutions=resolutions,
        now=now,
    )["gates"]["atp_100"] is False
    assert mod.evaluate_forward_window(
        observations=missing_checkpoint,
        resolutions=resolutions,
        now=now,
    )["gates"]["both_checkpoints_positive"] is False


def test_concentration_gate_fails_when_one_tournament_owns_profit():
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    observations, resolutions = _passing_window(now, same_competition=True)

    report = mod.evaluate_forward_window(
        observations=observations,
        resolutions=resolutions,
        now=now,
    )

    assert report["gates"]["tournament_concentration"] is False
    assert report["gates"]["all_pass"] is False


def test_checkpoint_gate_fails_when_one_checkpoint_loses_money():
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    observations, resolutions = _passing_window(now)
    for row in observations:
        if row["checkpoint"] != "t_minus_15m":
            continue
        if row["token_id"].endswith("-a"):
            row["devig_consensus_probability"] = 0.60
        else:
            row.update(
                {
                    "best_bid": 0.59,
                    "best_ask": 0.60,
                    "walked_ask": 0.60,
                    "devig_consensus_probability": 0.70,
                },
            )

    report = mod.evaluate_forward_window(
        observations=observations,
        resolutions=resolutions,
        now=now,
    )

    assert report["performance"]["by_checkpoint_net_pnl"]["t_minus_15m"] < 0
    assert report["gates"]["both_checkpoints_positive"] is False


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


def test_jsonl_loader_rejects_missing_or_empty_inputs(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        mod.load_observations([tmp_path / "missing"])
    with pytest.raises(ValueError, match="no sports forward files"):
        mod.load_observations([tmp_path])


def test_resolution_loader_ignores_truncated_tail_and_retries_incomplete_record(
    tmp_path: Path,
):
    path = tmp_path / "resolutions.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": "sports_forward_resolution.v1",
                "condition_id": "incomplete",
            },
        )
        + "\n"
        + '{"schema_version":',
        encoding="utf-8",
    )

    assert mod.load_resolution_records(path) == {}


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


def test_resolve_once_appends_later_named_source_reconciliation(tmp_path: Path):
    resolution_path = tmp_path / "resolutions.jsonl"
    observations = [_observation()]

    async def fetch_resolution(condition_id: str):
        return SimpleNamespace(
            condition_id=condition_id,
            resolved=True,
            winning_token_id="token-a",
            winning_outcome="Player A",
            resolution_source=f"https://clob.test/markets/{condition_id}",
            observed_at="2026-08-02T12:00:00+00:00",
        )

    asyncio.run(
        mod.resolve_once(
            observations=observations,
            resolution_path=resolution_path,
            clob_host="https://clob.test",
            fetch_resolution=fetch_resolution,
        ),
    )
    written = asyncio.run(
        mod.resolve_once(
            observations=observations,
            resolution_path=resolution_path,
            clob_host="https://clob.test",
            named_results={
                "condition-1": {
                    "source": "https://www.atptour.com/en/scores/current",
                    "winning_outcome": "Player A",
                },
            },
            fetch_resolution=fetch_resolution,
        ),
    )
    repeated = asyncio.run(
        mod.resolve_once(
            observations=observations,
            resolution_path=resolution_path,
            clob_host="https://clob.test",
            named_results={
                "condition-1": {
                    "source": "https://www.atptour.com/en/scores/current",
                    "winning_outcome": "Player A",
                },
            },
            fetch_resolution=fetch_resolution,
        ),
    )

    latest = mod.load_resolution_records(resolution_path)["condition-1"]
    assert written == 1
    assert repeated == 0
    assert latest["reconciliation_status"] == "matched"
    assert latest["named_source_outcome"] == "Player A"
    assert len(resolution_path.read_text().splitlines()) == 2


def test_status_and_evaluate_cli_have_distinct_successful_outputs(tmp_path: Path):
    observation_path = tmp_path / "sports_forward_2026-08-01.jsonl"
    resolution_path = tmp_path / "resolutions.jsonl"
    observation_path.write_text(
        json.dumps(_observation()) + "\n",
        encoding="utf-8",
    )
    resolution_path.write_text(
        json.dumps(_resolution()) + "\n",
        encoding="utf-8",
    )
    script = ROOT / "examples" / "live" / "polymarket" / "sports_forward_research.py"
    base_command = [
        sys.executable,
        str(script),
        "--observations",
        str(observation_path),
        "--resolutions",
        str(resolution_path),
    ]

    status = subprocess.run(  # noqa: S603
        [*base_command, "status"],
        check=False,
        text=True,
        capture_output=True,
    )
    evaluate = subprocess.run(  # noqa: S603
        [*base_command, "evaluate"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert status.returncode == 0, status.stderr
    assert evaluate.returncode == 0, evaluate.stderr
    status_payload = json.loads(status.stdout)
    evaluation_payload = json.loads(evaluate.stdout)
    assert status_payload["counts"]["complete_rows"] == 1
    assert "performance" not in status_payload
    assert evaluation_payload["performance"]["net_pnl"] == 1.94


def test_cli_fails_nonzero_for_missing_observation_path(tmp_path: Path):
    script = ROOT / "examples" / "live" / "polymarket" / "sports_forward_research.py"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(script),
            "--observations",
            str(tmp_path / "missing"),
            "--resolutions",
            str(tmp_path / "resolutions.jsonl"),
            "status",
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "FileNotFoundError" in result.stderr
