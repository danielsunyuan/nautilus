from __future__ import annotations
import importlib.util, sys, json, tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

EXAMPLES = Path(__file__).resolve().parents[3] / "examples/live/polymarket"

def _load(name):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

def test_score_market_relevance_exact_keyword():
    _load("sentinel_signal_models")
    m = _load("sentinel_signal_bridge")
    score = m.score_market_relevance(
        story_text="Russia Ukraine conflict escalation",
        market_question="Will Russia invade Ukraine in 2026?",
    )
    assert score > 0.3

def test_score_market_relevance_no_overlap():
    _load("sentinel_signal_models")
    m = _load("sentinel_signal_bridge")
    score = m.score_market_relevance(
        story_text="BTC price rises above 100k",
        market_question="Will Elon Musk become US president?",
    )
    assert score < 0.3

def test_infer_direction_returns_yes_or_no():
    _load("sentinel_signal_models")
    m = _load("sentinel_signal_bridge")
    direction = m.infer_direction(
        story_text="Russia launches major offensive",
        market_question="Will Russia escalate military operations?",
    )
    assert direction in ("YES", "NO")

def test_build_instrument_id_format():
    _load("sentinel_signal_models")
    m = _load("sentinel_signal_bridge")
    iid = m.build_polymarket_instrument_id(
        token_id="abc123",
        market_slug="will-russia-invade-ukraine",
        outcome="YES",
    )
    assert iid.endswith(".POLYMARKET")
    assert "ABC123" in iid

def test_extract_story_text_from_sentinel_response():
    _load("sentinel_signal_models")
    m = _load("sentinel_signal_bridge")
    story = {
        "id": "story-1",
        "title": "Russia Ukraine conflict",
        "news_items": [
            {"content": "Russia launched an attack.", "review": {"title": "Russia attacks"}},
        ],
    }
    text, headline = m.extract_story_text(story)
    assert "Russia" in text
    assert isinstance(headline, str) and headline

def test_filter_markets_by_end_date():
    _load("sentinel_signal_models")
    m = _load("sentinel_signal_bridge")
    now = datetime.now(timezone.utc)
    markets = [
        {"slug": "market-a", "endDate": (now + timedelta(days=1)).isoformat(), "question": "Q1"},
        {"slug": "market-b", "endDate": (now - timedelta(days=1)).isoformat(), "question": "Q2"},
    ]
    active = m.filter_active_markets(markets, now=now)
    assert len(active) == 1
    assert active[0]["slug"] == "market-a"

def test_classify_story_conflict():
    _load("sentinel_signal_models")
    m = _load("sentinel_signal_bridge")
    category = m.classify_story("Russia launched a missile attack on Ukraine troops")
    assert category == "conflict"

def test_process_story_skips_duplicate_story_ids():
    _load("sentinel_signal_models")
    m = _load("sentinel_signal_bridge")
    story = {"id": "dup-1", "title": "Test", "news_items": []}
    emitted = {"dup-1"}
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "signals.jsonl"
        result = m.process_story(
            story=story,
            gamma_base_url="http://localhost:9999",  # offline
            min_relevance=0.1,
            signal_path=path,
            emitted_story_ids=emitted,
        )
    assert result == []
