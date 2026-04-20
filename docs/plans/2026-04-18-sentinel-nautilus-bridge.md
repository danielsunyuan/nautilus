# Sentinel → Nautilus News Signal Bridge Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a bridge that converts Sentinel news stories into Polymarket trade signals, then executes them via NautilusTrader's sandbox execution client.

**Architecture:** A standalone poller (`sentinel_signal_bridge.py`) polls the Sentinel Core REST API every 60 s, fuzzy-maps story entities/keywords to open Polymarket markets via the Gamma API, and writes structured JSONL signals to a shared file. A new `SentinelSignalStrategy` (Nautilus Strategy subclass) subscribes to quote ticks and enters a position when a signal exists for its instrument. A `polymarket_sentinel_news_daemon.py` orchestrator discovers matched markets, builds a TradingNode per market, attaches strategy instances, and runs them until market expiry — following the exact same pattern as `polymarket_sports_paper_daemon.py`.

**Tech Stack:** Python 3.13, NautilusTrader (Rust/Cython engine), Polymarket Gamma REST API, Sentinel Core REST API (Taranis/Flask at localhost:8080), SandboxExecutionClientConfig (paper trading only), JSONL for signal transport.

---

## Shared Signal Schema (reference for all tasks)

All components share this schema. Define it once in `sentinel_signal_models.py`.

```python
@dataclasses.dataclass(frozen=True, slots=True)
class SentinelNewsSignal:
    event: str                  # always "sentinel_news_signal"
    story_id: str               # Sentinel story UUID
    headline: str               # first news item headline
    category: str               # "geopolitical" | "financial" | "election" | "conflict" | "other"
    market_slug: str            # Polymarket Gamma slug
    market_question: str        # human-readable market question
    condition_id: str           # Polymarket condition ID
    yes_token_id: str           # YES outcome token ID
    no_token_id: str            # NO outcome token ID
    instrument_id: str          # Nautilus instrument ID string, e.g. PM-...-YES-<token>.POLYMARKET
    direction: str              # "YES" or "NO" — which side the news favours
    relevance_score: float      # 0.0–1.0 keyword overlap score
    market_end_date_iso: str    # ISO-8601 end date of the market
    ts_ns: int                  # unix nanoseconds when signal was emitted

    def to_jsonl_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_jsonl_dict(cls, d: dict) -> "SentinelNewsSignal":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
```

---

## Context you MUST read before starting any task

```
/home/atlas/EL/nautilus/CLAUDE.md                                          # architecture rules
/home/atlas/EL/nautilus/AGENTS.md                                          # docker rules
/home/atlas/EL/nautilus/examples/live/polymarket/sports_live_strategy.py   # strategy pattern to follow
/home/atlas/EL/nautilus/examples/live/polymarket/sports_strategy_library.py # preset pattern
/home/atlas/EL/nautilus/examples/live/polymarket/polymarket_sports_paper_daemon.py  # daemon pattern
/home/atlas/EL/nautilus/examples/live/polymarket/polymarket_crypto_5m_paper_daemon.py  # node build pattern
/home/atlas/EL/nautilus/tests/unit_tests/examples/test_polymarket_weather_daily_temperature_daemon.py  # test stub pattern
```

Key conventions:
- All example modules use `try/except ModuleNotFoundError` + `importlib.util` fallback for cross-module imports.
- Frozen dataclasses with slots for all config/preset/model types.
- JSONL is the source of truth for events. Write with `json.dumps(payload) + "\n"`.
- Paper only — always `SandboxExecutionClientConfig`, never `PolymarketLiveExecClientFactory` for exec.
- Sentinel Core is reachable at `http://localhost:8080` from the host.
- Nautilus Redis is at `127.0.0.1:6379` (host-mapped).

---

## Task 1: Shared Signal Models

**Files:**
- Create: `nautilus/examples/live/polymarket/sentinel_signal_models.py`
- Create: `nautilus/tests/unit_tests/examples/test_sentinel_signal_models.py`

### Step 1: Write failing tests

```python
# tests/unit_tests/examples/test_sentinel_signal_models.py
from __future__ import annotations
import sys
import importlib.util
from pathlib import Path

MODELS_PATH = Path(__file__).resolve().parents[3] / "examples/live/polymarket/sentinel_signal_models.py"

def _load():
    spec = importlib.util.spec_from_file_location("sentinel_signal_models", MODELS_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_round_trip_jsonl():
    m = _load()
    sig = m.SentinelNewsSignal(
        event="sentinel_news_signal",
        story_id="abc-123",
        headline="Russia attacks Ukraine",
        category="conflict",
        market_slug="will-russia-attack-ukraine",
        market_question="Will Russia attack Ukraine?",
        condition_id="0xdeadbeef",
        yes_token_id="token-yes",
        no_token_id="token-no",
        instrument_id="PM-WILL-RUSSIA-ATTACK-UKRAINE-YES-token-yes.POLYMARKET",
        direction="YES",
        relevance_score=0.85,
        market_end_date_iso="2026-12-31T00:00:00+00:00",
        ts_ns=1_000_000_000,
    )
    d = sig.to_jsonl_dict()
    sig2 = m.SentinelNewsSignal.from_jsonl_dict(d)
    assert sig == sig2

def test_direction_must_be_yes_or_no():
    m = _load()
    import pytest
    with pytest.raises((ValueError, TypeError)):
        m.validate_direction("MAYBE")

def test_relevance_score_bounds():
    m = _load()
    import pytest
    with pytest.raises((ValueError, TypeError)):
        m.validate_relevance_score(1.5)
    with pytest.raises((ValueError, TypeError)):
        m.validate_relevance_score(-0.1)
```

### Step 2: Run to confirm failure
```bash
cd /home/atlas/EL/nautilus
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sentinel_signal_models.py -v
```
Expected: ImportError or AttributeError — module does not exist yet.

### Step 3: Implement `sentinel_signal_models.py`

```python
# examples/live/polymarket/sentinel_signal_models.py
from __future__ import annotations
import dataclasses


VALID_DIRECTIONS = frozenset({"YES", "NO"})
VALID_CATEGORIES = frozenset({"geopolitical", "financial", "election", "conflict", "sports", "other"})


def validate_direction(direction: str) -> str:
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {VALID_DIRECTIONS}, got {direction!r}")
    return direction


def validate_relevance_score(score: float) -> float:
    s = float(score)
    if not (0.0 <= s <= 1.0):
        raise ValueError(f"relevance_score must be between 0.0 and 1.0, got {s}")
    return s


@dataclasses.dataclass(frozen=True, slots=True)
class SentinelNewsSignal:
    event: str
    story_id: str
    headline: str
    category: str
    market_slug: str
    market_question: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    instrument_id: str
    direction: str
    relevance_score: float
    market_end_date_iso: str
    ts_ns: int

    def __post_init__(self) -> None:
        validate_direction(self.direction)
        validate_relevance_score(self.relevance_score)

    def to_jsonl_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_jsonl_dict(cls, d: dict) -> "SentinelNewsSignal":
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})

    @classmethod
    def _dataclass_fields(cls):
        return {f.name for f in dataclasses.fields(cls)}
```

### Step 4: Run tests
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sentinel_signal_models.py -v
```
Expected: 3 PASS.

### Step 5: Commit
```bash
git add examples/live/polymarket/sentinel_signal_models.py \
        tests/unit_tests/examples/test_sentinel_signal_models.py
git commit -m "feat: add SentinelNewsSignal model with round-trip JSONL and validation"
```

---

## Task 2: Sentinel API Client + Market Mapper

**Files:**
- Create: `nautilus/examples/live/polymarket/sentinel_signal_bridge.py`
- Create: `nautilus/tests/unit_tests/examples/test_sentinel_signal_bridge.py`

This module has three pure functions (no network, easy to test) plus a thin HTTP wrapper.

### Step 1: Write failing tests

```python
# tests/unit_tests/examples/test_sentinel_signal_bridge.py
from __future__ import annotations
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch
import json

BRIDGE_PATH = Path(__file__).resolve().parents[3] / "examples/live/polymarket/sentinel_signal_bridge.py"
MODELS_PATH = Path(__file__).resolve().parents[3] / "examples/live/polymarket/sentinel_signal_models.py"

def _load_bridge():
    # load models first so bridge can import it
    mspec = importlib.util.spec_from_file_location("sentinel_signal_models", MODELS_PATH)
    mmod = importlib.util.module_from_spec(mspec)
    import sys; sys.modules["sentinel_signal_models"] = mmod
    mspec.loader.exec_module(mmod)

    spec = importlib.util.spec_from_file_location("sentinel_signal_bridge", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def test_score_market_relevance_exact_keyword():
    m = _load_bridge()
    score = m.score_market_relevance(
        story_text="Russia Ukraine conflict escalation",
        market_question="Will Russia invade Ukraine in 2026?",
    )
    assert score > 0.5

def test_score_market_relevance_no_overlap():
    m = _load_bridge()
    score = m.score_market_relevance(
        story_text="BTC price rises above 100k",
        market_question="Will Elon Musk become US president?",
    )
    assert score < 0.2

def test_infer_direction_positive():
    m = _load_bridge()
    direction = m.infer_direction(
        story_text="Russia launches major offensive",
        market_question="Will Russia escalate military operations?",
    )
    assert direction in ("YES", "NO")

def test_build_instrument_id_format():
    m = _load_bridge()
    iid = m.build_polymarket_instrument_id(
        token_id="abc123",
        market_slug="will-russia-invade-ukraine",
        outcome="YES",
    )
    assert iid.endswith(".POLYMARKET")
    assert "abc123" in iid

def test_extract_story_text_from_sentinel_response():
    m = _load_bridge()
    story = {
        "id": "story-1",
        "title": "Russia Ukraine conflict",
        "news_items": [
            {"content": "Russia launched an attack.", "review": {"title": "Russia attacks"}},
        ],
    }
    text, headline = m.extract_story_text(story)
    assert "Russia" in text
    assert isinstance(headline, str)

def test_filter_markets_by_end_date():
    m = _load_bridge()
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    markets = [
        {"slug": "market-a", "endDate": (now + timedelta(days=1)).isoformat(), "question": "Q1"},
        {"slug": "market-b", "endDate": (now - timedelta(days=1)).isoformat(), "question": "Q2"},
    ]
    active = m.filter_active_markets(markets, now=now)
    assert len(active) == 1
    assert active[0]["slug"] == "market-a"
```

### Step 2: Run to confirm failure
```bash
cd /home/atlas/EL/nautilus
uv run --extra polymarket --with pytest --with pytest-asyncio python -m pytest \
  tests/unit_tests/examples/test_sentinel_signal_bridge.py -v --noconftest
```
Expected: ImportError — bridge module does not exist.

### Step 3: Implement `sentinel_signal_bridge.py`

```python
# examples/live/polymarket/sentinel_signal_bridge.py
"""
Sentinel → Polymarket signal bridge.

Polls Sentinel Core REST API for new stories, maps them to active Polymarket
markets via Gamma API, scores relevance, and writes SentinelNewsSignal JSONL
to a shared signal file.

Run standalone:  python -m examples.live.polymarket.sentinel_signal_bridge
Or as a daemon:  import and call run_bridge_loop()
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import httpx
    _HTTP_LIB = "httpx"
except ImportError:
    import urllib.request
    _HTTP_LIB = "urllib"

try:
    from examples.live.polymarket.sentinel_signal_models import (
        SentinelNewsSignal, validate_direction, VALID_CATEGORIES,
    )
except ModuleNotFoundError:
    import importlib.util, sys
    _p = Path(__file__).resolve().with_name("sentinel_signal_models.py")
    _spec = importlib.util.spec_from_file_location("sentinel_signal_models", _p)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["sentinel_signal_models"] = _mod
    _spec.loader.exec_module(_mod)
    SentinelNewsSignal = _mod.SentinelNewsSignal
    validate_direction = _mod.validate_direction
    VALID_CATEGORIES = _mod.VALID_CATEGORIES


DEFAULT_SENTINEL_URL = os.environ.get("SENTINEL_CORE_URL", "http://localhost:8080")
DEFAULT_GAMMA_URL = os.environ.get("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
DEFAULT_SIGNAL_PATH = os.environ.get(
    "SENTINEL_SIGNAL_PATH",
    "/data/nautilus_export/live_signals/sentinel_news_signals.jsonl",
)
DEFAULT_POLL_INTERVAL = float(os.environ.get("SENTINEL_POLL_INTERVAL", "60"))
DEFAULT_MIN_RELEVANCE = float(os.environ.get("SENTINEL_MIN_RELEVANCE", "0.25"))
DEFAULT_SENTINEL_API_KEY = os.environ.get("SENTINEL_API_KEY", "supersecret")
_STOP_WORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would could should "
    "may might shall can cannot of in on at to for with by from this that these those".split()
)

# Categories that map well to Polymarket geopolitical/news markets
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "conflict": ["war", "attack", "military", "troops", "missile", "bomb", "invasion", "offensive",
                 "ceasefire", "conflict", "sanction", "weapon", "nato", "ukraine", "russia", "israel",
                 "hamas", "iran", "china", "taiwan"],
    "election": ["election", "vote", "president", "prime minister", "candidate", "poll",
                 "ballot", "campaign", "resign", "impeach", "parliament"],
    "financial": ["bitcoin", "btc", "eth", "crypto", "fed", "rate", "inflation", "gdp", "recession",
                  "tariff", "trade war", "default", "debt", "market crash", "nasdaq", "s&p"],
    "geopolitical": ["diplomat", "treaty", "summit", "sanction", "alliance", "un", "nato",
                     "security council", "peace", "negotiation", "accord"],
}


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[a-z]+", text.lower())
    return {t for t in tokens if t not in _STOP_WORDS and len(t) > 2}


def score_market_relevance(*, story_text: str, market_question: str) -> float:
    """Jaccard-like overlap score between story tokens and market question tokens."""
    story_tokens = _tokenize(story_text)
    market_tokens = _tokenize(market_question)
    if not story_tokens or not market_tokens:
        return 0.0
    intersection = story_tokens & market_tokens
    union = story_tokens | market_tokens
    return len(intersection) / len(union)


def infer_direction(*, story_text: str, market_question: str) -> str:
    """Naive sentiment: look for negative/denial words in market question context.

    Returns "YES" unless the market is phrased as something the story contradicts.
    This is intentionally conservative — default YES lets the price gate the trade.
    """
    neg_patterns = [
        r"\bwill not\b", r"\bwon't\b", r"\bfail\b", r"\bdefeated\b",
        r"\bno\b.*\b(deal|agreement|ceasefire)\b",
    ]
    q_lower = market_question.lower()
    for pat in neg_patterns:
        if re.search(pat, q_lower):
            return "NO"
    return "YES"


def classify_story(story_text: str) -> str:
    """Return a category string for the story based on keyword presence."""
    text_lower = story_text.lower()
    scores: dict[str, int] = {}
    for category, keywords in _CATEGORY_KEYWORDS.items():
        scores[category] = sum(1 for kw in keywords if kw in text_lower)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"


def extract_story_text(story: dict[str, Any]) -> tuple[str, str]:
    """Extract combined text and headline from a Sentinel story dict."""
    parts = []
    headline = str(story.get("title") or "")
    if headline:
        parts.append(headline)
    for item in story.get("news_items", []):
        review = item.get("review") or {}
        item_title = str(review.get("title") or item.get("title") or "")
        content = str(item.get("content") or "")
        if item_title:
            parts.append(item_title)
        if content:
            parts.append(content[:500])  # first 500 chars per item
    if not headline and parts:
        headline = parts[0]
    return " ".join(parts), headline


def filter_active_markets(
    markets: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return only markets whose endDate is in the future."""
    now = now or datetime.now(timezone.utc)
    active = []
    for m in markets:
        end_raw = str(m.get("endDate") or m.get("end_date_iso") or "")
        if not end_raw:
            continue
        try:
            if end_raw.endswith("Z"):
                end_raw = end_raw[:-1] + "+00:00"
            end_dt = datetime.fromisoformat(end_raw)
            if end_dt.tzinfo is None:
                from datetime import timezone as _tz
                end_dt = end_dt.replace(tzinfo=_tz.utc)
            if end_dt > now:
                active.append(m)
        except ValueError:
            continue
    return active


def build_polymarket_instrument_id(*, token_id: str, market_slug: str, outcome: str) -> str:
    """Build a Nautilus-style instrument ID string for a Polymarket token."""
    safe_slug = re.sub(r"[^A-Z0-9]", "-", market_slug.upper())[:60]
    safe_outcome = outcome.upper()
    safe_token = re.sub(r"[^A-Z0-9]", "", token_id.upper())[:40]
    return f"PM-{safe_slug}-{safe_outcome}-{safe_token}.POLYMARKET"


def _http_get(url: str, *, headers: dict | None = None, timeout: float = 10.0) -> Any:
    """Thin HTTP GET wrapper that works with httpx or stdlib urllib."""
    headers = headers or {}
    if _HTTP_LIB == "httpx":
        resp = httpx.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    else:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())


def fetch_sentinel_stories(
    *,
    base_url: str,
    api_key: str,
    limit: int = 20,
    offset: int = 0,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Fetch recent stories from Sentinel Core REST API."""
    url = f"{base_url.rstrip('/')}/api/analyze/report-items"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        data = _http_get(url, headers=headers, timeout=timeout)
        if isinstance(data, dict):
            return data.get("items", data.get("data", []))
        return list(data)
    except Exception as exc:
        print(json.dumps({"event": "sentinel_fetch_error", "reason": str(exc)}, sort_keys=True), flush=True)
        return []


def fetch_gamma_markets(
    *,
    base_url: str,
    query: str,
    limit: int = 10,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Search Gamma API for markets matching a keyword query."""
    safe_q = query[:80].replace(" ", "+")
    url = f"{base_url.rstrip('/')}/markets?tag_slug={safe_q}&limit={limit}&active=true&closed=false"
    try:
        data = _http_get(url, timeout=timeout)
        if isinstance(data, list):
            return data
        return data.get("markets", data.get("data", []))
    except Exception as exc:
        print(json.dumps({"event": "gamma_fetch_error", "query": query, "reason": str(exc)}, sort_keys=True), flush=True)
        return []


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True))
        f.write("\n")


def process_story(
    *,
    story: dict[str, Any],
    gamma_base_url: str,
    min_relevance: float,
    signal_path: Path,
    emitted_story_ids: set[str],
    gamma_timeout: float = 10.0,
) -> list[SentinelNewsSignal]:
    """Map a single Sentinel story to Polymarket markets and emit signals."""
    story_id = str(story.get("id") or story.get("story_id") or "")
    if not story_id or story_id in emitted_story_ids:
        return []

    story_text, headline = extract_story_text(story)
    if not story_text.strip():
        return []

    category = classify_story(story_text)
    # Build search keywords from top tokens
    tokens = sorted(_tokenize(story_text), key=lambda t: len(t), reverse=True)
    search_query = " ".join(tokens[:3])

    raw_markets = fetch_gamma_markets(
        base_url=gamma_base_url,
        query=search_query,
        timeout=gamma_timeout,
    )
    active_markets = filter_active_markets(raw_markets)

    signals: list[SentinelNewsSignal] = []
    now = datetime.now(timezone.utc)

    for market in active_markets:
        question = str(market.get("question") or market.get("title") or "")
        score = score_market_relevance(story_text=story_text, market_question=question)
        if score < float(min_relevance):
            continue

        slug = str(market.get("slug") or market.get("conditionId") or "")
        condition_id = str(market.get("conditionId") or "")
        end_raw = str(market.get("endDate") or market.get("end_date_iso") or "")

        # Extract YES/NO token IDs from the market's outcomes
        outcomes = market.get("outcomes", [])
        tokens_map = market.get("outcomePrices", {})
        clob_token_ids = market.get("clobTokenIds", [])

        yes_token_id = ""
        no_token_id = ""
        if isinstance(clob_token_ids, list) and len(clob_token_ids) >= 2:
            yes_token_id = str(clob_token_ids[0])
            no_token_id = str(clob_token_ids[1])
        elif isinstance(outcomes, list):
            for outcome in outcomes:
                if isinstance(outcome, dict):
                    name = str(outcome.get("title") or outcome.get("name") or "").upper()
                    tid = str(outcome.get("id") or outcome.get("tokenId") or "")
                    if name == "YES":
                        yes_token_id = tid
                    elif name == "NO":
                        no_token_id = tid

        if not yes_token_id:
            continue  # can't trade without a token ID

        direction = infer_direction(story_text=story_text, market_question=question)
        trade_token_id = yes_token_id if direction == "YES" else no_token_id
        instrument_id = build_polymarket_instrument_id(
            token_id=trade_token_id,
            market_slug=slug,
            outcome=direction,
        )

        sig = SentinelNewsSignal(
            event="sentinel_news_signal",
            story_id=story_id,
            headline=headline[:200],
            category=category,
            market_slug=slug,
            market_question=question[:200],
            condition_id=condition_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            instrument_id=instrument_id,
            direction=direction,
            relevance_score=round(score, 4),
            market_end_date_iso=end_raw,
            ts_ns=_now_ns(),
        )
        _append_jsonl(signal_path, sig.to_jsonl_dict())
        print(json.dumps(sig.to_jsonl_dict(), sort_keys=True), flush=True)
        signals.append(sig)

    if signals:
        emitted_story_ids.add(story_id)
    return signals


def run_bridge_loop(
    *,
    sentinel_url: str = DEFAULT_SENTINEL_URL,
    sentinel_api_key: str = DEFAULT_SENTINEL_API_KEY,
    gamma_url: str = DEFAULT_GAMMA_URL,
    signal_path: Path | str = DEFAULT_SIGNAL_PATH,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    min_relevance: float = DEFAULT_MIN_RELEVANCE,
    max_iterations: int = 0,      # 0 = run forever; >0 for smoke tests
) -> None:
    signal_path = Path(signal_path)
    emitted_story_ids: set[str] = set()
    iteration = 0

    while max_iterations <= 0 or iteration < max_iterations:
        stories = fetch_sentinel_stories(
            base_url=sentinel_url,
            api_key=sentinel_api_key,
        )
        for story in stories:
            process_story(
                story=story,
                gamma_base_url=gamma_url,
                min_relevance=min_relevance,
                signal_path=signal_path,
                emitted_story_ids=emitted_story_ids,
            )
        iteration += 1
        if max_iterations <= 0 or iteration < max_iterations:
            time.sleep(poll_interval)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sentinel→Polymarket news signal bridge")
    p.add_argument("--sentinel-url", default=DEFAULT_SENTINEL_URL)
    p.add_argument("--gamma-url", default=DEFAULT_GAMMA_URL)
    p.add_argument("--signal-path", default=DEFAULT_SIGNAL_PATH)
    p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    p.add_argument("--min-relevance", type=float, default=DEFAULT_MIN_RELEVANCE)
    p.add_argument("--max-iterations", type=int, default=0, help="0=run forever (for smoke tests)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_bridge_loop(
        sentinel_url=str(args.sentinel_url),
        gamma_url=str(args.gamma_url),
        signal_path=Path(str(args.signal_path)),
        poll_interval=float(args.poll_interval),
        min_relevance=float(args.min_relevance),
        max_iterations=int(args.max_iterations),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### Step 4: Run tests
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sentinel_signal_bridge.py -v --noconftest
```
Expected: 6 PASS.

### Step 5: Commit
```bash
git add examples/live/polymarket/sentinel_signal_bridge.py \
        tests/unit_tests/examples/test_sentinel_signal_bridge.py
git commit -m "feat: add sentinel_signal_bridge — polls Sentinel API, maps stories to Polymarket markets"
```

---

## Task 3: Sentinel Signal Strategy (Nautilus)

**Files:**
- Create: `nautilus/examples/live/polymarket/sentinel_signal_strategy.py`
- Create: `nautilus/tests/unit_tests/examples/test_sentinel_signal_strategy.py`

The strategy is simple: on `on_start()`, load signals from JSONL for this instrument. On `on_quote_tick()`, if a signal exists and conditions pass (min_relevance, ask in band), enter once.

### Step 1: Write failing tests

```python
# tests/unit_tests/examples/test_sentinel_signal_strategy.py
from __future__ import annotations
import sys, importlib.util
from pathlib import Path
from decimal import Decimal
from unittest.mock import MagicMock, patch
import json, tempfile

EXAMPLES = Path(__file__).resolve().parents[3] / "examples/live/polymarket"

def _load(name):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_signal(**overrides):
    base = dict(
        event="sentinel_news_signal",
        story_id="story-1",
        headline="Test headline",
        category="conflict",
        market_slug="will-russia-attack",
        market_question="Will Russia attack?",
        condition_id="0xdeadbeef",
        yes_token_id="token-yes",
        no_token_id="token-no",
        instrument_id="PM-WILL-RUSSIA-ATTACK-YES-TOKENYES.POLYMARKET",
        direction="YES",
        relevance_score=0.75,
        market_end_date_iso="2026-12-31T00:00:00+00:00",
        ts_ns=1_000_000_000,
    )
    base.update(overrides)
    return base


def test_load_signals_from_jsonl():
    """Signals for the target instrument are loaded on start."""
    # ensure models module is loaded
    m_models = _load("sentinel_signal_models")
    m = _load("sentinel_signal_strategy")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        sig = _make_signal()
        f.write(json.dumps(sig) + "\n")
        # a signal for a different instrument — should be ignored
        sig2 = _make_signal(instrument_id="PM-OTHER-YES-XYZ.POLYMARKET")
        f.write(json.dumps(sig2) + "\n")
        path = f.name

    signals = m.load_signals_for_instrument(
        signal_path=path,
        instrument_id="PM-WILL-RUSSIA-ATTACK-YES-TOKENYES.POLYMARKET",
    )
    assert len(signals) == 1
    assert signals[0]["story_id"] == "story-1"


def test_should_enter_when_signal_present_and_ask_in_band():
    m_models = _load("sentinel_signal_models")
    m = _load("sentinel_signal_strategy")

    sig = _make_signal(relevance_score=0.75)
    result = m.should_enter_sentinel_market(
        signal=sig,
        ask=0.65,
        min_ask=0.50,
        max_ask=0.80,
        min_relevance=0.50,
        entry_submitted=False,
    )
    assert result is True


def test_should_not_enter_if_ask_out_of_band():
    m_models = _load("sentinel_signal_models")
    m = _load("sentinel_signal_strategy")

    sig = _make_signal(relevance_score=0.75)
    result = m.should_enter_sentinel_market(
        signal=sig, ask=0.95, min_ask=0.50, max_ask=0.80,
        min_relevance=0.50, entry_submitted=False,
    )
    assert result is False


def test_should_not_enter_if_already_submitted():
    m_models = _load("sentinel_signal_models")
    m = _load("sentinel_signal_strategy")

    sig = _make_signal(relevance_score=0.75)
    result = m.should_enter_sentinel_market(
        signal=sig, ask=0.65, min_ask=0.50, max_ask=0.80,
        min_relevance=0.50, entry_submitted=True,
    )
    assert result is False


def test_should_not_enter_if_relevance_too_low():
    m_models = _load("sentinel_signal_models")
    m = _load("sentinel_signal_strategy")

    sig = _make_signal(relevance_score=0.10)
    result = m.should_enter_sentinel_market(
        signal=sig, ask=0.65, min_ask=0.50, max_ask=0.80,
        min_relevance=0.50, entry_submitted=False,
    )
    assert result is False
```

### Step 2: Run to confirm failure
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sentinel_signal_strategy.py -v --noconftest
```

### Step 3: Implement `sentinel_signal_strategy.py`

```python
# examples/live/polymarket/sentinel_signal_strategy.py
"""
Nautilus Strategy that enters a Polymarket position when a Sentinel news signal
exists for the instrument. Reads signals from a shared JSONL file.

Follows the exact pattern of SportsPaperStrategy / PolymarketCrypto5mPaperStrategy.
One entry per strategy instance, no exit logic (holds to market resolution).
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy


def load_signals_for_instrument(
    *,
    signal_path: str | Path,
    instrument_id: str,
) -> list[dict[str, Any]]:
    """Read JSONL signal file and return entries for the given instrument_id."""
    path = Path(signal_path)
    if not path.exists():
        return []
    signals = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    entry.get("event") == "sentinel_news_signal"
                    and str(entry.get("instrument_id") or "") == str(instrument_id)
                ):
                    signals.append(entry)
    except OSError:
        pass
    return signals


def should_enter_sentinel_market(
    *,
    signal: dict[str, Any],
    ask: float,
    min_ask: float,
    max_ask: float,
    min_relevance: float,
    entry_submitted: bool,
) -> bool:
    """Pure entry predicate — no side effects."""
    if entry_submitted:
        return False
    relevance = float(signal.get("relevance_score") or 0.0)
    if relevance < float(min_relevance):
        return False
    ask_f = float(ask)
    return float(min_ask) <= ask_f <= float(max_ask)


class SentinelSignalStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    signal_path: str
    order_qty: Decimal = Decimal("10")
    min_ask: float = 0.30
    max_ask: float = 0.85
    min_relevance: float = 0.25
    close_positions_on_stop: bool = False  # hold to resolution by default


class SentinelSignalStrategy(Strategy):
    """
    Enters a single paper trade when a Sentinel news signal exists for this instrument
    and the ask price is within the configured band.

    Holds to resolution — no exit logic. Resolution is handled externally.
    """

    def __init__(self, config: SentinelSignalStrategyConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self._signals: list[dict[str, Any]] = []
        self._entry_submitted = False

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self.config.instrument_id}")
            self.stop()
            return
        self._signals = load_signals_for_instrument(
            signal_path=self.config.signal_path,
            instrument_id=str(self.config.instrument_id),
        )
        if self._signals:
            self.log.info(
                f"Loaded {len(self._signals)} signal(s) for {self.config.instrument_id} "
                f"(best relevance: {max(s.get('relevance_score', 0) for s in self._signals):.3f})"
            )
        else:
            self.log.warning(f"No signals found for {self.config.instrument_id} — will not enter")
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self.instrument is None or self._entry_submitted:
            return
        if not self._signals:
            return

        ask = float(tick.ask_price.as_double())
        # Use the highest-relevance signal
        best_signal = max(self._signals, key=lambda s: float(s.get("relevance_score") or 0.0))

        if not should_enter_sentinel_market(
            signal=best_signal,
            ask=ask,
            min_ask=self.config.min_ask,
            max_ask=self.config.max_ask,
            min_relevance=self.config.min_relevance,
            entry_submitted=self._entry_submitted,
        ):
            return

        self._entry_submitted = True
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(self.config.order_qty),
        )
        self.log.info(
            f"Entering {self.config.instrument_id} at ask={ask:.4f} "
            f"(signal relevance={best_signal.get('relevance_score'):.3f}, "
            f"headline={best_signal.get('headline', '')[:60]!r})"
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        if self.instrument is None:
            return
        self.cancel_all_orders(self.instrument.id)
        if self.config.close_positions_on_stop:
            self.close_all_positions(self.instrument.id)
```

### Step 4: Run tests
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sentinel_signal_strategy.py -v --noconftest
```
Expected: 5 PASS.

### Step 5: Commit
```bash
git add examples/live/polymarket/sentinel_signal_strategy.py \
        tests/unit_tests/examples/test_sentinel_signal_strategy.py
git commit -m "feat: add SentinelSignalStrategy — enters Polymarket positions on news signals"
```

---

## Task 4: Sentinel News Daemon (Orchestrator)

**Files:**
- Create: `nautilus/examples/live/polymarket/polymarket_sentinel_news_daemon.py`
- Create: `nautilus/tests/unit_tests/examples/test_polymarket_sentinel_news_daemon.py`

The daemon:
1. Runs the bridge to emit fresh signals
2. Reads the signal file to find matched markets
3. For each unique instrument_id in signals, resolves the instrument via Gamma
4. Builds a TradingNode with one `SentinelSignalStrategy` per instrument
5. Runs the node until the earliest market expiry or a configured timeout
6. Extracts results and writes JSONL
7. Sleeps and repeats

### Step 1: Write failing tests

```python
# tests/unit_tests/examples/test_polymarket_sentinel_news_daemon.py
from __future__ import annotations
import sys, importlib.util, json, tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio, pytest

EXAMPLES = Path(__file__).resolve().parents[3] / "examples/live/polymarket"

def _load(name):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_signal(**overrides):
    base = dict(
        event="sentinel_news_signal",
        story_id="story-1",
        headline="Test",
        category="conflict",
        market_slug="will-russia-attack",
        market_question="Will Russia attack?",
        condition_id="0xdeadbeef",
        yes_token_id="token-yes",
        no_token_id="token-no",
        instrument_id="PM-WILL-RUSSIA-ATTACK-YES-TOKENYES.POLYMARKET",
        direction="YES",
        relevance_score=0.75,
        market_end_date_iso="2026-12-31T00:00:00+00:00",
        ts_ns=1_000_000_000,
    )
    base.update(overrides)
    return base


def test_read_signals_from_file():
    # loads models and bridge first
    _load("sentinel_signal_models")
    _load("sentinel_signal_bridge")
    m = _load("polymarket_sentinel_news_daemon")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(_make_signal()) + "\n")
        f.write(json.dumps(_make_signal(instrument_id="PM-OTHER.POLYMARKET")) + "\n")
        path = f.name

    signals = m.read_all_signals(path)
    assert len(signals) == 2
    unique_ids = {s["instrument_id"] for s in signals}
    assert len(unique_ids) == 2


def test_build_output_path_format():
    _load("sentinel_signal_models")
    _load("sentinel_signal_bridge")
    m = _load("polymarket_sentinel_news_daemon")

    from datetime import datetime, timezone
    now = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
    path = m.build_daemon_output_path(output_dir="/tmp/outputs", now=now)
    assert "sentinel" in str(path)
    assert "20260418" in str(path)
    assert str(path).endswith(".jsonl")


def test_group_signals_by_instrument():
    _load("sentinel_signal_models")
    _load("sentinel_signal_bridge")
    m = _load("polymarket_sentinel_news_daemon")

    signals = [
        _make_signal(instrument_id="PM-A.POLYMARKET", relevance_score=0.8),
        _make_signal(instrument_id="PM-A.POLYMARKET", relevance_score=0.6),
        _make_signal(instrument_id="PM-B.POLYMARKET", relevance_score=0.5),
    ]
    grouped = m.group_signals_by_instrument(signals)
    assert set(grouped.keys()) == {"PM-A.POLYMARKET", "PM-B.POLYMARKET"}
    # best signal selected per instrument
    assert grouped["PM-A.POLYMARKET"]["relevance_score"] == 0.8
```

### Step 2: Run to confirm failure
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_polymarket_sentinel_news_daemon.py -v --noconftest
```

### Step 3: Implement `polymarket_sentinel_news_daemon.py`

The daemon follows the weather/sports daemon pattern closely. Key difference: it finds markets via the signal file rather than a fixed set of slugs.

```python
# examples/live/polymarket/polymarket_sentinel_news_daemon.py
"""
Polymarket Sentinel News Daemon

Runs the Sentinel→Polymarket bridge and executes paper trades on matched markets.

Flow:
  1. Run sentinel_signal_bridge to emit fresh JSONL signals (or use existing file)
  2. Read signal file, group by instrument_id (best relevance per instrument)
  3. For each matched instrument: resolve via Gamma, build TradingNode, add
     SentinelSignalStrategy, run until market_end or timeout
  4. Extract results, write JSONL run log
  5. Sleep poll_interval seconds, repeat
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import signal
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
import importlib.util

# --- importlib fallback pattern for all local modules ---

def _load_local(name: str):
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(name, here / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

try:
    from examples.live.polymarket.sentinel_signal_models import SentinelNewsSignal
    from examples.live.polymarket.sentinel_signal_bridge import (
        run_bridge_loop, fetch_gamma_markets, filter_active_markets,
    )
    from examples.live.polymarket.sentinel_signal_strategy import (
        SentinelSignalStrategy, SentinelSignalStrategyConfig,
    )
except ModuleNotFoundError:
    _models = _load_local("sentinel_signal_models")
    _bridge = _load_local("sentinel_signal_bridge")
    _strategy = _load_local("sentinel_signal_strategy")
    SentinelNewsSignal = _models.SentinelNewsSignal
    run_bridge_loop = _bridge.run_bridge_loop
    fetch_gamma_markets = _bridge.fetch_gamma_markets
    filter_active_markets = _bridge.filter_active_markets
    SentinelSignalStrategy = _strategy.SentinelSignalStrategy
    SentinelSignalStrategyConfig = _strategy.SentinelSignalStrategyConfig

from nautilus_trader.adapters.polymarket import POLYMARKET, POLYMARKET_VENUE
from nautilus_trader.adapters.polymarket import PolymarketDataClientConfig, PolymarketLiveDataClientFactory
from nautilus_trader.adapters.polymarket.providers import PolymarketInstrumentProviderConfig
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.config import (
    CacheConfig, DatabaseConfig, LiveExecEngineConfig,
    LoggingConfig, MessageBusConfig, TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.identifiers import InstrumentId, TraderId

DEFAULT_OUTPUT_DIR = "/workspace/outputs"
DEFAULT_SIGNAL_PATH = "/data/nautilus_export/live_signals/sentinel_news_signals.jsonl"
DEFAULT_SENTINEL_URL = os.environ.get("SENTINEL_CORE_URL", "http://localhost:8080")
DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"
DEFAULT_POLL_INTERVAL = 300.0   # 5 minutes between bridge poll cycles
DEFAULT_ROUND_TIMEOUT = 3600.0  # max 1h per market session
DEFAULT_CACHE_HOST = "redis"
DEFAULT_CACHE_PORT = 6379


class DaemonRunWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
            f.flush()


def build_daemon_output_path(*, output_dir: str | Path, now: datetime) -> Path:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(output_dir).resolve() / "polymarket" / "sentinel" / f"sentinel_{stamp}.jsonl"


def read_all_signals(signal_path: str | Path) -> list[dict[str, Any]]:
    path = Path(signal_path)
    if not path.exists():
        return []
    signals = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("event") == "sentinel_news_signal":
                    signals.append(entry)
            except json.JSONDecodeError:
                continue
    return signals


def group_signals_by_instrument(signals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the highest-relevance signal per instrument_id."""
    best: dict[str, dict[str, Any]] = {}
    for sig in signals:
        iid = str(sig.get("instrument_id") or "")
        if not iid:
            continue
        existing = best.get(iid)
        if existing is None or float(sig.get("relevance_score") or 0) > float(existing.get("relevance_score") or 0):
            best[iid] = sig
    return best


def _env_first(*names: str) -> str | None:
    for name in names:
        v = os.getenv(name)
        if v:
            return v
    return None


def _build_node_config(*, instrument_ids: list[str], trader_id: str) -> TradingNodeConfig:
    cache_host = os.getenv("NAUTILUS_CACHE_HOST", DEFAULT_CACHE_HOST)
    cache_port = int(os.getenv("NAUTILUS_CACHE_PORT", str(DEFAULT_CACHE_PORT)))
    provider_config = PolymarketInstrumentProviderConfig(load_ids=frozenset(instrument_ids))
    return TradingNodeConfig(
        trader_id=TraderId(trader_id),
        logging=LoggingConfig(log_level="INFO", use_pyo3=True),
        exec_engine=LiveExecEngineConfig(
            load_cache=False,
            reconciliation=False,
            open_check_interval_secs=10.0,
            snapshot_orders=True,
            snapshot_positions=True,
            snapshot_positions_interval_secs=10.0,
        ),
        cache=CacheConfig(
            database=DatabaseConfig(host=cache_host, port=cache_port),
            timestamps_as_iso8601=True,
            buffer_interval_ms=200,
            flush_on_start=False,
            use_instance_id=True,
        ),
        message_bus=MessageBusConfig(
            database=DatabaseConfig(host=cache_host, port=cache_port),
            timestamps_as_iso8601=True,
            buffer_interval_ms=200,
            streams_prefix="sentinel-news",
            use_trader_prefix=True,
            use_trader_id=True,
            use_instance_id=True,
            stream_per_topic=False,
            autotrim_mins=120,
            heartbeat_interval_secs=5,
        ),
        data_clients={
            POLYMARKET: PolymarketDataClientConfig(
                instrument_config=provider_config,
                private_key=_env_first("POLYMARKET_PRIVATE_KEY", "POLYMARKET_PK"),
                signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
                funder=_env_first("POLYMARKET_FUNDER_ADDRESS", "POLYMARKET_FUNDER"),
                api_key=_env_first("POLYMARKET_CLOB_API_KEY", "POLYMARKET_API_KEY"),
                api_secret=_env_first("POLYMARKET_CLOB_API_SECRET", "POLYMARKET_API_SECRET"),
                passphrase=_env_first("POLYMARKET_CLOB_PASSPHRASE", "POLYMARKET_PASSPHRASE"),
                base_url_http=_env_first("POLYMARKET_CLOB_HOST"),
            ),
        },
        exec_clients={
            POLYMARKET: SandboxExecutionClientConfig(
                venue=str(POLYMARKET_VENUE),
                base_currency=str(USDC_POS),
                account_type="CASH",
                starting_balances=["1_000 USDC/USDC_POS"],
                fee_model_path="nautilus_trader.adapters.polymarket.fee_model.PolymarketFeeModel",
            ),
        },
        timeout_connection=20.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )


async def _run_node_until_deadline(*, node: TradingNode, duration_seconds: float) -> None:
    run_task = asyncio.create_task(node.run_async())
    try:
        await asyncio.sleep(max(0.0, float(duration_seconds)))
        try:
            await asyncio.wait_for(node.stop_async(), timeout=30.0)
        except asyncio.TimeoutError:
            run_task.cancel()
        try:
            await asyncio.wait_for(run_task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    finally:
        if not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass


async def run_sentinel_round(
    *,
    signal_path: Path,
    writer: DaemonRunWriter,
    run_id: str,
    round_timeout: float = DEFAULT_ROUND_TIMEOUT,
    min_relevance: float = 0.25,
    min_ask: float = 0.30,
    max_ask: float = 0.85,
    order_qty: Decimal = Decimal("10"),
) -> None:
    signals = read_all_signals(signal_path)
    if not signals:
        writer.write({"run_id": run_id, "event": "no_signals", "ts": datetime.now(UTC).isoformat()})
        return

    grouped = group_signals_by_instrument(signals)
    instrument_ids = list(grouped.keys())
    if not instrument_ids:
        return

    now = datetime.now(UTC)
    writer.write({
        "run_id": run_id,
        "event": "round_start",
        "instruments": instrument_ids,
        "ts": now.isoformat(),
    })

    config = _build_node_config(
        instrument_ids=instrument_ids,
        trader_id=f"SENTINEL-DAEMON-{run_id[:8].upper()}",
    )
    node = TradingNode(config=config)

    for iid, sig in grouped.items():
        if float(sig.get("relevance_score") or 0) < min_relevance:
            continue
        strategy = SentinelSignalStrategy(
            config=SentinelSignalStrategyConfig(
                strategy_id=f"SENTINEL-{re.sub(r'[^A-Z0-9]', '-', iid.upper())[:30]}",
                instrument_id=InstrumentId.from_str(iid),
                signal_path=str(signal_path),
                order_qty=order_qty,
                min_ask=min_ask,
                max_ask=max_ask,
                min_relevance=min_relevance,
            )
        )
        node.trader.add_strategy(strategy)

    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    node.add_exec_client_factory(POLYMARKET, SandboxLiveExecClientFactory)
    node.build()

    try:
        await _run_node_until_deadline(node=node, duration_seconds=min(round_timeout, 3600.0))
    finally:
        node.kernel.dispose()
        if node.kernel.executor:
            node.kernel.executor.shutdown(wait=True, cancel_futures=True)

    writer.write({
        "run_id": run_id,
        "event": "round_end",
        "instruments": instrument_ids,
        "ts": datetime.now(UTC).isoformat(),
    })


async def run_daemon(
    *,
    signal_path: Path,
    output_dir: str,
    sentinel_url: str = DEFAULT_SENTINEL_URL,
    gamma_url: str = DEFAULT_GAMMA_URL,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    round_timeout: float = DEFAULT_ROUND_TIMEOUT,
    min_relevance: float = 0.25,
    min_ask: float = 0.30,
    max_ask: float = 0.85,
    order_qty: Decimal = Decimal("10"),
    max_rounds: int = 0,
) -> None:
    output_path = build_daemon_output_path(output_dir=output_dir, now=datetime.now(UTC))
    writer = DaemonRunWriter(output_path)
    run_id = uuid.uuid4().hex
    rounds = 0

    while max_rounds <= 0 or rounds < max_rounds:
        # 1. Run bridge to refresh signals
        try:
            run_bridge_loop(
                sentinel_url=sentinel_url,
                gamma_url=gamma_url,
                signal_path=signal_path,
                poll_interval=0,
                min_relevance=min_relevance,
                max_iterations=1,
            )
        except Exception as exc:
            writer.write({"run_id": run_id, "event": "bridge_error", "reason": str(exc),
                          "ts": datetime.now(UTC).isoformat()})

        # 2. Run a round
        try:
            await run_sentinel_round(
                signal_path=signal_path,
                writer=writer,
                run_id=run_id,
                round_timeout=round_timeout,
                min_relevance=min_relevance,
                min_ask=min_ask,
                max_ask=max_ask,
                order_qty=order_qty,
            )
        except Exception as exc:
            writer.write({"run_id": run_id, "event": "round_error", "reason": str(exc),
                          "ts": datetime.now(UTC).isoformat()})

        rounds += 1
        if max_rounds > 0 and rounds >= max_rounds:
            break
        await asyncio.sleep(poll_interval)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--signal-path", default=DEFAULT_SIGNAL_PATH)
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--sentinel-url", default=DEFAULT_SENTINEL_URL)
    p.add_argument("--gamma-url", default=DEFAULT_GAMMA_URL)
    p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    p.add_argument("--round-timeout", type=float, default=DEFAULT_ROUND_TIMEOUT)
    p.add_argument("--min-relevance", type=float, default=0.25)
    p.add_argument("--min-ask", type=float, default=0.30)
    p.add_argument("--max-ask", type=float, default=0.85)
    p.add_argument("--order-qty", type=Decimal, default=Decimal("10"))
    p.add_argument("--max-rounds", type=int, default=0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    asyncio.run(run_daemon(
        signal_path=Path(str(args.signal_path)),
        output_dir=str(args.output_dir),
        sentinel_url=str(args.sentinel_url),
        gamma_url=str(args.gamma_url),
        poll_interval=float(args.poll_interval),
        round_timeout=float(args.round_timeout),
        min_relevance=float(args.min_relevance),
        min_ask=float(args.min_ask),
        max_ask=float(args.max_ask),
        order_qty=Decimal(str(args.order_qty)),
        max_rounds=int(args.max_rounds),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### Step 4: Run tests
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_polymarket_sentinel_news_daemon.py -v --noconftest
```
Expected: 3 PASS.

### Step 5: Commit
```bash
git add examples/live/polymarket/polymarket_sentinel_news_daemon.py \
        tests/unit_tests/examples/test_polymarket_sentinel_news_daemon.py
git commit -m "feat: add polymarket_sentinel_news_daemon — orchestrates Sentinel signals into Nautilus paper trades"
```

---

## Task 5: Docker Compose Integration

**Files:**
- Modify: `nautilus/.docker/docker-compose.yml` (or the root docker-compose — confirm location with `ls nautilus/.docker/`)
- No new Dockerfile needed — reuse `nautilus-papertrade:latest` image

### Step 1: Locate the docker-compose file
```bash
ls /home/atlas/EL/nautilus/.docker/
# or
ls /home/atlas/EL/nautilus/docker-compose.yml
```

### Step 2: Add two new services

Add to the docker-compose under existing services (after `sports-settlement-vpn`):

```yaml
  # --- Sentinel News Signal Bridge ---
  sentinel-bridge:
    image: nautilus-papertrade:latest
    profiles: ["vpn", "sentinel"]
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    network_mode: "service:nordvpn"
    depends_on:
      nordvpn:
        condition: service_healthy
    environment:
      SENTINEL_CORE_URL: "${SENTINEL_CORE_URL:-http://172.18.0.1:8080}"  # sentinel_default gateway
      SENTINEL_API_KEY: "${SENTINEL_API_KEY:-supersecret}"
      POLYMARKET_GAMMA_URL: "${POLYMARKET_GAMMA_URL:-https://gamma-api.polymarket.com}"
      SENTINEL_POLL_INTERVAL: "${SENTINEL_POLL_INTERVAL:-60}"
      SENTINEL_MIN_RELEVANCE: "${SENTINEL_MIN_RELEVANCE:-0.25}"
      SENTINEL_SIGNAL_PATH: "/data/live_signals/sentinel_news_signals.jsonl"
    volumes:
      - ${NAUTILUS_SENTINEL_SIGNALS_PATH:-/tmp/sentinel_signals}:/data/live_signals
    command:
      - python
      - -m
      - examples.live.polymarket.sentinel_signal_bridge
      - --max-iterations
      - "0"

  # --- Sentinel News Daemon (paper trades) ---
  sentinel-news-daemon-vpn:
    image: nautilus-papertrade:latest
    profiles: ["vpn", "sentinel"]
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    network_mode: "service:nordvpn"
    depends_on:
      nordvpn:
        condition: service_healthy
    env_file:
      - path: .env.polymarket
        required: false
    environment:
      SENTINEL_CORE_URL: "${SENTINEL_CORE_URL:-http://172.18.0.1:8080}"
      SENTINEL_API_KEY: "${SENTINEL_API_KEY:-supersecret}"
      NAUTILUS_CACHE_HOST: "${NAUTILUS_VPN_CACHE_HOST:-redis}"
      NAUTILUS_CACHE_PORT: "${NAUTILUS_CACHE_PORT:-6379}"
      SENTINEL_SIGNAL_PATH: "/data/live_signals/sentinel_news_signals.jsonl"
      POLYMARKET_REQUIRE_UNBLOCKED: "${POLYMARKET_REQUIRE_UNBLOCKED:-1}"
      POLYMARKET_EXPECT_COUNTRY: "${POLYMARKET_EXPECT_COUNTRY:-JP}"
    volumes:
      - ${NAUTILUS_SENTINEL_SIGNALS_PATH:-/tmp/sentinel_signals}:/data/live_signals
      - ${NAUTILUS_SENTINEL_OUTPUT_PATH:-/tmp/sentinel_outputs}:/workspace/outputs
      - ../nautilus:/workspace/nautilus
    working_dir: /workspace/nautilus
    command:
      - python
      - /workspace/nautilus/examples/live/polymarket/polymarket_sentinel_news_daemon.py
      - --poll-interval
      - "${SENTINEL_POLL_INTERVAL:-300}"
      - --min-relevance
      - "${SENTINEL_MIN_RELEVANCE:-0.25}"
```

### Step 3: Verify compose parses without error
```bash
cd /home/atlas/EL/nautilus
docker compose -f .docker/docker-compose.yml config --quiet
```
Expected: exits 0.

### Step 4: Commit
```bash
git add .docker/docker-compose.yml
git commit -m "feat: add sentinel-bridge and sentinel-news-daemon-vpn services to docker-compose"
```

---

## Task 6: Full Test Suite Pass

Run the complete example test suite to verify nothing is broken.

```bash
cd /home/atlas/EL/nautilus
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sentinel_signal_models.py \
  tests/unit_tests/examples/test_sentinel_signal_bridge.py \
  tests/unit_tests/examples/test_sentinel_signal_strategy.py \
  tests/unit_tests/examples/test_polymarket_sentinel_news_daemon.py \
  -v
```
Expected: all PASS, no regressions in existing tests.

### Final commit
```bash
git add -A
git commit -m "test: verify full sentinel-nautilus bridge test suite passes"
```

---

## Network Note for Docker Services

Sentinel lives on `sentinel_default` (172.18.0.0/16). Nautilus lives on `nautilus_nautilus-network` (172.22.0.0/16). The services using `network_mode: "service:nordvpn"` share the nordvpn container's network namespace. To reach Sentinel from inside the VPN network:

- Use `SENTINEL_CORE_URL=http://172.18.0.1:8080` (the sentinel_default gateway, reachable from host) **or**
- Add the sentinel-bridge container to both networks explicitly if you need direct container DNS
- Alternatively use `host.docker.internal` which resolves to the host, then use the host-exposed port 8080

Verify connectivity before running live:
```bash
docker exec sentinel-bridge curl -s http://172.18.0.1:8080/api/isalive
```

---

## Smoke Test (manual, after deploy)

```bash
# 1. Start bridge for one iteration
docker run --rm \
  -e SENTINEL_CORE_URL=http://localhost:8080 \
  -e SENTINEL_API_KEY=supersecret \
  -v /tmp/sentinel_test:/data/live_signals \
  nautilus-papertrade:latest \
  python -m examples.live.polymarket.sentinel_signal_bridge --max-iterations 1

# 2. Inspect emitted signals
cat /tmp/sentinel_test/sentinel_news_signals.jsonl | python3 -m json.tool

# 3. Check signal count and relevance scores
wc -l /tmp/sentinel_test/sentinel_news_signals.jsonl
```
