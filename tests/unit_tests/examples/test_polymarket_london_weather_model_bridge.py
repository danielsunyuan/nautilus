from __future__ import annotations

from datetime import date

from examples.live.polymarket.london_weather_model_bridge import build_london_model_candidates


def _market(**overrides):
    market = {
        "slug": "london-high-20",
        "condition_id": "0xcondition",
        "city": "London",
        "observation_date": date(2026, 6, 1),
        "metric": "high",
        "threshold_f": 20.0,
        "band_type": "or_higher",
        "yes_token_id": "yes-token",
        "no_token_id": "no-token",
        "yes_ask": 0.52,
        "yes_bid": 0.48,
        "binary_yes_no_confirmed": True,
    }
    market.update(overrides)
    return market


def _snapshot(*rows):
    return [
        {
            "target_local_date": "2026-06-01",
            "market_line": line,
            "predicted_probability": probability,
            "model_version": "family_b_test",
        }
        for line, probability in rows
    ]


def test_or_higher_maps_directly_to_high_at_or_above_line() -> None:
    [candidate] = build_london_model_candidates(
        [_market()],
        _snapshot((20.0, 0.63)),
        min_edge=0.05,
    )

    assert candidate.filter_status == "accepted"
    assert candidate.selected_side == "yes"
    assert candidate.model_yes_probability == 0.63
    assert candidate.market_yes_price == 0.52
    assert candidate.edge == 0.11
    assert candidate.forecast_source == "family_b_test"


def test_no_side_uses_no_ask_when_available() -> None:
    [candidate] = build_london_model_candidates(
        [_market(yes_ask=0.65, no_ask=0.31)],
        _snapshot((20.0, 0.42)),
        min_edge=0.05,
    )

    assert candidate.filter_status == "accepted"
    assert candidate.selected_side == "no"
    assert candidate.model_yes_probability == 0.42
    # The legacy WeatherEnsembleCandidate field is named market_yes_price, but
    # for selected_side="no" the bridge stores the selected-side entry price.
    assert candidate.market_yes_price == 0.31
    assert candidate.edge == 0.27


def test_no_side_falls_back_to_yes_bid_for_confirmed_binary_market() -> None:
    [candidate] = build_london_model_candidates(
        [_market(yes_ask=0.72, yes_bid=0.58, no_ask=None, binary_yes_no_confirmed=True)],
        _snapshot((20.0, 0.46)),
        min_edge=0.05,
    )

    assert candidate.filter_status == "accepted"
    assert candidate.selected_side == "no"
    assert candidate.market_yes_price == 0.42
    assert candidate.edge == 0.12


def test_or_lower_rejects_when_next_line_probability_is_missing() -> None:
    [candidate] = build_london_model_candidates(
        [_market(slug="london-below-20", band_type="or_lower", or_lower_complement_confirmed=True)],
        _snapshot((20.0, 0.63)),
        min_edge=0.05,
    )

    assert candidate.filter_status == "rejected"
    assert candidate.filter_reasons == ("missing_next_line_probability",)


def test_or_lower_uses_complement_of_next_line_when_confirmed() -> None:
    [candidate] = build_london_model_candidates(
        [
            _market(
                slug="london-below-20",
                band_type="or_lower",
                threshold_f=20.0,
                yes_ask=0.35,
                or_lower_complement_confirmed=True,
            ),
        ],
        _snapshot((20.0, 0.63), (21.0, 0.58)),
        min_edge=0.05,
    )

    assert candidate.filter_status == "accepted"
    assert candidate.selected_side == "yes"
    assert candidate.model_yes_probability == 0.42
    assert candidate.edge == 0.07


def test_exact_bucket_is_rejected_not_approximated() -> None:
    [candidate] = build_london_model_candidates(
        [_market(band_type="exact")],
        _snapshot((20.0, 0.63)),
        min_edge=0.05,
    )

    assert candidate.filter_status == "rejected"
    assert candidate.filter_reasons == ("unsupported_exact_bucket",)


def test_candidate_below_min_edge_is_rejected() -> None:
    [candidate] = build_london_model_candidates(
        [_market(yes_ask=0.60, no_ask=0.40)],
        _snapshot((20.0, 0.63)),
        min_edge=0.05,
    )

    assert candidate.filter_status == "rejected"
    assert candidate.filter_reasons == ("edge_below_minimum",)
    assert candidate.selected_side == "yes"
    assert candidate.edge == 0.03


def test_model_probability_is_clipped_to_zero_one() -> None:
    candidates = build_london_model_candidates(
        [
            _market(slug="low-clip", threshold_f=19.0, yes_ask=0.10, no_ask=0.40),
            _market(slug="high-clip", threshold_f=20.0, yes_ask=0.90, no_ask=0.20),
        ],
        _snapshot((19.0, -0.2), (20.0, 1.2)),
        min_edge=0.05,
    )

    by_slug = {candidate.market_slug: candidate for candidate in candidates}
    assert by_slug["low-clip"].model_yes_probability == 0.0
    assert by_slug["low-clip"].selected_side == "no"
    assert by_slug["high-clip"].model_yes_probability == 1.0
    assert by_slug["high-clip"].selected_side == "yes"
