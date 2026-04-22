"""Unit tests for weather_city_ev_selector module.

Run with:
    docker exec nautilus-weather-settlement-vpn python3 -m pytest \
        /workspace/tests/unit_tests/examples/test_weather_city_ev_selector.py \
        --noconftest -q

Or on the host (no compiled extensions required):
    uv run --extra polymarket --with pytest python -m pytest \
        tests/unit_tests/examples/test_weather_city_ev_selector.py \
        --noconftest -q
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap — allows running without an installed examples package.
# ---------------------------------------------------------------------------
_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from examples.live.polymarket.weather_city_ev_selector import (
    CandidateSignal,
    SelectionResult,
    ev,
    select_best_city_candidates,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make(
    city: str = "NYC",
    observation_date: str = "2026-04-22",
    market_slug: str = "nyc-temp-2026-04-22",
    token_side: str = "yes",
    threshold_f: float = 70.0,
    mid: float = 0.50,
    estimated_prob: float = 0.60,
    fee_rate: float = 0.0,
    slippage: float = 0.0,
) -> CandidateSignal:
    return CandidateSignal(
        city=city,
        observation_date=observation_date,
        market_slug=market_slug,
        token_side=token_side,
        threshold_f=threshold_f,
        mid=mid,
        estimated_prob=estimated_prob,
        fee_rate=fee_rate,
        slippage=slippage,
    )


# ---------------------------------------------------------------------------
# ev() unit tests
# ---------------------------------------------------------------------------

class TestEvFunction:
    def test_positive_edge(self):
        """Model says 60%, market at 50% → EV = +0.10."""
        c = _make(mid=0.50, estimated_prob=0.60)
        assert math.isclose(ev(c), 0.10, rel_tol=1e-9)

    def test_zero_edge(self):
        """Model matches market → EV = 0."""
        c = _make(mid=0.40, estimated_prob=0.40)
        assert math.isclose(ev(c), 0.0, abs_tol=1e-12)

    def test_negative_edge(self):
        """Model says 30%, market at 50% → EV = -0.20."""
        c = _make(mid=0.50, estimated_prob=0.30)
        assert math.isclose(ev(c), -0.20, rel_tol=1e-9)

    def test_fee_reduces_ev(self):
        """Fee at mid=0.5 peaks → should reduce positive EV."""
        no_fee = _make(mid=0.50, estimated_prob=0.60, fee_rate=0.0)
        with_fee = _make(mid=0.50, estimated_prob=0.60, fee_rate=0.02)
        assert ev(with_fee) < ev(no_fee)
        # fee = 0.02 × 0.5 × 0.5 = 0.005
        expected = 0.10 - 0.005
        assert math.isclose(ev(with_fee), expected, rel_tol=1e-9)

    def test_fee_near_zero_at_extreme_price(self):
        """At mid=0.01 fees vanish (peak at 0.5)."""
        c = _make(mid=0.01, estimated_prob=0.05, fee_rate=0.02)
        fee = 0.02 * 0.01 * 0.99
        expected = 0.05 - 0.01 - fee
        assert math.isclose(ev(c), expected, rel_tol=1e-9)

    def test_slippage_reduces_ev(self):
        """Slippage increases effective entry cost."""
        base = _make(mid=0.50, estimated_prob=0.60, slippage=0.0)
        slipped = _make(mid=0.50, estimated_prob=0.60, slippage=0.02)
        assert ev(slipped) < ev(base)
        assert math.isclose(ev(slipped), ev(base) - 0.02, rel_tol=1e-9)

    def test_no_token_same_formula(self):
        """For a NO token, estimated_prob is P(NO wins). Same formula applies."""
        # P(YES) = 0.30 → P(NO) = 0.70; NO token mid = 0.70
        no_c = _make(token_side="no", mid=0.70, estimated_prob=0.70)
        # EV = 0.70 - 0.70 = 0.0 (fair price)
        assert math.isclose(ev(no_c), 0.0, abs_tol=1e-12)

        # NO token edge: model says P(NO wins) = 0.80, market mid = 0.70
        no_edge = _make(token_side="no", mid=0.70, estimated_prob=0.80)
        assert math.isclose(ev(no_edge), 0.10, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# select_best_city_candidates() tests
# ---------------------------------------------------------------------------

class TestSelectBestCityCandidates:

    # --- basic selection ---

    def test_single_candidate_positive_ev_selected(self):
        c = _make(mid=0.50, estimated_prob=0.60)
        results = select_best_city_candidates([c])
        assert len(results) == 1
        r = results[0]
        assert r.selected is True
        assert r.reason == "selected"
        assert math.isclose(r.ev, 0.10, rel_tol=1e-9)

    def test_single_candidate_negative_ev_rejected(self):
        c = _make(mid=0.50, estimated_prob=0.30)
        results = select_best_city_candidates([c])
        assert len(results) == 1
        r = results[0]
        assert r.selected is False
        assert r.reason == "negative_ev"

    def test_empty_input(self):
        assert select_best_city_candidates([]) == []

    # --- ranking by EV not raw threshold ---

    def test_higher_threshold_worse_ev_loses(self):
        """Lower threshold with better EV should win over higher threshold."""
        # NYC 70°F YES: mid=0.50, model=0.60 → EV = +0.10
        low_thresh = _make(
            market_slug="nyc-70f",
            threshold_f=70.0,
            mid=0.50,
            estimated_prob=0.60,
        )
        # NYC 75°F YES: mid=0.20, model=0.25 → EV = +0.05
        high_thresh = _make(
            market_slug="nyc-75f",
            threshold_f=75.0,
            mid=0.20,
            estimated_prob=0.25,
        )
        results = select_best_city_candidates([low_thresh, high_thresh])
        by_slug = {r.candidate.market_slug: r for r in results}

        assert by_slug["nyc-70f"].selected is True
        assert by_slug["nyc-70f"].reason == "selected"

        assert by_slug["nyc-75f"].selected is False
        assert by_slug["nyc-75f"].reason == "lower_ev"

    def test_higher_threshold_better_ev_wins(self):
        """Higher threshold CAN win if its EV is genuinely better."""
        # NYC 70°F YES: mid=0.50, model=0.55 → EV = +0.05
        low_thresh = _make(
            market_slug="nyc-70f",
            threshold_f=70.0,
            mid=0.50,
            estimated_prob=0.55,
        )
        # NYC 75°F YES: mid=0.20, model=0.40 → EV = +0.20
        high_thresh = _make(
            market_slug="nyc-75f",
            threshold_f=75.0,
            mid=0.20,
            estimated_prob=0.40,
        )
        results = select_best_city_candidates([low_thresh, high_thresh])
        by_slug = {r.candidate.market_slug: r for r in results}

        assert by_slug["nyc-75f"].selected is True
        assert by_slug["nyc-70f"].selected is False
        assert by_slug["nyc-70f"].reason == "lower_ev"

    # --- tie-break ---

    def test_tie_break_selects_lower_threshold(self):
        """Equal EV → prefer lower threshold_f (more conservative entry)."""
        # Both have EV = estimated_prob - mid. Set same EV explicitly.
        # EV = 0.60 - 0.50 = 0.10 for both
        low_thresh = _make(
            market_slug="nyc-70f",
            threshold_f=70.0,
            mid=0.50,
            estimated_prob=0.60,
        )
        high_thresh = _make(
            market_slug="nyc-75f",
            threshold_f=75.0,
            mid=0.50,
            estimated_prob=0.60,
        )
        assert math.isclose(ev(low_thresh), ev(high_thresh), abs_tol=1e-12)

        results = select_best_city_candidates([low_thresh, high_thresh])
        by_slug = {r.candidate.market_slug: r for r in results}

        assert by_slug["nyc-70f"].selected is True
        assert by_slug["nyc-70f"].reason == "selected"

        assert by_slug["nyc-75f"].selected is False
        assert by_slug["nyc-75f"].reason == "tie_break_loss"

    def test_tie_break_three_way(self):
        """Three-way tie: only the lowest threshold is selected."""
        # EV = 0.10 for all three
        c65 = _make(market_slug="nyc-65f", threshold_f=65.0, mid=0.50, estimated_prob=0.60)
        c70 = _make(market_slug="nyc-70f", threshold_f=70.0, mid=0.50, estimated_prob=0.60)
        c75 = _make(market_slug="nyc-75f", threshold_f=75.0, mid=0.50, estimated_prob=0.60)

        results = select_best_city_candidates([c65, c70, c75])
        by_slug = {r.candidate.market_slug: r for r in results}

        assert by_slug["nyc-65f"].selected is True
        assert by_slug["nyc-65f"].reason == "selected"

        assert by_slug["nyc-70f"].reason == "tie_break_loss"
        assert by_slug["nyc-75f"].reason == "tie_break_loss"

    # --- all negative EV ---

    def test_all_negative_ev_none_selected(self):
        """When all candidates have EV <= 0, return all with selected=False."""
        c1 = _make(market_slug="nyc-70f", threshold_f=70.0, mid=0.60, estimated_prob=0.40)
        c2 = _make(market_slug="nyc-75f", threshold_f=75.0, mid=0.80, estimated_prob=0.50)
        results = select_best_city_candidates([c1, c2])
        assert all(not r.selected for r in results)
        assert all(r.reason == "negative_ev" for r in results)

    def test_zero_ev_not_selected(self):
        """EV = 0 is not positive; should not be selected."""
        c = _make(mid=0.50, estimated_prob=0.50)
        assert math.isclose(ev(c), 0.0, abs_tol=1e-12)
        results = select_best_city_candidates([c])
        assert results[0].selected is False
        assert results[0].reason == "negative_ev"

    # --- per-city isolation ---

    def test_per_city_isolation_each_gets_winner(self):
        """City A and city B each independently get their best candidate."""
        nyc = _make(city="NYC", market_slug="nyc-70f", threshold_f=70.0, mid=0.50, estimated_prob=0.60)
        chi = _make(city="Chicago", market_slug="chi-65f", threshold_f=65.0, mid=0.45, estimated_prob=0.55)

        results = select_best_city_candidates([nyc, chi])
        by_slug = {r.candidate.market_slug: r for r in results}

        assert by_slug["nyc-70f"].selected is True
        assert by_slug["chi-65f"].selected is True

    def test_per_city_isolation_one_city_negative_other_selected(self):
        """Negative EV in NYC does not affect Chicago selection."""
        nyc_bad = _make(
            city="NYC",
            market_slug="nyc-70f",
            threshold_f=70.0,
            mid=0.70,
            estimated_prob=0.40,
        )
        chi_good = _make(
            city="Chicago",
            market_slug="chi-65f",
            threshold_f=65.0,
            mid=0.45,
            estimated_prob=0.60,
        )
        results = select_best_city_candidates([nyc_bad, chi_good])
        by_slug = {r.candidate.market_slug: r for r in results}

        assert by_slug["nyc-70f"].selected is False
        assert by_slug["nyc-70f"].reason == "negative_ev"

        assert by_slug["chi-65f"].selected is True
        assert by_slug["chi-65f"].reason == "selected"

    def test_per_city_multiple_candidates_only_one_selected(self):
        """Each city selects exactly one winner even with many candidates."""
        nyc_a = _make(city="NYC", market_slug="nyc-65f", threshold_f=65.0, mid=0.50, estimated_prob=0.60)
        nyc_b = _make(city="NYC", market_slug="nyc-70f", threshold_f=70.0, mid=0.45, estimated_prob=0.55)
        nyc_c = _make(city="NYC", market_slug="nyc-75f", threshold_f=75.0, mid=0.30, estimated_prob=0.50)
        chi_a = _make(city="Chicago", market_slug="chi-60f", threshold_f=60.0, mid=0.55, estimated_prob=0.65)
        chi_b = _make(city="Chicago", market_slug="chi-65f", threshold_f=65.0, mid=0.40, estimated_prob=0.45)

        results = select_best_city_candidates([nyc_a, nyc_b, nyc_c, chi_a, chi_b])
        nyc_selected = [r for r in results if r.candidate.city == "NYC" and r.selected]
        chi_selected = [r for r in results if r.candidate.city == "Chicago" and r.selected]

        assert len(nyc_selected) == 1
        assert len(chi_selected) == 1

    # --- different observation dates are independent groups ---

    def test_different_dates_independent_groups(self):
        """Same city, different observation dates → separate groups, each can win."""
        c_apr22 = _make(
            city="NYC",
            observation_date="2026-04-22",
            market_slug="nyc-70f-apr22",
            threshold_f=70.0,
            mid=0.50,
            estimated_prob=0.60,
        )
        c_apr23 = _make(
            city="NYC",
            observation_date="2026-04-23",
            market_slug="nyc-70f-apr23",
            threshold_f=70.0,
            mid=0.45,
            estimated_prob=0.58,
        )
        results = select_best_city_candidates([c_apr22, c_apr23])
        by_slug = {r.candidate.market_slug: r for r in results}

        # Both should be selected — they are independent groups
        assert by_slug["nyc-70f-apr22"].selected is True
        assert by_slug["nyc-70f-apr23"].selected is True

    def test_same_city_different_dates_winner_per_date(self):
        """Within each date group, only one winner; dates don't interfere."""
        # Apr 22: two candidates
        c_apr22_good = _make(
            city="NYC",
            observation_date="2026-04-22",
            market_slug="nyc-65f-apr22",
            threshold_f=65.0,
            mid=0.40,
            estimated_prob=0.60,  # EV = 0.20
        )
        c_apr22_bad = _make(
            city="NYC",
            observation_date="2026-04-22",
            market_slug="nyc-70f-apr22",
            threshold_f=70.0,
            mid=0.50,
            estimated_prob=0.55,  # EV = 0.05
        )
        # Apr 23: one candidate
        c_apr23 = _make(
            city="NYC",
            observation_date="2026-04-23",
            market_slug="nyc-75f-apr23",
            threshold_f=75.0,
            mid=0.30,
            estimated_prob=0.20,  # EV = -0.10 (negative)
        )
        results = select_best_city_candidates([c_apr22_good, c_apr22_bad, c_apr23])
        by_slug = {r.candidate.market_slug: r for r in results}

        assert by_slug["nyc-65f-apr22"].selected is True
        assert by_slug["nyc-70f-apr22"].selected is False
        assert by_slug["nyc-70f-apr22"].reason == "lower_ev"

        assert by_slug["nyc-75f-apr23"].selected is False
        assert by_slug["nyc-75f-apr23"].reason == "negative_ev"

    # --- output ordering ---

    def test_output_preserves_input_order(self):
        """Results are returned in the same order as input candidates."""
        c1 = _make(city="NYC", market_slug="nyc-70f", threshold_f=70.0, mid=0.50, estimated_prob=0.60)
        c2 = _make(city="Chicago", market_slug="chi-65f", threshold_f=65.0, mid=0.45, estimated_prob=0.55)
        c3 = _make(city="NYC", market_slug="nyc-75f", threshold_f=75.0, mid=0.20, estimated_prob=0.25)

        results = select_best_city_candidates([c1, c2, c3])
        assert results[0].candidate is c1
        assert results[1].candidate is c2
        assert results[2].candidate is c3

    # --- return type sanity ---

    def test_return_type_is_selection_result(self):
        c = _make()
        results = select_best_city_candidates([c])
        assert len(results) == 1
        assert isinstance(results[0], SelectionResult)

    def test_frozen_dataclass_immutable(self):
        """SelectionResult and CandidateSignal are frozen — mutation raises."""
        c = _make()
        with pytest.raises((AttributeError, TypeError)):
            c.city = "London"  # type: ignore[misc]

        results = select_best_city_candidates([c])
        with pytest.raises((AttributeError, TypeError)):
            results[0].selected = False  # type: ignore[misc]

    # --- no-token test ---

    def test_no_token_selected_when_positive_ev(self):
        """A NO token with positive EV is selectable like any other candidate."""
        # P(NO wins) = 0.75, NO token mid = 0.70 → EV = +0.05
        c = _make(token_side="no", mid=0.70, estimated_prob=0.75)
        results = select_best_city_candidates([c])
        assert results[0].selected is True

    def test_mixed_yes_no_tokens_best_ev_wins(self):
        """YES and NO tokens in same group compete on EV."""
        # YES: mid=0.30, prob=0.35 → EV = 0.05
        yes_c = _make(
            market_slug="nyc-70f-yes",
            token_side="yes",
            threshold_f=70.0,
            mid=0.30,
            estimated_prob=0.35,
        )
        # NO (same threshold, same market): mid=0.70, prob=0.75 → EV = 0.05
        # Equal EV → tie-break on threshold_f → both have same threshold, pick first
        no_c = _make(
            market_slug="nyc-70f-no",
            token_side="no",
            threshold_f=70.0,
            mid=0.70,
            estimated_prob=0.75,
        )
        results = select_best_city_candidates([yes_c, no_c])
        selected = [r for r in results if r.selected]
        assert len(selected) == 1
