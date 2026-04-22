"""
Pure EV-based candidate selector for Polymarket weather temperature markets.

Selects at most one candidate per (city, observation_date) group, ranked by
net expected value rather than raw threshold height. No network calls, no
Nautilus imports, no file I/O — fully unit-testable in isolation.

Usage
-----
Build a list of CandidateSignal objects (one per market/side being considered),
then call select_best_city_candidates() to get a SelectionResult per candidate.

The caller is responsible for supplying estimated_prob — this module does not
compute probability calibration. See the design note for what a real
calibration pipeline would need to produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
from operator import attrgetter


@dataclass(frozen=True, slots=True)
class CandidateSignal:
    """A single market side being considered for entry.

    Attributes
    ----------
    city:
        City name, e.g. "NYC".
    observation_date:
        Date the temperature observation applies to, "YYYY-MM-DD".
    market_slug:
        Polymarket market slug, used for identification only.
    token_side:
        "yes" or "no".
    threshold_f:
        Temperature threshold in °F (or °C for non-US cities — the unit is
        preserved as-is; comparison is only within a single city group so
        mixing units across cities is safe).
    mid:
        Current CLOB mid-price in [0, 1].
    estimated_prob:
        Calibrated probability that this token side wins, in [0, 1].
        For YES tokens: P(temperature meets or exceeds threshold).
        For NO tokens: P(temperature does NOT meet threshold) = 1 - P(YES).
    fee_rate:
        Polymarket fee fraction.  Polymarket applies fee = qty × rate × p × (1-p);
        here we approximate as fee_rate × mid × (1 - mid) per unit stake.
        Default 0 (conservative: ignore fees in testing / early use).
    slippage:
        Expected execution slippage as a fraction of stake. Subtracted from
        gross EV before fee. Default 0.
    """

    city: str
    observation_date: str  # "YYYY-MM-DD"
    market_slug: str
    token_side: str  # "yes" or "no"
    threshold_f: float  # may be °C for non-US cities
    mid: float  # current CLOB mid-price (0–1)
    estimated_prob: float  # calibrated probability of winning (0–1)
    fee_rate: float = 0.0  # Polymarket fee fraction (default 0)
    slippage: float = 0.0  # expected slippage fraction


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Outcome of the EV selection pass for one candidate.

    Attributes
    ----------
    candidate:
        The original CandidateSignal.
    selected:
        True if this candidate was chosen for entry.
    reason:
        One of:
        - "selected"       — highest EV in group and EV > 0.
        - "lower_ev"       — another candidate in the group had higher EV.
        - "negative_ev"    — this candidate's EV <= 0 (no positive edge).
        - "tie_break_loss" — equal EV but lost tie-break (higher threshold_f).
    ev:
        Computed net EV for this candidate.
    """

    candidate: CandidateSignal
    selected: bool
    reason: str  # "selected" | "lower_ev" | "negative_ev" | "tie_break_loss"
    ev: float


# ---------------------------------------------------------------------------
# EV calculation
# ---------------------------------------------------------------------------

def ev(candidate: CandidateSignal) -> float:
    """Net EV per unit stake, including fees and slippage.

    For a token whose winning probability is estimated_prob and whose current
    mid is p:

        gross_ev = estimated_prob × (1 − entry_cost) − (1 − estimated_prob) × entry_cost
                 = estimated_prob − entry_cost

    where entry_cost = mid + slippage.

    Polymarket fee approximation (peaks at mid = 0.5):
        fee ≈ fee_rate × mid × (1 − mid)

    Net EV = gross_ev − fee

    This formula applies identically to YES and NO tokens: for a NO token
    estimated_prob should already be the probability that the NO side wins
    (i.e. 1 − P(YES)), so no sign flip is needed.
    """
    entry_cost = candidate.mid + candidate.slippage
    gross_ev = candidate.estimated_prob - entry_cost
    fee = candidate.fee_rate * candidate.mid * (1.0 - candidate.mid)
    return gross_ev - fee


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_EV_TIE_EPSILON = 1e-9


def select_best_city_candidates(
    candidates: list[CandidateSignal],
) -> list[SelectionResult]:
    """Select at most one candidate per (city, observation_date) group.

    Algorithm
    ---------
    1. Group candidates by (city, observation_date).
    2. Within each group compute ev() for every candidate.
    3. If the highest EV <= 0, reject all with reason "negative_ev".
    4. Otherwise select the candidate with the highest EV.
    5. Tie-break: when two candidates have EV within 1e-9 of each other,
       prefer the lower threshold_f (more conservative entry). The loser
       receives reason "tie_break_loss".
    6. All non-selected candidates with positive EV receive reason "lower_ev".

    Returns a flat list of SelectionResult objects in the same relative order
    as the input (within each group). Groups themselves are processed in the
    order their first member appears in the input list.

    Parameters
    ----------
    candidates:
        List of CandidateSignal objects to evaluate. May be empty.

    Returns
    -------
    list[SelectionResult]
        One result per input candidate.
    """
    if not candidates:
        return []

    # Compute EV for all candidates up-front so we only call ev() once each.
    ev_map: dict[int, float] = {id(c): ev(c) for c in candidates}

    # Group by (city, observation_date) while preserving insertion order.
    # We cannot use itertools.groupby directly because that requires the list
    # to be sorted by the key, which would scramble the original order.
    groups: dict[tuple[str, str], list[CandidateSignal]] = {}
    for c in candidates:
        key = (c.city, c.observation_date)
        groups.setdefault(key, []).append(c)

    # Determine the winner for each group.
    winners: set[int] = set()  # id(candidate)
    tie_break_losers: set[int] = set()

    for group_candidates in groups.values():
        best_ev = max(ev_map[id(c)] for c in group_candidates)

        if best_ev <= 0.0:
            # All candidates are negative (or zero) EV — skip group entirely.
            continue

        # Find all candidates that share the best EV (within epsilon).
        top = [c for c in group_candidates if abs(ev_map[id(c)] - best_ev) < _EV_TIE_EPSILON]

        if len(top) == 1:
            winners.add(id(top[0]))
        else:
            # Tie-break: prefer lowest threshold_f (most conservative).
            top_sorted = sorted(top, key=attrgetter("threshold_f"))
            winners.add(id(top_sorted[0]))
            for loser in top_sorted[1:]:
                tie_break_losers.add(id(loser))

    # Build results in original input order.
    results: list[SelectionResult] = []
    for c in candidates:
        cid = id(c)
        candidate_ev = ev_map[cid]

        if cid in winners:
            reason = "selected"
            selected = True
        elif cid in tie_break_losers:
            reason = "tie_break_loss"
            selected = False
        elif candidate_ev <= 0.0:
            reason = "negative_ev"
            selected = False
        else:
            reason = "lower_ev"
            selected = False

        results.append(SelectionResult(candidate=c, selected=selected, reason=reason, ev=candidate_ev))

    return results
