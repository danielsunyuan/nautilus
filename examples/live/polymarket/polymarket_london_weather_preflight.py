#!/usr/bin/env python3
"""
Paper-trading readiness gate for London weather model Polymarket work.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

try:
    from examples.live.polymarket.london_weather_family_b_snapshot import (
        build_family_b_live_snapshot_from_research_path,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.london_weather_family_b_snapshot"
    module_path = Path(__file__).resolve().with_name("london_weather_family_b_snapshot.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    build_family_b_live_snapshot_from_research_path = module.build_family_b_live_snapshot_from_research_path

try:
    from examples.live.polymarket.london_weather_market_filter import (
        resolve_tradeable_london_weather_markets,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.london_weather_market_filter"
    module_path = Path(__file__).resolve().with_name("london_weather_market_filter.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    resolve_tradeable_london_weather_markets = module.resolve_tradeable_london_weather_markets


SNAPSHOT_FIELDS = (
    "target_local_date",
    "forecast_horizon_days",
    "market_line",
    "model_version",
    "predicted_probability",
    "raw_predicted_probability",
    "training_row_count",
)

QUOTE_FIELDS = (
    "yes_bid",
    "yes_ask",
    "spread",
    "yes_bid_size",
    "yes_ask_size",
    "timestamp",
)

TRUTHY = {"1", "true", "yes", "y", "on"}
DEFAULT_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_CLOB_BASE_URL = "https://clob.polymarket.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def _blank_report() -> dict[str, Any]:
    return {
        "live_data_status": "blocked",
        "execution_mode": "sandbox_only",
        "model_status": "blocked",
        "model_snapshot_status": "blocked",
        "market_discovery_status": "blocked",
        "resolution_status": "blocked",
        "market_data_status": "blocked",
        "ready_for_paper_round": False,
        "blocking_reasons": [],
        "model_snapshot": None,
        "accepted_markets": [],
        "rejected_markets": [],
    }


def _check_live_data_gate(env: dict[str, str], report: dict[str, Any]) -> None:
    reasons: list[str] = []
    if str(env.get("POLYMARKET_LIVE_DATA_READY", "")).strip().lower() != "yes":
        reasons.append("POLYMARKET_LIVE_DATA_READY=yes is required")
    if _is_truthy(env.get("POLYMARKET_FORCE_LIVE_EXECUTION")):
        reasons.append("POLYMARKET_FORCE_LIVE_EXECUTION requested live execution; paper preflight is sandbox-only")

    if reasons:
        report["live_data_status"] = "blocked"
        report["blocking_reasons"].extend(reasons)
    else:
        report["live_data_status"] = "passed"


def _check_model_path(env: dict[str, str], report: dict[str, Any]) -> Path | None:
    raw_path = str(env.get("WEATHER_RESEARCH_PATH", "")).strip()
    if not raw_path:
        report["model_status"] = "blocked"
        report["blocking_reasons"].append("WEATHER_RESEARCH_PATH is required")
        return None

    model_path = Path(raw_path)
    if not model_path.exists():
        report["model_status"] = "blocked"
        report["blocking_reasons"].append(str(model_path))
        return None
    if not model_path.is_dir():
        report["model_status"] = "blocked"
        report["blocking_reasons"].append(f"{model_path} is not a directory")
        return None

    report["model_status"] = "passed"
    return model_path


def _load_fixture(fixture_path: str | Path | None) -> dict[str, Any] | None:
    if fixture_path is None:
        return None
    payload = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("fixture payload must be a JSON object")
    return payload


def _normalize_snapshot_row(snapshot: dict[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
    missing = [field for field in SNAPSHOT_FIELDS if field not in snapshot]
    if missing:
        report["blocking_reasons"].append(f"model_snapshot missing fields: {', '.join(missing)}")
        return None

    try:
        probability = float(snapshot["predicted_probability"])
        raw_probability = float(snapshot["raw_predicted_probability"])
        training_rows = int(snapshot["training_row_count"])
    except (TypeError, ValueError):
        report["blocking_reasons"].append("model_snapshot probability and row count fields must be numeric")
        return None

    if not 0.0 <= probability <= 1.0 or not 0.0 <= raw_probability <= 1.0 or training_rows <= 0:
        report["blocking_reasons"].append("model_snapshot values are outside accepted ranges")
        return None

    normalized = {field: snapshot[field] for field in SNAPSHOT_FIELDS}
    normalized["predicted_probability"] = probability
    normalized["raw_predicted_probability"] = raw_probability
    normalized["training_row_count"] = training_rows
    return normalized


def _build_model_snapshot(
    fixture: dict[str, Any] | None,
    report: dict[str, Any],
    *,
    snapshot_payload: Any = None,
) -> Any:
    if snapshot_payload is None and fixture is None:
        report["model_snapshot_status"] = "blocked"
        report["blocking_reasons"].append("model snapshot fixture is required in no-network mode")
        return None

    snapshot = snapshot_payload if snapshot_payload is not None else fixture.get("model_snapshot")
    if isinstance(snapshot, list):
        normalized_rows = []
        for row in snapshot:
            if not isinstance(row, dict):
                report["model_snapshot_status"] = "blocked"
                report["blocking_reasons"].append("model_snapshot rows must be JSON objects")
                return None
            normalized = _normalize_snapshot_row(row, report)
            if normalized is None:
                report["model_snapshot_status"] = "blocked"
                return None
            normalized_rows.append(normalized)
        if not normalized_rows:
            report["model_snapshot_status"] = "blocked"
            report["blocking_reasons"].append("model_snapshot must contain at least one row")
            return None
        report["model_snapshot_status"] = "passed"
        report["model_snapshot"] = normalized_rows
        return normalized_rows

    if not isinstance(snapshot, dict):
        report["model_snapshot_status"] = "blocked"
        report["blocking_reasons"].append("model_snapshot fixture object is required")
        return None

    normalized = _normalize_snapshot_row(snapshot, report)
    if normalized is None:
        report["model_snapshot_status"] = "blocked"
        return None
    report["model_snapshot_status"] = "passed"
    report["model_snapshot"] = normalized
    return normalized


def _resolution_is_eglc_aligned(market: dict[str, Any]) -> bool:
    text = " ".join(
        str(market.get(key, ""))
        for key in ("resolution_source", "rules", "description", "question")
    ).lower()
    return "eglc" in text or ("wunderground" in text and "london city" in text)


def _quote_is_ready(quote: Any) -> bool:
    if not isinstance(quote, dict):
        return False
    for field in QUOTE_FIELDS:
        if quote.get(field) in (None, ""):
            return False
    try:
        yes_bid = float(quote["yes_bid"])
        yes_ask = float(quote["yes_ask"])
        spread = float(quote["spread"])
        yes_bid_size = float(quote["yes_bid_size"])
        yes_ask_size = float(quote["yes_ask_size"])
    except (TypeError, ValueError):
        return False
    if not (0.0 <= yes_bid <= 1.0 and 0.0 <= yes_ask <= 1.0):
        return False
    if yes_bid > yes_ask:
        return False
    return spread >= 0.0 and yes_bid_size > 0.0 and yes_ask_size > 0.0


def _reject(report: dict[str, Any], market: dict[str, Any], reason: str) -> None:
    report["rejected_markets"].append(
        {
            "condition_id": market.get("condition_id") or market.get("conditionId"),
            "reason": reason,
        },
    )


def _fetch_json(url: str, *, timeout_seconds: float) -> Any:
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"GET failed for {url}: {exc}") from exc


def _best_bid_ask_from_book(book: dict[str, Any]) -> dict[str, Any]:
    bids = book.get("bids") if isinstance(book.get("bids"), list) else []
    asks = book.get("asks") if isinstance(book.get("asks"), list) else []

    def _level(levels: list[Any], *, best_bid: bool) -> tuple[float | None, float | None]:
        parsed = []
        for level in levels:
            if not isinstance(level, dict):
                continue
            try:
                parsed.append((float(level["price"]), float(level["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not parsed:
            return None, None
        price, size = max(parsed, key=lambda item: item[0]) if best_bid else min(parsed, key=lambda item: item[0])
        return price, size

    bid, bid_size = _level(bids, best_bid=True)
    ask, ask_size = _level(asks, best_bid=False)
    return {
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
    }


def _fetch_token_book(
    *,
    clob_base_url: str,
    token_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    token = quote(str(token_id), safe="")
    book = _fetch_json(f"{clob_base_url.rstrip('/')}/book?token_id={token}", timeout_seconds=timeout_seconds)
    if not isinstance(book, dict):
        raise RuntimeError(f"CLOB book response for token {token_id} was not a JSON object")
    return _best_bid_ask_from_book(book)


def _fetch_market_details(
    *,
    gamma_base_url: str,
    slug: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if not slug:
        return {}
    data = _fetch_json(
        f"{gamma_base_url.rstrip('/')}/markets/slug/{quote(str(slug), safe='')}",
        timeout_seconds=timeout_seconds,
    )
    return data if isinstance(data, dict) else {}


def _load_temperature_resolver_module() -> Any:
    module_name = "polymarket_weather_daily_temperature_resolver_local"
    module_path = Path(__file__).resolve().with_name("weather_daily_temperature_resolver.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _fetch_weather_event_markets_raw(
    *,
    gamma_base_url: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    offset = 0
    limit = 100
    markets: list[dict[str, Any]] = []
    while True:
        url = (
            f"{gamma_base_url.rstrip('/')}/events?"
            f"tag_slug=weather&active=true&closed=false&limit={limit}&offset={offset}"
        )
        events = _fetch_json(url, timeout_seconds=timeout_seconds)
        if not isinstance(events, list):
            break
        for event in events:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets", []):
                if not isinstance(market, dict):
                    continue
                if "tokens" not in market:
                    clob_ids = market.get("clobTokenIds")
                    outcomes = market.get("outcomes")
                    if isinstance(clob_ids, str):
                        try:
                            clob_ids = json.loads(clob_ids)
                        except (TypeError, ValueError):
                            clob_ids = None
                    if isinstance(outcomes, str):
                        try:
                            outcomes = json.loads(outcomes)
                        except (TypeError, ValueError):
                            outcomes = None
                    if isinstance(clob_ids, list) and isinstance(outcomes, list) and len(clob_ids) == len(outcomes):
                        market["tokens"] = [
                            {"token_id": token_id, "outcome": outcome}
                            for token_id, outcome in zip(clob_ids, outcomes)
                        ]
                markets.append(market)
        if len(events) < limit:
            break
        offset += limit
    return markets


def _resolve_london_markets_raw(
    *,
    gamma_base_url: str,
    timeout_seconds: float,
) -> list[Any]:
    resolver = _load_temperature_resolver_module()
    raw_markets = _fetch_weather_event_markets_raw(
        gamma_base_url=gamma_base_url,
        timeout_seconds=timeout_seconds,
    )
    parsed = [
        market
        for raw_market in raw_markets
        if (market := resolver.parse_daily_temperature_market(raw_market)) is not None
    ]
    tradeable = resolver.filter_tradeable_daily_temperature_markets(parsed, date.today())
    return [
        market
        for market in tradeable
        if market.city == "London"
        and market.metric == "high"
        and market.band_type in {"or_higher", "or_lower"}
        and market.active
        and market.accepting_orders
        and market.condition_id
        and market.yes_token_id
        and market.no_token_id
    ]


def _market_attr(market: Any, name: str, default: Any = None) -> Any:
    if isinstance(market, dict):
        return market.get(name, default)
    return getattr(market, name, default)


def _json_safe_date(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _live_market_to_preflight_dict(
    market: Any,
    *,
    details: dict[str, Any],
    quote_data: dict[str, Any],
) -> dict[str, Any]:
    yes = quote_data["yes"]
    no = quote_data["no"]
    yes_bid = yes["bid"]
    yes_ask = yes["ask"]
    spread = None if yes_bid is None or yes_ask is None else float(yes_ask) - float(yes_bid)
    return {
        "slug": _market_attr(market, "slug", ""),
        "city": _market_attr(market, "city", ""),
        "metric": _market_attr(market, "metric", ""),
        "band_type": _market_attr(market, "band_type", ""),
        "threshold_f": _market_attr(market, "threshold_f", None),
        "observation_date": _json_safe_date(_market_attr(market, "observation_date", None)),
        "condition_id": _market_attr(market, "condition_id", ""),
        "yes_token_id": _market_attr(market, "yes_token_id", ""),
        "no_token_id": _market_attr(market, "no_token_id", ""),
        "active": bool(_market_attr(market, "active", False)),
        "accepting_orders": bool(_market_attr(market, "accepting_orders", False)),
        "resolution_source": details.get("resolution_source") or details.get("resolutionSource") or "",
        "rules": details.get("rules") or "",
        "description": details.get("description") or "",
        "question": details.get("question") or "",
        "quote": {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no["bid"],
            "no_ask": no["ask"],
            "spread": spread,
            "yes_bid_size": yes["bid_size"],
            "yes_ask_size": yes["ask_size"],
            "no_bid_size": no["bid_size"],
            "no_ask_size": no["ask_size"],
            "timestamp": datetime.now().astimezone().isoformat(),
        },
    }


def _discover_live_markets(
    *,
    gamma_base_url: str,
    clob_base_url: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    try:
        from nautilus_trader.core.nautilus_pyo3 import HttpClient

        async def _resolve() -> Any:
            return await resolve_tradeable_london_weather_markets(
                http_client=HttpClient(),
                gamma_base_url=gamma_base_url,
                today=date.today(),
                timeout_seconds=timeout_seconds,
            )

        result = asyncio.run(_resolve())
        accepted_markets = result.accepted
    except ModuleNotFoundError:
        accepted_markets = _resolve_london_markets_raw(
            gamma_base_url=gamma_base_url,
            timeout_seconds=timeout_seconds,
        )

    markets = []
    for market in accepted_markets:
        details = _fetch_market_details(
            gamma_base_url=gamma_base_url,
            slug=str(_market_attr(market, "slug", "")),
            timeout_seconds=timeout_seconds,
        )
        quote_data = {
            "yes": _fetch_token_book(
                clob_base_url=clob_base_url,
                token_id=str(_market_attr(market, "yes_token_id", "")),
                timeout_seconds=timeout_seconds,
            ),
            "no": _fetch_token_book(
                clob_base_url=clob_base_url,
                token_id=str(_market_attr(market, "no_token_id", "")),
                timeout_seconds=timeout_seconds,
            ),
        }
        markets.append(_live_market_to_preflight_dict(market, details=details, quote_data=quote_data))
    return markets


def _build_live_model_snapshot(
    *,
    accepted_markets: list[dict[str, Any]],
    model_path: Path,
) -> list[dict[str, Any]]:
    market_lines = [float(market["threshold_f"]) for market in accepted_markets if market.get("threshold_f") is not None]
    target_dates = [market["observation_date"] for market in accepted_markets if market.get("observation_date")]
    return build_family_b_live_snapshot_from_research_path(
        research_path=model_path,
        market_lines=market_lines,
        target_local_dates=target_dates,
    )


def _validate_markets(
    fixture: dict[str, Any] | None,
    report: dict[str, Any],
    *,
    raw_markets: list[dict[str, Any]] | None = None,
) -> None:
    if raw_markets is None and fixture is None:
        report["blocking_reasons"].append("market fixture is required in no-network mode")
        return

    raw_markets = raw_markets if raw_markets is not None else fixture.get("markets")
    if not isinstance(raw_markets, list) or not raw_markets:
        report["blocking_reasons"].append("at least one market fixture is required")
        return

    discovery_blocked = False
    resolution_blocked = False
    market_data_blocked = False
    accepted: list[dict[str, Any]] = []
    model_snapshot = report.get("model_snapshot") if isinstance(report.get("model_snapshot"), dict) else {}

    for market in raw_markets:
        if not isinstance(market, dict):
            discovery_blocked = True
            continue

        if str(market.get("city", "")).strip().lower() != "london":
            _reject(report, market, "not_london")
            continue
        if str(market.get("metric", "")).strip().lower() != "high":
            _reject(report, market, "unsupported_metric")
            continue
        if str(market.get("band_type", "")).strip().lower() == "exact":
            discovery_blocked = True
            _reject(report, market, "unsupported_exact_bucket")
            continue
        if str(market.get("band_type", "")).strip().lower() not in {"or_higher", "or_lower"}:
            discovery_blocked = True
            _reject(report, market, "unsupported_band_type")
            continue
        if not all(
            market.get(field)
            for field in ("condition_id", "yes_token_id", "no_token_id")
        ):
            discovery_blocked = True
            _reject(report, market, "missing_market_identity")
            continue
        if not bool(market.get("active")) or not bool(market.get("accepting_orders")):
            discovery_blocked = True
            _reject(report, market, "not_accepting_orders")
            continue
        if not _resolution_is_eglc_aligned(market):
            resolution_blocked = True
            _reject(report, market, "resolution_not_eglc_aligned")
            continue
        if not _quote_is_ready(market.get("quote")):
            market_data_blocked = True
            _reject(report, market, "market_data_not_ready")
            continue

        accepted.append(
            {
                "slug": market.get("slug") or market.get("market_slug"),
                "city": "London",
                "metric": "high",
                "band_type": market["band_type"],
                "threshold_f": market.get("threshold_f", market.get("threshold", market.get("market_line", model_snapshot.get("market_line")))),
                "observation_date": _json_safe_date(market.get("observation_date") or market.get("target_local_date") or model_snapshot.get("target_local_date")),
                "condition_id": market["condition_id"],
                "yes_token_id": market["yes_token_id"],
                "no_token_id": market["no_token_id"],
                "accepting_orders": bool(market["accepting_orders"]),
                "yes_bid": market.get("quote", {}).get("yes_bid"),
                "yes_ask": market.get("quote", {}).get("yes_ask"),
                "no_ask": market.get("quote", {}).get("no_ask"),
                "best_ask": market.get("quote", {}).get("yes_ask"),
                "binary_yes_no_confirmed": bool(market.get("binary_yes_no_confirmed", True)),
                "or_lower_complement_confirmed": bool(market.get("or_lower_complement_confirmed", False)),
            },
        )

    report["accepted_markets"] = accepted
    report["market_discovery_status"] = "passed" if accepted else "blocked"
    report["resolution_status"] = "passed" if accepted else "blocked"
    report["market_data_status"] = "passed" if accepted else "blocked"

    if not accepted:
        report["blocking_reasons"].append("no London high-temperature markets passed preflight")
    if discovery_blocked and not accepted:
        report["blocking_reasons"].append("market discovery rejected one or more hard-gated markets")
    if resolution_blocked and not accepted:
        report["blocking_reasons"].append("resolution metadata must name London City Airport / Wunderground EGLC")
    if market_data_blocked and not accepted:
        report["blocking_reasons"].append("CLOB quote readiness requires bid, ask, spread, sizes, and timestamp")


def _finalize(report: dict[str, Any]) -> dict[str, Any]:
    hard_statuses = (
        "live_data_status",
        "model_status",
        "model_snapshot_status",
        "market_discovery_status",
        "resolution_status",
        "market_data_status",
    )
    report["ready_for_paper_round"] = all(report[status] == "passed" for status in hard_statuses)
    return report


def run_preflight(
    *,
    env: dict[str, str] | None = None,
    fixture_path: str | Path | None = None,
    no_network: bool = False,
    live_markets: list[dict[str, Any]] | None = None,
    live_model_snapshot_builder: Any = None,
    gamma_base_url: str = DEFAULT_GAMMA_BASE_URL,
    clob_base_url: str = DEFAULT_CLOB_BASE_URL,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    runtime_env = dict(os.environ if env is None else env)
    report = _blank_report()

    _check_live_data_gate(runtime_env, report)
    model_path = _check_model_path(runtime_env, report)

    fixture = _load_fixture(fixture_path)
    attempted_live_discovery = False
    if fixture is not None:
        _build_model_snapshot(fixture, report)
        _validate_markets(fixture, report)
    else:
        if (
            live_markets is None
            and not no_network
            and report["live_data_status"] == "passed"
            and model_path is not None
        ):
            attempted_live_discovery = True
            try:
                live_markets = _discover_live_markets(
                    gamma_base_url=gamma_base_url,
                    clob_base_url=clob_base_url,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:
                report["blocking_reasons"].append(f"live market discovery failed: {exc}")
        _validate_markets(fixture, report, raw_markets=live_markets)
        live_model_snapshot_builder = live_model_snapshot_builder or _build_live_model_snapshot
        if live_model_snapshot_builder is not None and report["accepted_markets"] and model_path is not None:
            try:
                snapshot_payload = live_model_snapshot_builder(
                    accepted_markets=report["accepted_markets"],
                    model_path=model_path,
                )
                _build_model_snapshot(fixture, report, snapshot_payload=snapshot_payload)
            except Exception as exc:
                report["model_snapshot_status"] = "blocked"
                report["blocking_reasons"].append(f"live model snapshot failed: {exc}")
        else:
            _build_model_snapshot(fixture, report)

    if not no_network and fixture is None and live_markets is None and not attempted_live_discovery:
        report["blocking_reasons"].append("live network discovery is not enabled by this preflight task")

    return _finalize(report)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-json", help="Local deterministic preflight fixture")
    parser.add_argument("--no-network", action="store_true", help="Use fixture data only")
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL)
    parser.add_argument("--clob-host", default=DEFAULT_CLOB_BASE_URL)
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    report = run_preflight(
        fixture_path=args.fixture_json,
        no_network=bool(args.no_network),
        gamma_base_url=args.gamma_host,
        clob_base_url=args.clob_host,
        timeout_seconds=args.timeout,
    )
    print(json.dumps(report, default=str, sort_keys=True))
    return 0 if report["ready_for_paper_round"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
