from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def _without_repo_root_on_sys_path():
    original = list(sys.path)
    sys.path = [
        entry
        for entry in original
        if Path(entry or ".").resolve() != ROOT
    ]
    try:
        yield
    finally:
        sys.path = original


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        with _without_repo_root_on_sys_path():
            spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


parity = _load_module(
    "examples.live.polymarket.polymarket_crypto_5m_parity",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_crypto_5m_parity.py",
)


def _supportive_session():
    round_start = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)
    market_end = round_start + timedelta(minutes=5)
    return parity.ReplaySession(
        asset="BTC",
        market_slug="btc-updown-5m-1776168000",
        round_start=round_start,
        market_end=market_end,
        winning_side="up",
        snapshots=[
            parity.ReplaySnapshot(
                timestamp=round_start,
                quotes={
                    "up-token": parity.QuoteSnapshot(0.94, 0.95, 18.0, 8.0),
                    "down-token": parity.QuoteSnapshot(0.04, 0.05, 6.0, 18.0),
                },
                reference_mid_price=100.0,
            ),
            parity.ReplaySnapshot(
                timestamp=round_start + timedelta(seconds=2),
                quotes={
                    "up-token": parity.QuoteSnapshot(0.95, 0.96, 18.0, 8.0),
                    "down-token": parity.QuoteSnapshot(0.03, 0.04, 6.0, 18.0),
                },
                reference_mid_price=100.2,
            ),
            parity.ReplaySnapshot(
                timestamp=round_start + timedelta(seconds=4),
                quotes={
                    "up-token": parity.QuoteSnapshot(0.96, 0.97, 19.0, 8.0),
                    "down-token": parity.QuoteSnapshot(0.02, 0.03, 5.0, 19.0),
                },
                reference_mid_price=100.3,
            ),
            parity.ReplaySnapshot(
                timestamp=round_start + timedelta(seconds=151),
                quotes={
                    "up-token": parity.QuoteSnapshot(0.96, 0.97, 19.0, 8.0),
                    "down-token": parity.QuoteSnapshot(0.02, 0.03, 5.0, 19.0),
                },
                reference_mid_price=100.5,
            ),
            parity.ReplaySnapshot(
                timestamp=round_start + timedelta(seconds=170),
                quotes={
                    "up-token": parity.QuoteSnapshot(0.99, 1.00, 20.0, 7.0),
                    "down-token": parity.QuoteSnapshot(0.00, 0.01, 4.0, 20.0),
                },
                reference_mid_price=100.7,
            ),
        ],
    )


def _negative_session():
    round_start = datetime(2026, 4, 14, 13, 0, tzinfo=UTC)
    market_end = round_start + timedelta(minutes=5)
    return parity.ReplaySession(
        asset="BTC",
        market_slug="btc-updown-5m-1776171600",
        round_start=round_start,
        market_end=market_end,
        winning_side="down",
        snapshots=[
            parity.ReplaySnapshot(
                timestamp=round_start,
                quotes={
                    "up-token": parity.QuoteSnapshot(0.94, 0.95, 5.0, 18.0),
                    "down-token": parity.QuoteSnapshot(0.04, 0.05, 18.0, 5.0),
                },
                reference_mid_price=100.0,
            ),
            parity.ReplaySnapshot(
                timestamp=round_start + timedelta(seconds=1),
                quotes={
                    "up-token": parity.QuoteSnapshot(0.93, 0.96, 5.0, 18.0),
                    "down-token": parity.QuoteSnapshot(0.03, 0.04, 18.0, 5.0),
                },
                reference_mid_price=99.9,
            ),
            parity.ReplaySnapshot(
                timestamp=round_start + timedelta(seconds=40),
                quotes={
                    "up-token": parity.QuoteSnapshot(0.60, 0.62, 4.0, 20.0),
                    "down-token": parity.QuoteSnapshot(0.38, 0.40, 20.0, 4.0),
                },
                reference_mid_price=99.2,
            ),
        ],
    )


def test_replay_session_parity_matches_first_wave_presets() -> None:
    report = parity.replay_session_parity(_supportive_session())

    assert report["summary"]["total_presets"] == 7
    assert report["summary"]["matched_entry"] == 7
    assert report["summary"]["matched_exit_reason"] == 7
    assert report["summary"]["matched_outcome_accounting"] == 7
    decisions = {row["strategy_name"]: row for row in report["decisions"]}
    assert decisions["entry_95"]["candidate"]["entered"] is True
    assert decisions["late_half_95"]["candidate"]["entry_timing_bucket"] == 2
    assert decisions["flow_bullish_90"]["reference"]["exit_reason"] == "target_exit"


def test_replay_session_parity_handles_no_entry_paths() -> None:
    report = parity.replay_session_parity(_negative_session())

    decisions = {row["strategy_name"]: row for row in report["decisions"]}
    assert decisions["support_ratio_95"]["reference"]["entered"] is False
    assert decisions["stable_quotes_95"]["candidate"]["entered"] is False
    assert decisions["flow_bullish_90"]["candidate"]["exit_reason"] is None
    assert report["summary"]["matched_entry"] == report["summary"]["total_presets"]


def test_assess_parity_thresholds_flags_mismatch() -> None:
    thresholds = parity.ParityThresholds(
        entry_match_rate=1.0,
        side_match_rate=1.0,
        timing_bucket_match_rate=1.0,
        exit_reason_match_rate=1.0,
        outcome_accounting_match_rate=1.0,
    )
    summary = {
        "total_presets": 2,
        "matched_entry": 2,
        "matched_side": 1,
        "matched_timing_bucket": 2,
        "matched_exit_reason": 2,
        "matched_outcome_accounting": 2,
    }

    assessment = parity.assess_parity(summary=summary, thresholds=thresholds)

    assert assessment["passed"] is False
    assert assessment["metrics"]["side_match_rate"]["passed"] is False
