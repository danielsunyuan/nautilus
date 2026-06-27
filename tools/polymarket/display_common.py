from __future__ import annotations

import json
import math
import os
import sys
import time
from math import ceil
from typing import Any
from urllib.parse import quote

import msgspec

from nautilus_trader.adapters.polymarket.common.gamma_markets import DEFAULT_GAMMA_BASE_URL
from nautilus_trader.core.nautilus_pyo3 import HttpClient
from py_clob_client.client import ClobClient


DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


class DisplayStyle:
    def __init__(self, enabled: bool) -> None:
        self.reset = "\033[0m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.green = "\033[92m" if enabled else ""
        self.red = "\033[91m" if enabled else ""
        self.cyan = "\033[96m" if enabled else ""
        self.yellow = "\033[93m" if enabled else ""


def use_color(*, no_color_flag: bool) -> bool:
    if no_color_flag or os.environ.get("NO_COLOR", "").strip():
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def coerce_list(value: Any, *, name: str) -> list[Any]:
    if isinstance(value, str):
        try:
            parsed = msgspec.json.decode(value.encode())
        except msgspec.DecodeError:
            try:
                import ast

                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError) as exc:
                raise ValueError(f"unexpected {name} payload") from exc
        if isinstance(parsed, list):
            return parsed
    if isinstance(value, list):
        return value
    raise ValueError(f"unexpected {name} payload")


def parse_outcome_prices(market: dict[str, Any]) -> list[tuple[str, float]]:
    outcomes = coerce_list(market.get("outcomes", []), name="outcomes")
    prices = coerce_list(market.get("outcomePrices", []), name="outcomePrices")
    if len(outcomes) != len(prices):
        raise ValueError("Gamma outcome arrays do not align")
    rows: list[tuple[str, float]] = []
    for outcome, price in zip(outcomes, prices, strict=False):
        rows.append((str(outcome), float(price)))
    return rows


def normalize_book_side(levels: list[dict[str, Any]] | None, *, side: str) -> list[tuple[float, float]]:
    parsed: list[tuple[float, float]] = []
    for level in levels or []:
        price_raw = level.get("price")
        size_raw = level.get("size")
        if price_raw in (None, "") or size_raw in (None, ""):
            continue
        parsed.append((float(price_raw), float(size_raw)))
    if side == "bid":
        parsed.sort(key=lambda row: row[0], reverse=True)
    else:
        parsed.sort(key=lambda row: row[0])
    return parsed


def fetch_order_book(*, token_id: str, clob_host: str = DEFAULT_CLOB_HOST) -> dict[str, Any]:
    from py_clob_client.exceptions import PolyApiException

    client = get_clob_client(clob_host)
    try:
        payload = client.get_order_book(token_id)
    except PolyApiException as exc:
        raise RuntimeError(f"no CLOB order book for token {token_id}: {exc}") from exc
    if isinstance(payload, dict):
        return payload
    if hasattr(payload, "__dict__"):
        return dict(vars(payload))
    raise RuntimeError(f"unexpected order book payload type for token {token_id}: {type(payload).__name__}")


def render_order_book(
    *,
    label: str,
    book: dict[str, Any],
    depth: int,
    style: DisplayStyle,
) -> str:
    bids = normalize_book_side(book.get("bids"), side="bid")[:depth]
    asks = normalize_book_side(book.get("asks"), side="ask")[:depth]
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread = None if best_bid is None or best_ask is None else best_ask - best_bid
    mid = None if best_bid is None or best_ask is None else (best_bid + best_ask) / 2.0

    lines = [
        f"{style.bold}{label}{style.reset}",
        (
            f"{style.dim}best bid {best_bid}  best ask {best_ask}  "
            f"spread {spread if spread is not None else '-'}  mid {mid if mid is not None else '-'}{style.reset}"
        ),
        f"{style.green}{'BID':>8} {'SIZE':>12}{style.reset}",
    ]
    for price, size in bids:
        lines.append(f"{style.green}{price:>8.4f} {size:>12.3f}{style.reset}")
    lines.append(f"{style.red}{'ASK':>8} {'SIZE':>12}{style.reset}")
    for price, size in asks:
        lines.append(f"{style.red}{price:>8.4f} {size:>12.3f}{style.reset}")
    return "\n".join(lines)


async def fetch_public_search(
    *,
    query: str,
    http_client: HttpClient,
    gamma_host: str = DEFAULT_GAMMA_BASE_URL,
    events_status: str | None = "active",
    limit_per_type: int = 50,
    page: int = 1,
    keep_closed_markets: bool = False,
    timeout: float = 15.0,
) -> dict[str, Any]:
    base = gamma_host.rstrip("/")
    params: dict[str, Any] = {
        "q": query,
        "limit_per_type": limit_per_type,
        "page": page,
        "keep_closed_markets": keep_closed_markets,
    }
    if events_status:
        params["events_status"] = events_status
    response = await http_client.get(
        f"{base}/public-search",
        params=params,
        headers=DEFAULT_HTTP_HEADERS,
        timeout_secs=max(1, ceil(timeout)),
    )
    if response.status != 200:
        body = response.body.decode("utf-8", errors="replace")
        raise RuntimeError(f"Gamma public-search failed ({response.status}): {body}")
    payload = msgspec.json.decode(response.body)
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected public-search payload")
    return payload


async def fetch_market_by_slug(
    *,
    slug: str,
    http_client: HttpClient,
    gamma_host: str = DEFAULT_GAMMA_BASE_URL,
    timeout: float = 10.0,
) -> dict[str, Any]:
    base = gamma_host.rstrip("/")
    response = await http_client.get(
        f"{base}/markets/slug/{quote(slug)}",
        headers=DEFAULT_HTTP_HEADERS,
        timeout_secs=max(1, ceil(timeout)),
    )
    if response.status == 404:
        raise ValueError(f"market slug {slug!r} not found")
    if response.status != 200:
        body = response.body.decode("utf-8", errors="replace")
        raise RuntimeError(f"Gamma market lookup failed ({response.status}): {body}")
    payload = msgspec.json.decode(response.body)
    if isinstance(payload, list):
        if not payload:
            raise ValueError(f"market slug {slug!r} not found")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected market payload")
    return payload


def iter_search_markets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        event_title = str(event.get("title") or event.get("ticker") or event.get("slug") or "")
        event_slug = str(event.get("slug") or "")
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            rows.append(
                {
                    "event_title": event_title,
                    "event_slug": event_slug,
                    "market": market,
                },
            )
    markets = payload.get("markets") or []
    for market in markets:
        if isinstance(market, dict):
            rows.append({"event_title": "", "event_slug": "", "market": market})
    return rows


def render_market_odds_row(
    *,
    event_title: str,
    market: dict[str, Any],
    style: DisplayStyle,
) -> str:
    question = str(market.get("question") or market.get("groupItemTitle") or "Unknown market")
    slug = str(market.get("slug") or "")
    try:
        outcomes = parse_outcome_prices(market)
    except ValueError:
        outcomes = []
    odds = " | ".join(f"{label} {price:.1%}" for label, price in outcomes) if outcomes else "n/a"
    header = event_title.strip() or question
    if event_title.strip() and question != event_title.strip():
        header = f"{event_title.strip()} — {question}"
    link = f"https://polymarket.com/event/{slug}" if slug else ""
    return "\n".join(
        [
            f"{style.bold}{header}{style.reset}",
            f"{style.cyan}{odds}{style.reset}",
            f"{style.dim}{link}{style.reset}" if link else "",
        ],
    ).strip()


def render_market_odds_table(
    rows: list[dict[str, Any]],
    *,
    style: DisplayStyle,
    max_markets: int | None = None,
) -> str:
    rendered: list[str] = []
    for idx, row in enumerate(rows):
        if max_markets is not None and idx >= max_markets:
            break
        rendered.append(
            render_market_odds_row(
                event_title=str(row.get("event_title") or ""),
                market=row["market"],
                style=style,
            ),
        )
    return "\n\n".join(rendered)


def dump_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


_CLOB_CLIENTS: dict[str, ClobClient] = {}


def get_clob_client(clob_host: str = DEFAULT_CLOB_HOST) -> ClobClient:
    host = clob_host.rstrip("/")
    client = _CLOB_CLIENTS.get(host)
    if client is None:
        client = ClobClient(host)
        _CLOB_CLIENTS[host] = client
    return client


def fetch_order_books_parallel(
    *,
    token_ids: tuple[str, str],
    clob_host: str = DEFAULT_CLOB_HOST,
    retries: int = 3,
    retry_delay: float = 0.2,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor

    last_error: RuntimeError | None = None
    for attempt in range(max(1, retries)):
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.map(
                    lambda token_id: fetch_order_book(token_id=token_id, clob_host=clob_host),
                    token_ids,
                )
            return first, second
        except RuntimeError as exc:
            last_error = exc
            if attempt + 1 >= retries:
                break
            time.sleep(retry_delay)
    assert last_error is not None
    raise last_error


def format_top_level(
    levels: list[tuple[float, float]],
    *,
    style: DisplayStyle,
    side: str,
) -> str:
    if not levels:
        return f"{style.dim}-{style.reset}"
    price, size = levels[0]
    color = style.green if side == "bid" else style.red
    return f"{color}{price:.2f}x{size:g}{style.reset}"


def render_fast_books(
    *,
    slug: str,
    remaining_seconds: int,
    up_book: dict[str, Any],
    down_book: dict[str, Any],
    style: DisplayStyle,
) -> str:
    minutes, seconds = divmod(max(0, remaining_seconds), 60)
    up_bid = normalize_book_side(up_book.get("bids"), side="bid")[:1]
    up_ask = normalize_book_side(up_book.get("asks"), side="ask")[:1]
    down_bid = normalize_book_side(down_book.get("bids"), side="bid")[:1]
    down_ask = normalize_book_side(down_book.get("asks"), side="ask")[:1]
    return "\n".join(
        [
            f"{style.cyan}T{minutes:02d}:{seconds:02d}{style.reset} {style.dim}{slug}{style.reset}",
            (
                f"{style.bold}UP{style.reset}  "
                f"{format_top_level(up_bid, style=style, side='bid')} | "
                f"{format_top_level(up_ask, style=style, side='ask')}"
            ),
            (
                f"{style.bold}NO{style.reset}  "
                f"{format_top_level(down_bid, style=style, side='bid')} | "
                f"{format_top_level(down_ask, style=style, side='ask')}"
            ),
        ],
    )


async def fetch_gamma_event_by_slug(
    *,
    slug: str,
    http_client: HttpClient,
    gamma_host: str = DEFAULT_GAMMA_BASE_URL,
    timeout: float = 10.0,
) -> dict[str, Any]:
    base = gamma_host.rstrip("/")
    response = await http_client.get(
        f"{base}/events",
        params={"slug": slug},
        headers=DEFAULT_HTTP_HEADERS,
        timeout_secs=max(1, ceil(timeout)),
    )
    if response.status != 200:
        body = response.body.decode("utf-8", errors="replace")
        raise RuntimeError(f"Gamma event lookup failed ({response.status}): {body}")
    payload = msgspec.json.decode(response.body)
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"event slug {slug!r} not found")
    event = payload[0]
    if not isinstance(event, dict):
        raise RuntimeError("unexpected event payload")
    return event


def parse_yes_winner_market(market: dict[str, Any]) -> dict[str, Any] | None:
    if market.get("closed") is True or market.get("active") is False:
        return None
    if market.get("acceptingOrders") is False or market.get("accepting_orders") is False:
        return None
    label = str(market.get("groupItemTitle") or "").strip()
    question = str(market.get("question") or "").strip()
    if not label and question.lower().startswith("will "):
        label = question.removeprefix("Will ").split(" win ", maxsplit=1)[0].strip()
    if not label:
        return None
    outcomes = coerce_list(market.get("outcomes", []), name="outcomes")
    token_ids = coerce_list(market.get("clobTokenIds", []), name="clobTokenIds")
    if len(outcomes) != len(token_ids):
        return None
    yes_index = next((idx for idx, outcome in enumerate(outcomes) if str(outcome).lower() == "yes"), 0)
    yes_token = str(token_ids[yes_index]).strip()
    if not yes_token:
        return None
    gamma_yes = 0.0
    try:
        prices = parse_outcome_prices(market)
        for outcome, price in prices:
            if str(outcome).lower() == "yes":
                gamma_yes = price
                break
    except ValueError:
        pass
    return {
        "label": label,
        "slug": str(market.get("slug") or ""),
        "yes_token_id": yes_token,
        "gamma_yes": gamma_yes,
    }


def fetch_order_books_many(
    *,
    token_ids: list[str],
    clob_host: str = DEFAULT_CLOB_HOST,
    max_workers: int = 8,
    retries: int = 2,
) -> dict[str, dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor

    books: dict[str, dict[str, Any]] = {}

    def _fetch(token_id: str) -> tuple[str, dict[str, Any] | None]:
        try:
            return token_id, fetch_order_book(token_id=token_id, clob_host=clob_host)
        except RuntimeError:
            return token_id, None

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for token_id, book in pool.map(_fetch, token_ids):
            if book is not None:
                books[token_id] = book
    if not books and token_ids:
        if retries > 0:
            time.sleep(0.2)
            return fetch_order_books_many(
                token_ids=token_ids,
                clob_host=clob_host,
                max_workers=max_workers,
                retries=retries - 1,
            )
    return books


def top_of_book_mid(book: dict[str, Any]) -> float:
    bids = normalize_book_side(book.get("bids"), side="bid")[:1]
    asks = normalize_book_side(book.get("asks"), side="ask")[:1]
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2.0
    if bids:
        return bids[0][0]
    if asks:
        return asks[0][0]
    return 0.0


def load_winner_market_entries(event: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for market in event.get("markets") or []:
        if not isinstance(market, dict):
            continue
        parsed = parse_yes_winner_market(market)
        if parsed is not None:
            entries.append(parsed)
    entries.sort(key=lambda row: float(row["gamma_yes"]), reverse=True)
    return entries


def fit_probability_gaussian(probabilities: list[float]) -> tuple[float, float]:
    values = [prob for prob in probabilities if prob > 0.0]
    if not values:
        return 0.0, 0.01
    total = sum(values)
    mean = sum(prob * prob for prob in values) / total
    variance = sum(prob * (prob - mean) ** 2 for prob in values) / total
    return mean, math.sqrt(max(variance, 1e-8))


def render_ascii_gaussian(
    *,
    title: str,
    points: list[tuple[str, float]],
    style: DisplayStyle,
    width: int = 56,
    height: int = 10,
    marker_limit: int = 6,
) -> list[str]:
    if not points:
        return [f"{style.bold}{title}{style.reset}", f"{style.dim}no live probabilities{style.reset}"]

    probs = [prob for _, prob in points]
    mu, sigma = fit_probability_gaussian(probs)
    total_prob = sum(probs)
    xmax = max(0.12, max(probs) * 1.25, mu + sigma * 2.5)
    xmin = 0.0

    grid = [[" " for _ in range(width)] for _ in range(height)]

    peak = 0.0
    samples: list[float] = []
    for col in range(width):
        x = xmin + (xmax - xmin) * (col / max(1, width - 1))
        density = math.exp(-0.5 * ((x - mu) / sigma) ** 2)
        samples.append(density)
        peak = max(peak, density)

    for col, density in enumerate(samples):
        if peak <= 0.0:
            continue
        row = height - 1 - int(round((density / peak) * (height - 1)))
        grid[row][col] = "*"

    curve_lines = ["".join(grid[row]) for row in range(height)]
    axis = f"{style.dim}{'0%':<{width // 2}}{int(xmax * 100)}%{style.reset}"
    header = (
        f"{style.bold}{title}{style.reset}  "
        f"{style.cyan}mu={mu * 100:.1f}%{style.reset}  "
        f"{style.cyan}sigma={sigma * 100:.1f}%{style.reset}  "
        f"{style.dim}sum={total_prob * 100:.1f}%{style.reset}"
    )

    marker_row = [" "] * width
    marker_labels: list[str] = []
    for label, prob in points[:marker_limit]:
        col = int(round((prob - xmin) / max(xmax - xmin, 1e-9) * (width - 1)))
        col = max(0, min(width - 1, col))
        marker_row[col] = "|"
        short = label[:3].upper()
        marker_labels.append(f"{style.yellow}{short}{style.reset} {prob * 100:.1f}%")

    lines = [header, *curve_lines, axis, "".join(marker_row), "  ".join(marker_labels)]
    return lines


def render_winner_stream(
    *,
    title: str,
    rows: list[tuple[str, dict[str, Any]]],
    style: DisplayStyle,
) -> str:
    lines: list[str] = []
    if title:
        lines.append(f"{style.bold}{title}{style.reset}")
    label_width = max(8, min(16, max((len(label) for label, _ in rows), default=8)))
    for label, book in rows:
        bid = normalize_book_side(book.get("bids"), side="bid")[:1]
        ask = normalize_book_side(book.get("asks"), side="ask")[:1]
        lines.append(
            f"{style.dim}{label:<{label_width}}{style.reset} "
            f"{format_top_level(bid, style=style, side='bid')} | "
            f"{format_top_level(ask, style=style, side='ask')}",
        )
    return "\n".join(lines)


def render_winner_gaussian_stream(
    *,
    title: str,
    rows: list[tuple[str, dict[str, Any]]],
    style: DisplayStyle,
    list_limit: int = 8,
) -> str:
    points = [(label, top_of_book_mid(book)) for label, book in rows if top_of_book_mid(book) > 0.0]
    gaussian_lines = render_ascii_gaussian(title=title, points=points, style=style)
    list_lines = render_winner_stream(
        title="",
        rows=rows[:list_limit],
        style=style,
    ).splitlines()
    list_lines = [line for line in list_lines if line.strip()]
    return "\n".join([*gaussian_lines, "", *list_lines])


def clear_fast_stream() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def write_fast_stream(text: str, *, line_count: int, first: bool, reset: bool = False) -> None:
    if reset:
        clear_fast_stream()
        print(text, flush=True)
        return
    if first:
        print(text, flush=True)
        return
    sys.stdout.write(f"\033[{line_count}F{text}\033[J")
    sys.stdout.flush()
