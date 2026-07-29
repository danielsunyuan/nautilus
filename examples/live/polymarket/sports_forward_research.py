#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------
"""Resolve, validate, and evaluate registered sports forward observations."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import importlib.util
import json
import os
import sys
from collections import Counter
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any


OBSERVATION_SCHEMA_VERSION = "sports_forward_observation.v1"
RESOLUTION_SCHEMA_VERSION = "sports_forward_resolution.v1"
REGISTERED_CHECKPOINTS = ("t_minus_60m", "t_minus_15m")
REGISTERED_SPORTS = ("atp", "wta")
REGISTERED_SHARE_QUANTITY = 5.0
REGISTERED_MIN_EDGE = 0.05
REGISTERED_MIN_PRICE = 0.50
REGISTERED_MAX_PRICE = 0.981
REGISTERED_MAX_SPREAD = 0.02
MAX_CHECKPOINT_LAG_SECONDS = 120.0
MAX_SOURCE_AGE_SECONDS = 180.0
MIN_BOOKMAKERS = 3


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _observation_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    values = (
        row.get("condition_id"),
        row.get("token_id"),
        row.get("checkpoint"),
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    return values  # type: ignore[return-value]


def calculate_entry_fee(
    *,
    shares: float,
    price: float,
    fee_rate: float,
    fee_exponent: float,
) -> float:
    """Calculate the captured taker fee and apply Polymarket's USDC precision."""
    values = (shares, price, fee_rate, fee_exponent)
    if not all(_float_or_none(value) is not None for value in values):
        raise ValueError("fee inputs must be finite numbers")
    if shares <= 0 or not 0 < price < 1 or fee_rate < 0 or fee_exponent < 0:
        raise ValueError("invalid fee inputs")
    fee = shares * fee_rate * (price * (1.0 - price)) ** fee_exponent
    return round(fee, 5)


def observation_exclusion_reasons(row: dict[str, Any]) -> tuple[str, ...]:  # noqa: C901
    """Return every registered reason which makes an observation ineligible."""
    reasons: list[str] = []
    if row.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        reasons.append("schema_version_invalid")
    if _observation_key(row) is None:
        reasons.append("observation_key_invalid")
    if row.get("is_complete") is not True:
        reasons.append("observation_incomplete")
    raw_reasons = row.get("missing_data_reasons")
    if isinstance(raw_reasons, list):
        reasons.extend(
            reason
            for reason in raw_reasons
            if isinstance(reason, str) and reason
        )
    elif raw_reasons not in (None, ()):
        reasons.append("missing_data_reasons_invalid")

    if row.get("sport") not in REGISTERED_SPORTS:
        reasons.append("sport_out_of_scope")
    if row.get("market_type") != "moneyline":
        reasons.append("market_type_out_of_scope")
    if row.get("checkpoint") not in REGISTERED_CHECKPOINTS:
        reasons.append("checkpoint_out_of_scope")
    if not isinstance(row.get("resolution_source"), str) or not row["resolution_source"]:
        reasons.append("resolution_source_missing")

    collected_at = _parse_datetime(row.get("collected_at"))
    intended_at = _parse_datetime(row.get("intended_decision_time"))
    start_time = _parse_datetime(row.get("start_time"))
    venue_time = _parse_datetime(row.get("venue_timestamp"))
    if None in (collected_at, intended_at, start_time, venue_time):
        reasons.append("timestamp_invalid")

    checkpoint_lag = _float_or_none(row.get("checkpoint_lag_seconds"))
    if checkpoint_lag is None or checkpoint_lag < 0:
        reasons.append("checkpoint_lag_invalid")
    if (
        collected_at is not None
        and intended_at is not None
        and start_time is not None
        and venue_time is not None
    ):
        computed_lag = (collected_at - intended_at).total_seconds()
        expected_offset = (
            3600.0
            if row.get("checkpoint") == "t_minus_60m"
            else 900.0
        )
        if (
            checkpoint_lag is None
            or abs(checkpoint_lag - computed_lag) > 1.0
        ):
            reasons.append("checkpoint_lag_mismatch")
        if (
            computed_lag < 0
            or collected_at >= start_time
            or venue_time > collected_at
            or abs((start_time - intended_at).total_seconds() - expected_offset) > 1.0
        ):
            reasons.append("checkpoint_time_invalid")
        if computed_lag > MAX_CHECKPOINT_LAG_SECONDS:
            reasons.append("checkpoint_late")
    if checkpoint_lag is not None and checkpoint_lag > MAX_CHECKPOINT_LAG_SECONDS:
        reasons.append("checkpoint_late")

    bid = _float_or_none(row.get("best_bid"))
    bid_size = _float_or_none(row.get("bid_size"))
    ask = _float_or_none(row.get("best_ask"))
    ask_size = _float_or_none(row.get("ask_size"))
    walked_ask = _float_or_none(row.get("walked_ask"))
    if (
        bid is None
        or ask is None
        or walked_ask is None
        or bid_size is None
        or ask_size is None
        or not 0 <= bid <= ask <= walked_ask <= 1
        or bid_size < 0
        or ask_size < 0
    ):
        reasons.append("venue_data_invalid")

    shares = _float_or_none(row.get("research_share_quantity"))
    tick_size = _float_or_none(row.get("tick_size"))
    minimum_order_size = _float_or_none(row.get("minimum_order_size"))
    fee_rate = _float_or_none(row.get("fee_rate"))
    fee_exponent = _float_or_none(row.get("fee_exponent"))
    if shares != REGISTERED_SHARE_QUANTITY:
        reasons.append("research_quantity_invalid")
    if (
        tick_size is None
        or tick_size <= 0
        or minimum_order_size is None
        or minimum_order_size <= 0
    ):
        reasons.append("market_parameters_invalid")
    if (
        shares is not None
        and minimum_order_size is not None
        and minimum_order_size > shares
    ):
        reasons.append("minimum_order_size_exceeds_quantity")
    if (
        fee_rate is None
        or fee_rate < 0
        or fee_exponent is None
        or fee_exponent < 0
        or not isinstance(row.get("fee_taker_only"), bool)
    ):
        reasons.append("fee_parameters_invalid")

    consensus = _float_or_none(row.get("devig_consensus_probability"))
    if consensus is None or not 0 <= consensus <= 1:
        reasons.append("bookmaker_consensus_invalid")

    quotes = row.get("bookmaker_quotes")
    bookmaker_outcomes: dict[str, set[str]] = defaultdict(set)
    quote_invalid = False
    if isinstance(quotes, list):
        for quote in quotes:
            if not isinstance(quote, dict):
                quote_invalid = True
                continue
            bookmaker = quote.get("bookmaker")
            outcome = quote.get("outcome_name")
            if isinstance(bookmaker, str) and bookmaker and isinstance(outcome, str) and outcome:
                bookmaker_outcomes[bookmaker].add(outcome)
            decimal_odds = _float_or_none(quote.get("decimal_odds"))
            implied = _float_or_none(quote.get("implied_probability"))
            devig = _float_or_none(quote.get("devig_probability"))
            if (
                _parse_datetime(quote.get("observed_at")) is None
                or decimal_odds is None
                or decimal_odds <= 1
                or implied is None
                or not 0 <= implied <= 1
                or devig is None
                or not 0 <= devig <= 1
            ):
                quote_invalid = True
    if (
        len(bookmaker_outcomes) < MIN_BOOKMAKERS
        or any(len(outcomes) != 2 for outcomes in bookmaker_outcomes.values())
        or any(
            row.get("outcome_name") not in outcomes
            for outcomes in bookmaker_outcomes.values()
        )
    ):
        reasons.append("bookmaker_count_below_3")
    if quote_invalid:
        reasons.append("bookmaker_quote_invalid")

    freshness = row.get("source_freshness")
    source_stale = not isinstance(freshness, list) or not freshness
    fresh_bookmakers: set[str] = set()
    if isinstance(freshness, list):
        for source in freshness:
            if not isinstance(source, dict):
                source_stale = True
                continue
            source_name = source.get("source")
            age = _float_or_none(source.get("age_seconds"))
            source_time = _parse_datetime(source.get("observed_at"))
            if (
                age is None
                or age < 0
                or age > MAX_SOURCE_AGE_SECONDS
                or collected_at is None
                or source_time is None
                or source_time > collected_at
                or abs((collected_at - source_time).total_seconds() - age) > 1.0
            ):
                source_stale = True
            elif isinstance(source_name, str) and source_name.startswith("bookmaker:"):
                fresh_bookmakers.add(source_name.removeprefix("bookmaker:"))
    if source_stale:
        reasons.append("source_stale")
    if not set(bookmaker_outcomes).issubset(fresh_bookmakers):
        reasons.append("bookmaker_freshness_mismatch")

    return _unique(reasons)


def _files_for_paths(paths: Iterable[Path]) -> list[Path]:
    files: set[Path] = set()
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_dir():
            matched = set(path.rglob("sports_forward_*.jsonl"))
            matched.update(path.rglob("sports_forward_*.jsonl.gz"))
            if not matched:
                raise ValueError(f"no sports forward files under {path}")
            files.update(matched)
        elif path.is_file():
            files.add(path)
    if not files:
        raise ValueError("no sports forward files supplied")
    return sorted(files)


def load_observations(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Load observation rows from plain or gzip JSONL paths."""
    rows: list[dict[str, Any]] = []
    for path in _files_for_paths(Path(value) for value in paths):
        opener = gzip.open if path.suffix == ".gz" else Path.open
        kwargs = {"mode": "rt", "encoding": "utf-8"} if path.suffix == ".gz" else {
            "mode": "r",
            "encoding": "utf-8",
        }
        with opener(path, **kwargs) as handle:  # type: ignore[arg-type]
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"non-object JSON at {path}:{line_number}")
                rows.append(row)
    return rows


def load_resolution_records(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load the latest valid resolution record per condition."""
    result: dict[str, dict[str, Any]] = {}
    resolution_path = Path(path)
    if not resolution_path.exists():
        return result
    with resolution_path.open(encoding="utf-8") as handle:
        lines = handle.readlines()
        final_nonempty = max(
            (index for index, line in enumerate(lines) if line.strip()),
            default=-1,
        )
        for line_index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                if line_index == final_nonempty:
                    break
                raise ValueError(
                    f"invalid resolution JSON at {resolution_path}:{line_index + 1}",
                ) from exc
            if not isinstance(row, dict):
                continue
            condition_id = row.get("condition_id")
            if (
                row.get("schema_version") == RESOLUTION_SCHEMA_VERSION
                and isinstance(condition_id, str)
                and condition_id
                and isinstance(row.get("winning_token_id"), str)
                and row["winning_token_id"]
                and isinstance(row.get("winning_outcome"), str)
                and row["winning_outcome"]
                and isinstance(row.get("resolution_source"), str)
                and row["resolution_source"]
                and _parse_datetime(row.get("resolution_observed_at")) is not None
                and row.get("reconciliation_status")
                in {"matched", "mismatched", "unavailable"}
            ):
                result[condition_id] = row
    return result


def load_named_results(path: str | Path | None) -> dict[str, dict[str, Any]]:
    """Load optional independently captured ATP/WTA result evidence."""
    if path is None or not Path(path).exists():
        return {}
    results: dict[str, dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid named result at {path}:{line_number}") from exc
            if not isinstance(row, dict):
                continue
            condition_id = row.get("condition_id")
            outcome = row.get("winning_outcome")
            source = row.get("source")
            if (
                isinstance(condition_id, str)
                and condition_id
                and isinstance(outcome, str)
                and outcome
                and isinstance(source, str)
                and source
            ):
                results[condition_id] = row
    return results


def _normalized_outcome(value: str) -> str:
    return " ".join(value.casefold().split())


def resolution_record_from_market_resolution(
    resolution: Any,
    *,
    named_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Convert exact CLOB evidence into the append-only derived record."""
    required = (
        getattr(resolution, "condition_id", None),
        getattr(resolution, "winning_token_id", None),
        getattr(resolution, "winning_outcome", None),
        getattr(resolution, "resolution_source", None),
        getattr(resolution, "observed_at", None),
    )
    if getattr(resolution, "resolved", None) is not True or not all(
        isinstance(value, str) and value
        for value in required
    ):
        return None

    named_source = None
    named_outcome = None
    reconciliation_status = "unavailable"
    if named_result is not None:
        source = named_result.get("source")
        outcome = named_result.get("winning_outcome")
        if isinstance(source, str) and source and isinstance(outcome, str) and outcome:
            named_source = source
            named_outcome = outcome
            reconciliation_status = (
                "matched"
                if _normalized_outcome(outcome)
                == _normalized_outcome(resolution.winning_outcome)
                else "mismatched"
            )

    return {
        "schema_version": RESOLUTION_SCHEMA_VERSION,
        "condition_id": resolution.condition_id,
        "winning_token_id": resolution.winning_token_id,
        "winning_outcome": resolution.winning_outcome,
        "resolution_source": resolution.resolution_source,
        "resolution_observed_at": resolution.observed_at,
        "named_source": named_source,
        "named_source_outcome": named_outcome,
        "reconciliation_status": reconciliation_status,
    }


def enrich_observation(
    observation: dict[str, Any],
    resolution: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a derived copy with exact winner-token settlement evidence."""
    enriched = dict(observation)
    enriched.update(
        {
            "resolved": False,
            "settlement_price": None,
            "winning_token_id": None,
            "winning_outcome": None,
            "resolution_observed_at": None,
            "reconciliation_status": None,
        },
    )
    if (
        not isinstance(resolution, dict)
        or resolution.get("schema_version") != RESOLUTION_SCHEMA_VERSION
        or resolution.get("condition_id") != observation.get("condition_id")
        or not isinstance(resolution.get("winning_token_id"), str)
        or not resolution["winning_token_id"]
    ):
        return enriched

    enriched.update(
        {
            "resolved": True,
            "settlement_price": (
                1.0
                if observation.get("token_id") == resolution["winning_token_id"]
                else 0.0
            ),
            "winning_token_id": resolution["winning_token_id"],
            "winning_outcome": resolution.get("winning_outcome"),
            "resolution_observed_at": resolution.get("resolution_observed_at"),
            "reconciliation_status": resolution.get("reconciliation_status"),
        },
    )
    return enriched


def _candidate_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    if observation_exclusion_reasons(row) or row.get("resolved") is not True:
        return None
    bid = float(row["best_bid"])
    ask = float(row["best_ask"])
    price = float(row["walked_ask"])
    shares = float(row["research_share_quantity"])
    consensus = float(row["devig_consensus_probability"])
    if not REGISTERED_MIN_PRICE <= price < REGISTERED_MAX_PRICE:
        return None
    if ask - bid > REGISTERED_MAX_SPREAD:
        return None

    fee = calculate_entry_fee(
        shares=shares,
        price=price,
        fee_rate=float(row["fee_rate"]),
        fee_exponent=float(row["fee_exponent"]),
    )
    expected_edge = consensus - price - (fee / shares)
    if expected_edge < REGISTERED_MIN_EDGE:
        return None

    settlement_price = float(row["settlement_price"])
    gross_pnl = (settlement_price - price) * shares
    candidate = dict(row)
    candidate.update(
        {
            "entry_fee": fee,
            "fee_adjusted_expected_edge": expected_edge,
            "gross_pnl": gross_pnl,
            "net_pnl": gross_pnl - fee,
            "won": settlement_price == 1.0,
        },
    )
    return candidate


def select_registered_candidates(
    enriched_observations: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select at most one registered token per condition and checkpoint."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in enriched_observations:
        candidate = _candidate_from_row(row)
        if candidate is None:
            continue
        grouped[(candidate["condition_id"], candidate["checkpoint"])].append(candidate)

    selected: list[dict[str, Any]] = []
    for key in sorted(grouped):
        candidates = sorted(
            grouped[key],
            key=lambda row: (
                -float(row["fee_adjusted_expected_edge"]),
                str(row["token_id"]),
            ),
        )
        selected.append(candidates[0])
    return selected


def _sum_by(rows: Iterable[dict[str, Any]], field: str) -> dict[str, float]:
    result: dict[str, float] = defaultdict(float)
    for row in rows:
        key = str(row.get(field) or "unknown")
        result[key] += float(row["net_pnl"])
    return dict(sorted(result.items()))


def _week_for_row(row: dict[str, Any]) -> str:
    timestamp = _parse_datetime(row.get("collected_at"))
    if timestamp is None:
        return "unknown"
    year, week, _weekday = timestamp.isocalendar()
    return f"{year}-W{week:02d}"


def _price_band_for_row(row: dict[str, Any]) -> str:
    price = float(row["walked_ask"])
    lower = int(price * 10) / 10
    upper = lower + 0.1
    return f"{lower:.2f}-{upper:.2f}"


def _calibration_report(rows: list[dict[str, Any]]) -> dict[str, float | int | None]:
    if not rows:
        return {
            "rows": 0,
            "consensus_brier_score": None,
            "clob_mid_brier_score": None,
        }
    consensus_squared_error = 0.0
    midpoint_squared_error = 0.0
    for row in rows:
        actual = float(row["settlement_price"])
        consensus = float(row["devig_consensus_probability"])
        midpoint = (float(row["best_bid"]) + float(row["best_ask"])) / 2
        consensus_squared_error += (consensus - actual) ** 2
        midpoint_squared_error += (midpoint - actual) ** 2
    return {
        "rows": len(rows),
        "consensus_brier_score": consensus_squared_error / len(rows),
        "clob_mid_brier_score": midpoint_squared_error / len(rows),
    }


def _candidate_summary(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    candidates = select_registered_candidates(rows)
    return {
        "entries": len(candidates),
        "net_pnl": sum(float(row["net_pnl"]) for row in candidates),
    }


def _bookmaker_count(row: dict[str, Any]) -> int:
    quotes = row.get("bookmaker_quotes")
    if not isinstance(quotes, list):
        return 0
    return len(
        {
            quote.get("bookmaker")
            for quote in quotes
            if isinstance(quote, dict)
            and isinstance(quote.get("bookmaker"), str)
            and quote["bookmaker"]
        },
    )


def _maximum_bookmaker_age(row: dict[str, Any]) -> float:
    freshness = row.get("source_freshness")
    ages = [
        float(source["age_seconds"])
        for source in freshness
        if isinstance(source, dict)
        and isinstance(source.get("source"), str)
        and source["source"].startswith("bookmaker:")
        and _float_or_none(source.get("age_seconds")) is not None
    ]
    return max(ages, default=float("inf"))


def _sensitivity_report(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, float | int]]]:
    return {
        "minimum_bookmakers": {
            str(minimum): _candidate_summary(
                [
                    row
                    for row in rows
                    if _bookmaker_count(row) >= minimum
                ],
            )
            for minimum in (3, 4, 5)
        },
        "maximum_quote_age_seconds": {
            str(maximum): _candidate_summary(
                [
                    row
                    for row in rows
                    if _maximum_bookmaker_age(row) <= maximum
                ],
            )
            for maximum in (60, 120, 180)
        },
    }


def _concentration_gate(grouped_net: dict[str, float], total_net: float) -> bool:
    if total_net <= 0 or not grouped_net:
        return False
    largest_positive = max(max(value, 0.0) for value in grouped_net.values())
    return largest_positive / total_net <= 0.5


def evaluate_forward_window(
    *,
    observations: list[dict[str, Any]],
    resolutions: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the deterministic registered status and performance report."""
    evaluated_at = (now or datetime.now(tz=UTC)).astimezone(UTC)
    key_counts = Counter(
        key
        for row in observations
        if (key := _observation_key(row)) is not None
    )
    duplicate_keys = {key for key, count in key_counts.items() if count > 1}

    exclusions: Counter[str] = Counter()
    complete: list[dict[str, Any]] = []
    for row in observations:
        key = _observation_key(row)
        if key in duplicate_keys:
            exclusions["duplicate_observation_key"] += 1
            continue
        reasons = observation_exclusion_reasons(row)
        if reasons:
            exclusions.update(reasons)
            continue
        complete.append(row)

    enriched = [
        enrich_observation(row, resolutions.get(str(row.get("condition_id"))))
        for row in complete
    ]
    resolved = [row for row in enriched if row.get("resolved") is True]
    candidates = select_registered_candidates(resolved)

    first_complete = min(
        (_parse_datetime(row.get("collected_at")) for row in complete),
        default=None,
    )
    elapsed_30_days = (
        first_complete is not None
        and evaluated_at - first_complete >= timedelta(days=30)
    )

    by_checkpoint = _sum_by(candidates, "checkpoint")
    by_sport = _sum_by(candidates, "sport")
    by_tournament = _sum_by(candidates, "competition")
    by_price_band: dict[str, float] = defaultdict(float)
    for row in candidates:
        by_price_band[_price_band_for_row(row)] += float(row["net_pnl"])
    by_price_band = dict(sorted(by_price_band.items()))
    by_week: dict[str, float] = defaultdict(float)
    for row in candidates:
        by_week[_week_for_row(row)] += float(row["net_pnl"])
    by_week = dict(sorted(by_week.items()))

    gross_pnl = sum(float(row["gross_pnl"]) for row in candidates)
    entry_fees = sum(float(row["entry_fee"]) for row in candidates)
    net_pnl = sum(float(row["net_pnl"]) for row in candidates)
    atp_resolved = sum(row.get("sport") == "atp" for row in resolved)
    wta_resolved = sum(row.get("sport") == "wta" for row in resolved)

    gates = {
        "elapsed_30_days": elapsed_30_days,
        "resolved_300": len(resolved) >= 300,
        "atp_100": atp_resolved >= 100,
        "wta_100": wta_resolved >= 100,
        "aggregate_net_positive": net_pnl > 0,
        "both_checkpoints_positive": all(
            by_checkpoint.get(checkpoint, 0.0) > 0
            for checkpoint in REGISTERED_CHECKPOINTS
        ),
        "tournament_concentration": _concentration_gate(by_tournament, net_pnl),
        "week_concentration": _concentration_gate(by_week, net_pnl),
    }
    gates["all_pass"] = all(gates.values())

    reconciliation = Counter(
        str(row.get("reconciliation_status") or "unavailable")
        for row in resolved
    )
    return {
        "schema_version": "sports_forward_evaluation.v1",
        "evaluated_at": evaluated_at.isoformat(),
        "first_complete_at": first_complete.isoformat() if first_complete else None,
        "registered_rule": {
            "share_quantity": REGISTERED_SHARE_QUANTITY,
            "minimum_fee_adjusted_edge": REGISTERED_MIN_EDGE,
            "minimum_walked_ask": REGISTERED_MIN_PRICE,
            "maximum_walked_ask_exclusive": REGISTERED_MAX_PRICE,
            "maximum_spread": REGISTERED_MAX_SPREAD,
        },
        "counts": {
            "raw_rows": len(observations),
            "complete_rows": len(complete),
            "resolved_complete_rows": len(resolved),
            "atp_resolved_complete_rows": atp_resolved,
            "wta_resolved_complete_rows": wta_resolved,
            "registered_entries": len(candidates),
        },
        "exclusions": dict(sorted(exclusions.items())),
        "reconciliation": dict(sorted(reconciliation.items())),
        "calibration": _calibration_report(resolved),
        "sensitivity": _sensitivity_report(resolved),
        "performance": {
            "gross_pnl": gross_pnl,
            "entry_fees": entry_fees,
            "net_pnl": net_pnl,
            "by_checkpoint_net_pnl": by_checkpoint,
            "by_sport_net_pnl": by_sport,
            "by_price_band_net_pnl": by_price_band,
            "by_tournament_net_pnl": by_tournament,
            "by_week_net_pnl": by_week,
        },
        "gates": gates,
    }


def _append_resolution(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_sports_settlement_module() -> Any:
    module_name = "sports_settlement_for_forward_research"
    module_path = Path(__file__).resolve().with_name("sports_settlement.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _append_named_reconciliations(
    *,
    existing: dict[str, dict[str, Any]],
    named_results: dict[str, dict[str, Any]],
    resolution_path: Path,
) -> int:
    written = 0
    for condition_id, named_result in named_results.items():
        current = existing.get(condition_id)
        if current is None:
            continue
        named_source = named_result.get("source")
        named_outcome = named_result.get("winning_outcome")
        winning_outcome = current.get("winning_outcome")
        if (
            not isinstance(named_source, str)
            or not named_source
            or not isinstance(named_outcome, str)
            or not named_outcome
            or not isinstance(winning_outcome, str)
            or not winning_outcome
        ):
            continue
        reconciled = dict(current)
        reconciled.update(
            {
                "named_source": named_source,
                "named_source_outcome": named_outcome,
                "reconciliation_status": (
                    "matched"
                    if _normalized_outcome(named_outcome)
                    == _normalized_outcome(winning_outcome)
                    else "mismatched"
                ),
            },
        )
        if reconciled != current:
            _append_resolution(resolution_path, reconciled)
            existing[condition_id] = reconciled
            written += 1
    return written


async def resolve_once(
    *,
    observations: list[dict[str, Any]],
    resolution_path: Path,
    clob_host: str,
    named_results: dict[str, dict[str, Any]] | None = None,
    fetch_resolution: Any | None = None,
) -> int:
    """Append newly resolved exact CLOB winner records once per condition."""
    existing = load_resolution_records(resolution_path)
    written = _append_named_reconciliations(
        existing=existing,
        named_results=named_results or {},
        resolution_path=resolution_path,
    )

    condition_ids = sorted(
        {
            str(row["condition_id"])
            for row in observations
            if isinstance(row.get("condition_id"), str)
            and row["condition_id"]
            and row["condition_id"] not in existing
        },
    )
    if fetch_resolution is not None:
        for condition_id in condition_ids:
            resolution = await fetch_resolution(condition_id)
            record = resolution_record_from_market_resolution(
                resolution,
                named_result=(named_results or {}).get(condition_id),
            )
            if record is not None:
                _append_resolution(resolution_path, record)
                written += 1
        return written

    try:
        import httpx
    except ImportError as exc:
        raise SystemExit("httpx is required: pip install httpx") from exc

    settlement = _load_sports_settlement_module()
    async with httpx.AsyncClient() as client:
        for condition_id in condition_ids:
            resolution = await settlement.fetch_market_resolution(
                condition_id=condition_id,
                http_client=client,
                clob_base_url=clob_host,
            )
            record = resolution_record_from_market_resolution(
                resolution,
                named_result=(named_results or {}).get(condition_id),
            )
            if record is not None:
                _append_resolution(resolution_path, record)
                written += 1
    return written


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve and evaluate registered sports forward observations",
    )
    parser.add_argument(
        "--observations",
        action="append",
        required=True,
        help="Observation file or directory; may be repeated",
    )
    parser.add_argument(
        "--resolutions",
        required=True,
        help="Append-only sports forward resolution JSONL",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("evaluate")
    resolve_parser = subparsers.add_parser("resolve-once")
    resolve_parser.add_argument(
        "--clob-host",
        default="https://clob.polymarket.com",
    )
    resolve_parser.add_argument("--named-results")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    observations = load_observations(args.observations)
    resolution_path = Path(args.resolutions)
    if args.command == "resolve-once":
        named_results = load_named_results(args.named_results)
        written = asyncio.run(
            resolve_once(
                observations=observations,
                resolution_path=resolution_path,
                clob_host=args.clob_host,
                named_results=named_results,
            ),
        )
        print(json.dumps({"resolutions_written": written}, sort_keys=True))
        return

    report = evaluate_forward_window(
        observations=observations,
        resolutions=load_resolution_records(resolution_path),
    )
    if args.command == "status":
        report = {
            key: report[key]
            for key in (
                "schema_version",
                "evaluated_at",
                "first_complete_at",
                "counts",
                "exclusions",
                "reconciliation",
                "gates",
            )
        }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
