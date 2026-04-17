from __future__ import annotations
import sys, importlib.util, pytest
from pathlib import Path

MODELS_PATH = Path(__file__).resolve().parents[3] / "examples/live/polymarket/sentinel_signal_models.py"

def _load():
    spec = importlib.util.spec_from_file_location("sentinel_signal_models", MODELS_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["sentinel_signal_models"] = m
    spec.loader.exec_module(m)
    return m

def test_round_trip_jsonl():
    m = _load()
    sig = m.SentinelNewsSignal(
        event="sentinel_news_signal", story_id="abc-123", headline="Russia attacks Ukraine",
        category="conflict", market_slug="will-russia-attack-ukraine",
        market_question="Will Russia attack Ukraine?", condition_id="0xdeadbeef",
        yes_token_id="token-yes", no_token_id="token-no",
        instrument_id="PM-WILL-RUSSIA-ATTACK-UKRAINE-YES-token-yes.POLYMARKET",
        direction="YES", relevance_score=0.85,
        market_end_date_iso="2026-12-31T00:00:00+00:00", ts_ns=1_000_000_000,
    )
    d = sig.to_jsonl_dict()
    sig2 = m.SentinelNewsSignal.from_jsonl_dict(d)
    assert sig == sig2

def test_direction_must_be_yes_or_no():
    m = _load()
    with pytest.raises((ValueError, TypeError)):
        m.validate_direction("MAYBE")

def test_relevance_score_bounds():
    m = _load()
    with pytest.raises((ValueError, TypeError)):
        m.validate_relevance_score(1.5)
    with pytest.raises((ValueError, TypeError)):
        m.validate_relevance_score(-0.1)
