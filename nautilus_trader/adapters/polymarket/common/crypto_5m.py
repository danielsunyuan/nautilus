from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from math import ceil
from typing import Any
from urllib.parse import urlparse

import ipaddress
import msgspec

from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
from nautilus_trader.core.nautilus_pyo3 import HttpClient
from nautilus_trader.model.identifiers import InstrumentId


DEFAULT_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
ROUND_INTERVAL_SECONDS = 300
SUPPORTED_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
_UP_LABELS = frozenset({"up", "yes"})
_DOWN_LABELS = frozenset({"down", "no"})


@dataclass(frozen=True)
class PolymarketCrypto5mSession:
    asset: str
    slug: str
    round_start: datetime
    end_time: datetime
    condition_id: str
    question: str | None
    token_ids: dict[str, str]
    instrument_ids: dict[str, InstrumentId]
    active: bool | None
    closed: bool | None
    archived: bool | None
    accepting_orders: bool | None


def normalize_crypto_asset(asset: str) -> str:
    symbol = asset.strip().upper()
    if symbol not in SUPPORTED_ASSETS:
        raise ValueError(f"unsupported asset {asset!r}; choose one of {SUPPORTED_ASSETS}")
    return symbol


def _normalize_base_url(base_url: str | None) -> str:
    return validate_http_base_url(base_url or DEFAULT_GAMMA_BASE_URL, name="gamma_base_url")


def _validate_hostname(hostname: str | None, *, name: str) -> str:
    if not hostname:
        raise ValueError(f"{name} must include a hostname")
    lowered = hostname.strip().lower()
    if lowered == "localhost":
        raise ValueError(f"{name} must not target localhost")
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return lowered
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise ValueError(f"{name} must not target a private or local address")
    return lowered


def _validate_url(url: str, *, schemes: tuple[str, ...], name: str) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in schemes:
        raise ValueError(f"{name} must use one of {schemes}")
    if not parsed.netloc:
        raise ValueError(f"{name} must include a network location")
    _validate_hostname(parsed.hostname, name=name)
    return parsed.geturl().rstrip("/")


def validate_http_base_url(base_url: str, *, name: str = "base_url") -> str:
    return _validate_url(base_url, schemes=("http", "https"), name=name)


def validate_ws_url(url: str, *, name: str = "wss_url") -> str:
    return _validate_url(url, schemes=("ws", "wss"), name=name)


def _round_start_epoch(now: datetime) -> int:
    epoch = int(now.astimezone(UTC).timestamp())
    return epoch - (epoch % ROUND_INTERVAL_SECONDS)


def _coerce_list(value: Any, *, name: str) -> list[Any]:
    if isinstance(value, str):
        try:
            parsed = msgspec.json.decode(value.encode())
        except msgspec.DecodeError as exc:
            raise ValueError(f"unexpected {name} payload") from exc
        if isinstance(parsed, list):
            return parsed
    if isinstance(value, list):
        return value
    raise ValueError(f"unexpected {name} payload")


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(UTC)
    return None


def _parse_round_start(slug: str) -> datetime:
    try:
        round_start = int(str(slug).rsplit("-", maxsplit=1)[-1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unable to parse round start from slug {slug!r}") from exc
    return datetime.fromtimestamp(round_start, tz=UTC)


def _normalize_outcome_label(value: Any) -> str | None:
    label = str(value).strip().lower()
    if label in _UP_LABELS:
        return "up"
    if label in _DOWN_LABELS:
        return "down"
    return None


def current_crypto_5m_market_slug(*, asset: str, now: datetime) -> str:
    symbol = normalize_crypto_asset(asset)
    round_start = _round_start_epoch(now)
    return f"{symbol.lower()}-updown-5m-{round_start}"


def candidate_crypto_5m_market_slugs(*, asset: str, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(tz=UTC)
    symbol = normalize_crypto_asset(asset).lower()
    current = _round_start_epoch(now)
    return [
        f"{symbol}-updown-5m-{current}",
        f"{symbol}-updown-5m-{current - ROUND_INTERVAL_SECONDS}",
    ]


def build_forward_crypto_5m_slugs(
    *,
    assets: tuple[str, ...] = SUPPORTED_ASSETS,
    now: datetime | None = None,
    intervals: int = 2,
) -> list[str]:
    now = now or datetime.now(tz=UTC)
    round_start = _round_start_epoch(now)
    slugs: list[str] = []
    for i in range(intervals):
        ts = round_start + (ROUND_INTERVAL_SECONDS * i)
        for asset in assets:
            symbol = normalize_crypto_asset(asset).lower()
            slugs.append(f"{symbol}-updown-5m-{ts}")
    return slugs


def parse_crypto_5m_market(payload: dict[str, Any], *, asset: str) -> PolymarketCrypto5mSession:
    symbol = normalize_crypto_asset(asset)
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        raise ValueError("Gamma market payload missing slug")

    condition_id = str(payload.get("conditionId") or "").strip()
    if not condition_id:
        raise ValueError("Gamma market payload missing conditionId")

    outcomes = _coerce_list(payload.get("outcomes", []), name="outcomes")
    token_ids = _coerce_list(payload.get("clobTokenIds", []), name="clobTokenIds")
    if len(outcomes) != len(token_ids):
        raise ValueError("Gamma outcome arrays do not align")

    resolved_token_ids: dict[str, str] = {}
    instrument_ids: dict[str, InstrumentId] = {}
    for outcome, token_id in zip(outcomes, token_ids, strict=False):
        side = _normalize_outcome_label(outcome)
        token = str(token_id).strip()
        if side is None or not token:
            continue
        resolved_token_ids[side] = token
        instrument_ids[side] = get_polymarket_instrument_id(condition_id, token)

    if "up" not in resolved_token_ids or "down" not in resolved_token_ids:
        raise ValueError("Gamma market payload missing Up/Down outcomes")

    round_start = _parse_round_start(slug)
    slug_end_time = round_start + timedelta(seconds=ROUND_INTERVAL_SECONDS)
    end_time = slug_end_time

    return PolymarketCrypto5mSession(
        asset=symbol,
        slug=slug,
        round_start=round_start,
        end_time=end_time,
        condition_id=condition_id,
        question=str(payload.get("question")) if payload.get("question") is not None else None,
        token_ids=resolved_token_ids,
        instrument_ids=instrument_ids,
        active=payload.get("active"),
        closed=payload.get("closed"),
        archived=payload.get("archived"),
        accepting_orders=payload.get("acceptingOrders", payload.get("accepting_orders")),
    )


def validate_crypto_5m_market(
    session: PolymarketCrypto5mSession,
    *,
    now: datetime | None = None,
) -> None:
    if session.closed is True:
        raise ValueError(f"market {session.slug!r} is closed")
    if session.archived is True:
        raise ValueError(f"market {session.slug!r} is archived")
    if session.active is not True:
        raise ValueError(f"market {session.slug!r} is inactive")
    if session.accepting_orders is not True:
        raise ValueError(f"market {session.slug!r} is not accepting orders")
    resolved_now = now or datetime.now(tz=UTC)
    if session.end_time <= resolved_now:
        raise ValueError(
            f"market {session.slug!r} is expired at {session.end_time.isoformat()}",
        )


async def fetch_crypto_5m_market(
    *,
    slug: str,
    http_client: HttpClient,
    gamma_base_url: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    response = await http_client.get(
        url=f"{_normalize_base_url(gamma_base_url)}/markets/slug/{slug}",
        timeout_secs=max(1, ceil(timeout)),
    )

    if response.status == 404:
        raise ValueError(f"Market with slug '{slug}' not found")

    if response.status != 200:
        body = response.body.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP request failed with status {response.status}: {body}")

    payload = msgspec.json.decode(response.body)
    if isinstance(payload, list):
        if not payload:
            raise ValueError(f"Market with slug '{slug}' not found")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Unexpected response type for slug '{slug}': {type(payload).__name__}",
        )
    return payload


async def resolve_crypto_5m_session(
    *,
    asset: str,
    http_client: HttpClient,
    now: datetime | None = None,
    gamma_base_url: str | None = None,
    timeout: float = 10.0,
    validate_open: bool = True,
) -> PolymarketCrypto5mSession:
    last_error: Exception | None = None
    resolved_now = now or datetime.now(tz=UTC)
    candidates = candidate_crypto_5m_market_slugs(asset=asset, now=resolved_now)

    for slug in candidates:
        try:
            payload = await fetch_crypto_5m_market(
                slug=slug,
                http_client=http_client,
                gamma_base_url=gamma_base_url,
                timeout=timeout,
            )
            session = parse_crypto_5m_market(payload, asset=asset)
            if validate_open:
                validate_crypto_5m_market(session, now=resolved_now)
            return session
        except (RuntimeError, ValueError) as exc:
            last_error = exc
            continue

    message = f"could not resolve a live 5m market for {asset!r}; tried {candidates!r}"
    if last_error is not None:
        raise RuntimeError(message) from last_error
    raise RuntimeError(message)
