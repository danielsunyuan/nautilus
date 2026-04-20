"""
Sentinel → Polymarket signal bridge.

Polls Sentinel Core REST API for new stories, maps them to active Polymarket
markets via Gamma API, scores relevance, and writes SentinelNewsSignal JSONL
to a shared signal file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import urllib.request
import urllib.error

try:
    from examples.live.polymarket.sentinel_signal_models import SentinelNewsSignal, validate_direction
except ModuleNotFoundError:
    import importlib.util, sys
    _p = Path(__file__).resolve().with_name("sentinel_signal_models.py")
    _spec = importlib.util.spec_from_file_location("sentinel_signal_models", _p)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["sentinel_signal_models"] = _mod
    _spec.loader.exec_module(_mod)
    SentinelNewsSignal = _mod.SentinelNewsSignal
    validate_direction = _mod.validate_direction


DEFAULT_SENTINEL_URL = os.environ.get("SENTINEL_CORE_URL", "http://localhost:8080")
DEFAULT_GAMMA_URL = os.environ.get("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
DEFAULT_SIGNAL_PATH = os.environ.get(
    "SENTINEL_SIGNAL_PATH",
    "/data/nautilus_export/live_signals/sentinel_news_signals.jsonl",
)
DEFAULT_POLL_INTERVAL = float(os.environ.get("SENTINEL_POLL_INTERVAL", "60"))
DEFAULT_MIN_RELEVANCE = float(os.environ.get("SENTINEL_MIN_RELEVANCE", "0.05"))
DEFAULT_SENTINEL_API_KEY = os.environ.get("SENTINEL_API_KEY", "supersecret")

_STOP_WORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would could should "
    "may might shall can cannot of in on at to for with by from this that these those".split()
)

_CATEGORY_TAG_SLUGS: dict[str, list[str]] = {
    "conflict": ["geopolitics", "ukraine", "russia", "middle-east", "israel", "wars"],
    "election": ["elections", "politics"],
    "financial": ["crypto", "economics", "finance", "bitcoin"],
    "geopolitical": ["geopolitics", "politics", "trump", "united-nations"],
    "other": ["geopolitics", "politics", "trump"],
}

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "conflict": ["war", "attack", "military", "troops", "missile", "bomb", "invasion", "offensive",
                 "ceasefire", "conflict", "sanction", "weapon", "nato", "ukraine", "russia", "israel",
                 "hamas", "iran", "china", "taiwan"],
    "election": ["election", "vote", "president", "prime", "minister", "candidate", "poll",
                 "ballot", "campaign", "resign", "impeach", "parliament"],
    "financial": ["bitcoin", "btc", "eth", "crypto", "fed", "rate", "inflation", "gdp", "recession",
                  "tariff", "trade", "default", "debt", "nasdaq"],
    "geopolitical": ["diplomat", "treaty", "summit", "sanction", "alliance", "un", "nato",
                     "security", "council", "peace", "negotiation", "accord"],
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
    """Return YES or NO based on naive sentiment matching."""
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
    """Extract combined text and headline from a Sentinel news item dict.

    Supports two layouts:
    - Flat (real API): item has top-level ``title`` and ``content`` fields.
    - Nested (legacy/analyst): item has a ``news_items`` list of sub-items,
      each optionally with a ``review`` sub-dict.
    """
    parts = []
    headline = str(story.get("title") or "")
    if headline:
        parts.append(headline)

    # Flat layout: content is directly on the item
    content = str(story.get("content") or "")
    if content:
        parts.append(content[:500])

    # Nested layout: analyst-created report items with embedded news_items
    for item in story.get("news_items", []):
        review = item.get("review") or {}
        item_title = str(review.get("title") or item.get("title") or "")
        item_content = str(item.get("content") or "")
        if item_title:
            parts.append(item_title)
        if item_content:
            parts.append(item_content[:500])

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
                end_dt = end_dt.replace(tzinfo=timezone.utc)
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


_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _http_get_json(url: str, *, headers: dict | None = None, timeout: float = 10.0) -> Any:
    merged = {**_DEFAULT_HEADERS, **(headers or {})}
    req = urllib.request.Request(url, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"URL error from {url}: {exc.reason}") from exc


def fetch_sentinel_jwt(
    *,
    base_url: str,
    username: str,
    password: str,
    timeout: float = 10.0,
) -> str:
    """Login to Sentinel and return a JWT access token."""
    url = f"{base_url.rstrip('/')}/api/auth/login"
    payload = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
            return str(data.get("access_token") or data.get("token") or "")
    except Exception as exc:
        raise RuntimeError(f"Sentinel JWT login failed: {exc}") from exc


def fetch_sentinel_stories(
    *,
    base_url: str,
    api_key: str,
    limit: int = 20,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Fetch recent news items from Sentinel Core REST API.

    Authenticates via JWT (POST /api/auth/login with api_key used as password
    for the default 'user' account), then fetches from /api/assess/news-items.
    Falls back to using api_key directly as a Bearer token if login fails.
    """
    # Try JWT login first
    try:
        token = fetch_sentinel_jwt(
            base_url=base_url,
            username="user",
            password=api_key,
            timeout=timeout,
        )
    except Exception as exc:
        print(json.dumps({"event": "sentinel_jwt_error", "reason": str(exc)}, sort_keys=True), flush=True)
        token = api_key  # fall back to raw key

    url = f"{base_url.rstrip('/')}/api/assess/news-items?limit={limit}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        data = _http_get_json(url, headers=headers, timeout=timeout)
        if isinstance(data, dict):
            return data.get("items", data.get("data", []))
        return list(data)
    except Exception as exc:
        print(json.dumps({"event": "sentinel_fetch_error", "reason": str(exc)}, sort_keys=True), flush=True)
        return []


def fetch_gamma_markets_for_category(
    *,
    base_url: str,
    category: str,
    limit_per_tag: int = 5,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Fetch active Polymarket markets from Gamma /events, filtered by category tag slugs.

    Uses the /events endpoint (which contains nested markets) because /markets
    only returns featured markets regardless of tag filters.
    """
    tag_slugs = _CATEGORY_TAG_SLUGS.get(category, _CATEGORY_TAG_SLUGS["other"])
    seen_condition_ids: set[str] = set()
    all_markets: list[dict[str, Any]] = []

    for slug in tag_slugs:
        url = (
            f"{base_url.rstrip('/')}/events"
            f"?tag_slug={slug}&active=true&closed=false&limit={limit_per_tag}"
        )
        try:
            events = _http_get_json(url, timeout=timeout)
            if not isinstance(events, list):
                events = events.get("events", events.get("data", []))
            for event in events:
                for market in event.get("markets", []):
                    cid = str(market.get("conditionId") or "")
                    if cid and cid not in seen_condition_ids:
                        seen_condition_ids.add(cid)
                        all_markets.append(market)
        except Exception as exc:
            print(
                json.dumps(
                    {"event": "gamma_fetch_error", "tag_slug": slug, "reason": str(exc)},
                    sort_keys=True,
                ),
                flush=True,
            )

    return all_markets


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
    category_markets: dict[str, list[dict[str, Any]]],
    min_relevance: float,
    signal_path: Path,
    emitted_story_ids: set[str],
) -> list[SentinelNewsSignal]:
    """Map a single Sentinel story to Polymarket markets and emit signals.

    ``category_markets`` is a pre-fetched dict of {category: [market_dict]}
    so no per-story Gamma API calls are needed.
    """
    story_id = str(story.get("id") or story.get("story_id") or "")
    if not story_id or story_id in emitted_story_ids:
        return []

    story_text, headline = extract_story_text(story)
    if not story_text.strip():
        return []

    # Skip non-English stories — Cyrillic/CJK/etc. tokenize to empty sets
    # and can never match English Polymarket market questions.
    if len(_tokenize(story_text)) < 3:
        return []

    category = classify_story(story_text)
    active_markets = category_markets.get(category, [])

    signals: list[SentinelNewsSignal] = []
    now = datetime.now(timezone.utc)

    for market in active_markets:
        question = str(market.get("question") or market.get("title") or "")
        score = score_market_relevance(story_text=story_text, market_question=question)
        if score < float(min_relevance):
            continue

        slug = str(market.get("slug") or market.get("conditionId") or "")
        condition_id = str(market.get("conditionId") or "")
        end_raw = str(market.get("endDate") or "")

        raw_clob = market.get("clobTokenIds", [])
        # Gamma /events returns clobTokenIds as a JSON-encoded string, not a list.
        if isinstance(raw_clob, str):
            try:
                raw_clob = json.loads(raw_clob)
            except (ValueError, TypeError):
                raw_clob = []
        clob_token_ids = raw_clob if isinstance(raw_clob, list) else []
        yes_token_id = ""
        no_token_id = ""
        if len(clob_token_ids) >= 2:
            yes_token_id = str(clob_token_ids[0])
            no_token_id = str(clob_token_ids[1])

        if not yes_token_id:
            continue

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


def _fetch_all_category_markets(
    *,
    gamma_url: str,
    timeout: float = 10.0,
) -> dict[str, list[dict[str, Any]]]:
    """Pre-fetch and cache active markets for all categories in one pass.

    Returns a dict mapping category name → list of active market dicts.
    Gamma calls are batched by category (not per-story) to keep total
    requests to ~5 category sets regardless of story count.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for category, tag_slugs in _CATEGORY_TAG_SLUGS.items():
        seen: set[str] = set()
        markets: list[dict[str, Any]] = []
        for slug in tag_slugs:
            url = (
                f"{gamma_url.rstrip('/')}/events"
                f"?tag_slug={slug}&active=true&closed=false&limit=5"
            )
            try:
                events = _http_get_json(url, timeout=timeout)
                if not isinstance(events, list):
                    events = events.get("events", events.get("data", []))
                for event in events:
                    for m in event.get("markets", []):
                        cid = str(m.get("conditionId") or "")
                        if cid and cid not in seen:
                            seen.add(cid)
                            markets.append(m)
            except Exception as exc:
                print(
                    json.dumps(
                        {"event": "gamma_fetch_error", "tag_slug": slug, "reason": str(exc)},
                        sort_keys=True,
                    ),
                    flush=True,
                )
        result[category] = filter_active_markets(markets)
    return result


def run_bridge_loop(
    *,
    sentinel_url: str = DEFAULT_SENTINEL_URL,
    sentinel_api_key: str = DEFAULT_SENTINEL_API_KEY,
    gamma_url: str = DEFAULT_GAMMA_URL,
    signal_path: "Path | str" = DEFAULT_SIGNAL_PATH,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    min_relevance: float = DEFAULT_MIN_RELEVANCE,
    max_iterations: int = 0,
) -> None:
    signal_path = Path(signal_path)
    emitted_story_ids: set[str] = set()
    iteration = 0

    while max_iterations <= 0 or iteration < max_iterations:
        # Pre-fetch markets once per poll cycle (batched by category)
        category_markets = _fetch_all_category_markets(gamma_url=gamma_url)

        stories = fetch_sentinel_stories(
            base_url=sentinel_url,
            api_key=sentinel_api_key,
            limit=50,
        )
        for story in stories:
            process_story(
                story=story,
                category_markets=category_markets,
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
    p.add_argument("--max-iterations", type=int, default=0)
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
