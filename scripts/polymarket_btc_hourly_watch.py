from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import quote


DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_GAMMA_HOST = "https://gamma-api.polymarket.com"
DEFAULT_BINANCE_SYMBOL = "BTCUSDT"
DEFAULT_VOLATILITY = 0.008
DEFAULT_MIN_EDGE = 0.05
DEFAULT_EXECUTION_COST_BUFFER = 0.03
DEFAULT_MIN_ROUND_TRIP_EDGE = 0.0
DEFAULT_MIN_TIME_REMAINING_SECONDS = 10 * 60
DEFAULT_MAX_TIME_REMAINING_SECONDS = 45 * 60
DEFAULT_POLL_SECONDS = 5.0
EASTERN_TZ = ZoneInfo("America/New_York")
DEFAULT_SLUG_PREFIX_BY_SYMBOL = {
    "BTCUSDT": "bitcoin-up-or-down",
    "ETHUSDT": "ethereum-up-or-down",
    "SOLUSDT": "solana-up-or-down",
    "BNBUSDT": "bnb-up-or-down",
    "XRPUSDT": "xrp-up-or-down",
    "DOGEUSDT": "dogecoin-up-or-down",
    "HYPEUSDT": "hype-up-or-down",
}


def _json_get(url: str, *, timeout: float) -> Any:
    # Polymarket's public endpoints are sometimes more permissive with a browser-like UA.
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _binance_klines_url(symbol: str) -> str:
    return f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=1"


def _binance_ticker_url(symbol: str) -> str:
    return f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"


def _gamma_market_slug_url(host: str, slug: str) -> str:
    base = host.rstrip("/")
    return f"{base}/markets/slug/{quote(slug)}"


def current_hour_market_slug(*, slug_prefix: str, now: datetime) -> str:
    eastern = now.astimezone(EASTERN_TZ)
    hour = eastern.strftime("%-I%p").lower()
    month = eastern.strftime("%B").lower()
    day = str(eastern.day)
    year = str(eastern.year)
    return f"{slug_prefix}-{month}-{day}-{year}-{hour}-et"


def current_hour_open(*, klines_url: str, timeout: float) -> tuple[datetime, float]:
    payload = _json_get(klines_url, timeout=timeout)
    if not isinstance(payload, list) or not payload:
        raise ValueError("unexpected Binance klines payload")
    row = payload[-1]
    if not isinstance(row, list) or len(row) < 2:
        raise ValueError("unexpected Binance kline row")
    return datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc), float(row[1])


def current_spot(*, ticker_url: str, timeout: float) -> float:
    payload = _json_get(ticker_url, timeout=timeout)
    if not isinstance(payload, dict) or "price" not in payload:
        raise ValueError("unexpected Binance ticker payload")
    return float(payload["price"])


def time_remaining_seconds(*, now: datetime, hour_open: datetime) -> int:
    hour_end = hour_open + timedelta(hours=1)
    remaining = int((hour_end - now).total_seconds())
    return max(0, remaining)


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_up_probability(
    *,
    open_price: float,
    current_price: float,
    time_remaining_seconds: float,
    volatility: float,
) -> float:
    if open_price <= 0 or current_price <= 0:
        raise ValueError("prices must be positive")
    if volatility <= 0:
        raise ValueError("volatility must be positive")
    if time_remaining_seconds <= 0:
        return 1.0 if current_price >= open_price else 0.0
    time_remaining_hours = time_remaining_seconds / 3600.0
    z = math.log(current_price / open_price) / (volatility * math.sqrt(time_remaining_hours))
    return min(1.0, max(0.0, normal_cdf(z)))


def market_edges(*, fair_up_probability: float, market_up_probability: float) -> dict[str, float]:
    fair_down_probability = 1.0 - fair_up_probability
    market_down_probability = 1.0 - market_up_probability
    return {
        "up": abs(fair_up_probability - market_up_probability),
        "down": market_down_probability - fair_down_probability,
    }


def cost_adjusted_edge(*, fair_probability: float, executable_probability: float, execution_cost_buffer: float) -> float:
    return fair_probability - executable_probability - execution_cost_buffer


def liquidation_sanity_edge(*, entry_probability: float, exit_probability: float, execution_cost_buffer: float) -> float:
    return exit_probability - entry_probability - execution_cost_buffer


def classify_trigger_state(
    *,
    market_up_probability: float,
    fair_up_probability: float,
    min_edge: float,
    time_remaining_seconds: int,
    min_time_remaining_seconds: int,
    max_time_remaining_seconds: int,
) -> dict[str, Any]:
    fair_down_probability = 1.0 - fair_up_probability
    market_down_probability = 1.0 - market_up_probability
    edge_up = fair_up_probability - market_up_probability
    edge_down = fair_down_probability - market_down_probability
    inside_window = min_time_remaining_seconds <= time_remaining_seconds <= max_time_remaining_seconds
    if not inside_window:
        return {"triggered": False, "side": None, "edge": 0.0}
    if edge_up >= min_edge and edge_up >= edge_down:
        return {"triggered": True, "side": "up", "edge": edge_up}
    if edge_down >= min_edge:
        return {"triggered": True, "side": "down", "edge": edge_down}
    return {"triggered": False, "side": None, "edge": max(edge_up, edge_down)}


def classify_trigger_state_from_executable_prices(
    *,
    executable_up_probability: float,
    executable_down_probability: float,
    fair_up_probability: float,
    min_edge: float,
    execution_cost_buffer: float,
    time_remaining_seconds: int,
    min_time_remaining_seconds: int,
    max_time_remaining_seconds: int,
    liquidation_up_probability: float | None = None,
    liquidation_down_probability: float | None = None,
    min_round_trip_edge: float = DEFAULT_MIN_ROUND_TRIP_EDGE,
) -> dict[str, Any]:
    fair_down_probability = 1.0 - fair_up_probability
    raw_up_edge = fair_up_probability - executable_up_probability
    raw_down_edge = fair_down_probability - executable_down_probability
    edge_up = cost_adjusted_edge(
        fair_probability=fair_up_probability,
        executable_probability=executable_up_probability,
        execution_cost_buffer=execution_cost_buffer,
    )
    edge_down = cost_adjusted_edge(
        fair_probability=fair_down_probability,
        executable_probability=executable_down_probability,
        execution_cost_buffer=execution_cost_buffer,
    )
    liquidation_up_edge = (
        liquidation_sanity_edge(
            entry_probability=executable_up_probability,
            exit_probability=liquidation_up_probability,
            execution_cost_buffer=execution_cost_buffer,
        )
        if liquidation_up_probability is not None
        else None
    )
    liquidation_down_edge = (
        liquidation_sanity_edge(
            entry_probability=executable_down_probability,
            exit_probability=liquidation_down_probability,
            execution_cost_buffer=execution_cost_buffer,
        )
        if liquidation_down_probability is not None
        else None
    )
    inside_window = min_time_remaining_seconds <= time_remaining_seconds <= max_time_remaining_seconds
    if not inside_window:
        return {
            "triggered": False,
            "side": None,
            "edge": 0.0,
            "raw_up_edge": raw_up_edge,
            "raw_down_edge": raw_down_edge,
            "liquidation_up_edge": liquidation_up_edge,
            "liquidation_down_edge": liquidation_down_edge,
        }
    if edge_up >= min_edge and edge_up >= edge_down:
        if liquidation_up_edge is None or liquidation_up_edge >= min_round_trip_edge:
            return {
                "triggered": True,
                "side": "up",
                "edge": edge_up,
                "raw_up_edge": raw_up_edge,
                "raw_down_edge": raw_down_edge,
                "liquidation_up_edge": liquidation_up_edge,
                "liquidation_down_edge": liquidation_down_edge,
            }
    if edge_down >= min_edge:
        if liquidation_down_edge is None or liquidation_down_edge >= min_round_trip_edge:
            return {
                "triggered": True,
                "side": "down",
                "edge": edge_down,
                "raw_up_edge": raw_up_edge,
                "raw_down_edge": raw_down_edge,
                "liquidation_up_edge": liquidation_up_edge,
                "liquidation_down_edge": liquidation_down_edge,
            }
    return {
        "triggered": False,
        "side": None,
        "edge": max(edge_up, edge_down),
        "raw_up_edge": raw_up_edge,
        "raw_down_edge": raw_down_edge,
        "liquidation_up_edge": liquidation_up_edge,
        "liquidation_down_edge": liquidation_down_edge,
    }


def render_watch_line(
    *,
    label: str,
    now: datetime,
    hour_open: datetime,
    market_up_probability: float,
    fair_up_probability: float,
    trigger_state: dict[str, Any],
) -> str:
    prefix = "TRIGGER" if trigger_state.get("triggered") else "WATCH"
    return (
        f"{prefix} {label} "
        f"t={now.isoformat()} "
        f"hour_open={hour_open.isoformat()} "
        f"mkt_up={market_up_probability:.3f} "
        f"fair_up={fair_up_probability:.3f} "
        f"side={trigger_state.get('side') or '-'} "
        f"edge={float(trigger_state.get('edge') or 0.0):.3f}"
    )


def _build_public_clob_client(host: str) -> Any:
    from py_clob_client.client import ClobClient  # type: ignore

    return ClobClient(host.strip() or DEFAULT_CLOB_HOST)


def _buy_probability(client: Any, token_id: str) -> float:
    payload = client.get_price(token_id, "BUY")
    if not isinstance(payload, dict) or "price" not in payload:
        raise ValueError(f"unexpected price payload for token {token_id}")
    return float(payload["price"])


def _sell_probability(client: Any, token_id: str) -> float:
    payload = client.get_price(token_id, "SELL")
    if not isinstance(payload, dict) or "price" not in payload:
        raise ValueError(f"unexpected price payload for token {token_id}")
    return float(payload["price"])


def _require_list(value: Any, *, name: str) -> list[Any]:
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    if isinstance(value, list):
        return value
    raise ValueError(f"unexpected {name} payload")


def gamma_market_snapshot(*, gamma_host: str, slug: str, timeout: float) -> dict[str, Any]:
    payload = _json_get(_gamma_market_slug_url(gamma_host, slug), timeout=timeout)
    if not isinstance(payload, dict):
        raise ValueError("unexpected Gamma market payload")
    outcomes = _require_list(payload.get("outcomes", []), name="outcomes")
    prices = _require_list(payload.get("outcomePrices", []), name="outcomePrices")
    token_ids = _require_list(payload.get("clobTokenIds", []), name="clobTokenIds")
    if len(outcomes) != len(prices) or len(outcomes) != len(token_ids):
        raise ValueError("Gamma outcome arrays do not align")
    market: dict[str, dict[str, Any]] = {}
    for idx, outcome in enumerate(outcomes):
        label = str(outcome).strip().lower()
        market[label] = {
            "price": float(prices[idx]),
            "token_id": str(token_ids[idx]),
        }
    if "up" not in market or "down" not in market:
        raise ValueError("Gamma market payload missing Up/Down outcomes")
    return {
        "question": payload.get("question"),
        "slug": payload.get("slug"),
        "up": market["up"],
        "down": market["down"],
        "active": payload.get("active"),
        "closed": payload.get("closed"),
        "accepting_orders": payload.get("acceptingOrders", payload.get("accepting_orders")),
        "order_min_size": payload.get("orderMinSize", payload.get("order_min_size")),
        "order_price_min_tick_size": payload.get(
            "orderPriceMinTickSize",
            payload.get("order_price_min_tick_size"),
        ),
    }


def validate_gamma_market_open(market: dict[str, Any]) -> None:
    if market.get("closed") is True:
        raise ValueError(f"Gamma market slug {market.get('slug') or '?'} is already closed")
    if market.get("accepting_orders") is False:
        raise ValueError(f"Gamma market slug {market.get('slug') or '?'} is not accepting orders")


def run_watch(
    *,
    label: str,
    up_token_id: str | None,
    down_token_id: str | None,
    market_slug: str | None,
    market_slug_prefix: str | None,
    binance_symbol: str,
    host: str,
    gamma_host: str,
    volatility: float,
    min_edge: float,
    min_time_remaining_seconds: int,
    max_time_remaining_seconds: int,
    timeout: float,
    poll_seconds: float,
    once: bool,
    market_up_probability_override: float | None,
    execution_cost_buffer: float,
    min_round_trip_edge: float,
) -> int:
    client = _build_public_clob_client(host) if market_up_probability_override is None else None
    while True:
        now = datetime.now(timezone.utc)
        hour_open, open_price = current_hour_open(klines_url=_binance_klines_url(binance_symbol), timeout=timeout)
        spot = current_spot(ticker_url=_binance_ticker_url(binance_symbol), timeout=timeout)
        remaining = time_remaining_seconds(now=now, hour_open=hour_open)
        executable_up_probability: float | None = None
        executable_down_probability: float | None = None
        liquidation_up_probability: float | None = None
        liquidation_down_probability: float | None = None
        if market_up_probability_override is not None:
            market_up_probability = float(market_up_probability_override)
        elif market_slug or market_slug_prefix:
            resolved_slug = market_slug or current_hour_market_slug(slug_prefix=str(market_slug_prefix), now=now)
            market = gamma_market_snapshot(gamma_host=gamma_host, slug=resolved_slug, timeout=timeout)
            validate_gamma_market_open(market)
            market_up_probability = float(market["up"]["price"])
            if client is not None:
                executable_up_probability = _buy_probability(client, str(market["up"]["token_id"]))
                executable_down_probability = _buy_probability(client, str(market["down"]["token_id"]))
                liquidation_up_probability = _sell_probability(client, str(market["up"]["token_id"]))
                liquidation_down_probability = _sell_probability(client, str(market["down"]["token_id"]))
        else:
            assert client is not None
            assert up_token_id and down_token_id
            market_up_probability = _buy_probability(client, up_token_id)
            executable_up_probability = market_up_probability
            executable_down_probability = _buy_probability(client, down_token_id)
            liquidation_up_probability = _sell_probability(client, up_token_id)
            liquidation_down_probability = _sell_probability(client, down_token_id)
        fair = fair_up_probability(
            open_price=open_price,
            current_price=spot,
            time_remaining_seconds=remaining,
            volatility=volatility,
        )
        if executable_up_probability is not None and executable_down_probability is not None:
            trigger_state = classify_trigger_state_from_executable_prices(
                executable_up_probability=executable_up_probability,
                executable_down_probability=executable_down_probability,
                fair_up_probability=fair,
                min_edge=min_edge,
                execution_cost_buffer=execution_cost_buffer,
                time_remaining_seconds=remaining,
                min_time_remaining_seconds=min_time_remaining_seconds,
                max_time_remaining_seconds=max_time_remaining_seconds,
                liquidation_up_probability=liquidation_up_probability,
                liquidation_down_probability=liquidation_down_probability,
                min_round_trip_edge=min_round_trip_edge,
            )
            market_up_probability = executable_up_probability
        else:
            trigger_state = classify_trigger_state(
                market_up_probability=market_up_probability,
                fair_up_probability=fair,
                min_edge=min_edge,
                time_remaining_seconds=remaining,
                min_time_remaining_seconds=min_time_remaining_seconds,
                max_time_remaining_seconds=max_time_remaining_seconds,
            )
        print(
            render_watch_line(
                label=label,
                now=now,
                hour_open=hour_open,
                market_up_probability=market_up_probability,
                fair_up_probability=fair,
                trigger_state=trigger_state,
            )
        )
        sys.stdout.flush()
        if once:
            return 0
        time.sleep(max(0.0, poll_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch one Polymarket BTC hourly contract and alert on model-vs-market edge.")
    parser.add_argument("--label", required=True, help="Human-readable market label.")
    parser.add_argument("--up-token-id", default="", help="Polymarket token id for the Up outcome.")
    parser.add_argument("--down-token-id", default="", help="Polymarket token id for the Down outcome.")
    parser.add_argument("--market-slug", default="", help="Polymarket Gamma market slug for automatic hourly market ingestion.")
    parser.add_argument("--market-slug-prefix", default="", help="Slug prefix for current-hour market automation, for example bitcoin-up-or-down.")
    parser.add_argument("--binance-symbol", default=DEFAULT_BINANCE_SYMBOL, help="Binance symbol for the underlying reference.")
    parser.add_argument("--host", default=DEFAULT_CLOB_HOST, help="Polymarket CLOB host.")
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_HOST, help="Polymarket Gamma host.")
    parser.add_argument("--volatility", type=float, default=DEFAULT_VOLATILITY, help="Short-horizon volatility assumption.")
    parser.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE, help="Minimum edge required for a trigger.")
    parser.add_argument(
        "--execution-cost-buffer",
        type=float,
        default=DEFAULT_EXECUTION_COST_BUFFER,
        help="Conservative probability buffer subtracted from executable edge for fees and slippage.",
    )
    parser.add_argument(
        "--min-round-trip-edge",
        type=float,
        default=DEFAULT_MIN_ROUND_TRIP_EDGE,
        help="Minimum acceptable immediate liquidation sanity edge required before triggering.",
    )
    parser.add_argument(
        "--min-time-remaining-seconds",
        type=int,
        default=DEFAULT_MIN_TIME_REMAINING_SECONDS,
        help="Minimum time remaining before a trigger is allowed.",
    )
    parser.add_argument(
        "--max-time-remaining-seconds",
        type=int,
        default=DEFAULT_MAX_TIME_REMAINING_SECONDS,
        help="Maximum time remaining before a trigger is allowed.",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds.")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS, help="Polling interval.")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit.")
    parser.add_argument(
        "--market-up-probability",
        type=float,
        default=None,
        help="Optional manual override for market Up probability when token ids are unavailable.",
    )
    args = parser.parse_args()
    market_slug_prefix = args.market_slug_prefix.strip()
    if not market_slug_prefix:
        market_slug_prefix = DEFAULT_SLUG_PREFIX_BY_SYMBOL.get(args.binance_symbol.strip().upper(), "")
    if args.market_up_probability is None and not args.market_slug and not market_slug_prefix and (not args.up_token_id or not args.down_token_id):
        parser.error("provide --market-slug, --market-slug-prefix, or both --up-token-id/--down-token-id, or pass --market-up-probability")

    return run_watch(
        label=args.label,
        up_token_id=args.up_token_id or None,
        down_token_id=args.down_token_id or None,
        market_slug=args.market_slug or None,
        market_slug_prefix=market_slug_prefix or None,
        binance_symbol=args.binance_symbol.strip().upper(),
        host=args.host,
        gamma_host=args.gamma_host,
        volatility=args.volatility,
        min_edge=args.min_edge,
        min_time_remaining_seconds=args.min_time_remaining_seconds,
        max_time_remaining_seconds=args.max_time_remaining_seconds,
        timeout=args.timeout,
        poll_seconds=args.poll_seconds,
        once=bool(args.once),
        market_up_probability_override=args.market_up_probability,
        execution_cost_buffer=args.execution_cost_buffer,
        min_round_trip_edge=args.min_round_trip_edge,
    )


if __name__ == "__main__":
    raise SystemExit(main())
