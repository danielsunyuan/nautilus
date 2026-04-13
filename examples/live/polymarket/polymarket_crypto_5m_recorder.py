#!/usr/bin/env python3
"""
Record Polymarket 5-minute crypto top-of-book quotes into a Nautilus catalog.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from decimal import InvalidOperation
import importlib.util
import json
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import quote
import urllib.request

try:
    from examples.live.polymarket._crypto_5m_support import DEFAULT_GAMMA_BASE_URL
    from examples.live.polymarket._crypto_5m_support import SUPPORTED_ASSETS
    from examples.live.polymarket._crypto_5m_support import resolve_crypto_5m_session
except ModuleNotFoundError:
    module_name = "examples.live.polymarket._crypto_5m_support"
    module_path = Path(__file__).resolve().with_name("_crypto_5m_support.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    DEFAULT_GAMMA_BASE_URL = module.DEFAULT_GAMMA_BASE_URL
    SUPPORTED_ASSETS = module.SUPPORTED_ASSETS
    resolve_crypto_5m_session = module.resolve_crypto_5m_session

try:
    from websockets.asyncio.client import connect
except ModuleNotFoundError:
    def connect(*args: Any, **kwargs: Any):  # type: ignore[misc]
        class _MissingConnection:
            async def __aenter__(self) -> Any:
                raise RuntimeError(
                    "websockets dependency is required for live recorder execution",
                )

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

        return _MissingConnection()

from nautilus_trader.core.nautilus_pyo3 import HttpClient
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog


DEFAULT_CATALOG_PATH = "/data/nautilus_catalog"
DEFAULT_WSS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_METADATA_FILE = "polymarket_5m_resolutions.jsonl"
DEFAULT_RESOLUTION_RETRIES = 3


@dataclass
class _TokenState:
    instrument_id: Any
    side: str
    best_bid: str | None = None
    best_ask: str | None = None
    best_bid_size: str | None = None
    best_ask_size: str | None = None


def _now_ns() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1_000_000_000)


def _decimal_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return text


def build_token_state(session: Any) -> dict[str, _TokenState]:
    return {
        token_id: _TokenState(instrument_id=session.instrument_ids[side], side=side)
        for side, token_id in session.token_ids.items()
    }


def quote_ticks_from_messages(
    *,
    token_state: dict[str, _TokenState],
    messages: Iterable[dict[str, Any]],
    ts_ns: int,
) -> list[QuoteTick]:
    batch: list[QuoteTick] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        token_id = str(message.get("asset_id") or message.get("asset") or "").strip()
        state = token_state.get(token_id)
        if state is None:
            continue

        bid_text = _decimal_text(message.get("best_bid")) or state.best_bid
        ask_text = _decimal_text(message.get("best_ask")) or state.best_ask
        if bid_text is None or ask_text is None:
            continue

        bid_size_text = _decimal_text(message.get("best_bid_size")) or state.best_bid_size or "0"
        ask_size_text = _decimal_text(message.get("best_ask_size")) or state.best_ask_size or "0"

        state.best_bid = bid_text
        state.best_ask = ask_text
        state.best_bid_size = bid_size_text
        state.best_ask_size = ask_size_text

        batch.append(
            QuoteTick(
                instrument_id=state.instrument_id,
                bid_price=Price.from_str(bid_text or "0"),
                ask_price=Price.from_str(ask_text or "0"),
                bid_size=Quantity.from_str(bid_size_text),
                ask_size=Quantity.from_str(ask_size_text),
                ts_event=ts_ns,
                ts_init=ts_ns,
            ),
        )
    return batch


class _BufferedCatalogWriter:
    def __init__(
        self,
        *,
        catalog: Any,
        flush_rows: int,
        flush_seconds: float,
        now_fn: Any = None,
    ) -> None:
        self._catalog = catalog
        self._flush_rows = max(1, int(flush_rows))
        self._flush_seconds = max(0.0, float(flush_seconds))
        self._now_fn = now_fn or time.monotonic
        self._buffer: list[Any] = []
        self._buffer_started_at: float | None = None

    def add(self, rows: list[Any]) -> int:
        if not rows:
            return 0
        now = float(self._now_fn())
        if self._buffer_started_at is None:
            self._buffer_started_at = now
        self._buffer.extend(rows)
        return self.flush_if_needed(now=now)

    def flush_if_needed(self, *, now: float | None = None) -> int:
        if not self._buffer:
            return 0
        current = float(self._now_fn()) if now is None else float(now)
        if len(self._buffer) >= self._flush_rows:
            return self.flush(force=True, reason="row_threshold", now=current)
        if self._flush_seconds > 0 and self._buffer_started_at is not None:
            if current - self._buffer_started_at >= self._flush_seconds:
                return self.flush(force=True, reason="time_threshold", now=current)
        return 0

    def flush(
        self,
        *,
        force: bool = False,
        reason: str = "manual",
        now: float | None = None,
    ) -> int:
        if not self._buffer:
            return 0
        current = float(self._now_fn()) if now is None else float(now)
        if not force and self._buffer_started_at is not None:
            if len(self._buffer) < self._flush_rows:
                if self._flush_seconds <= 0:
                    return 0
                if current - self._buffer_started_at < self._flush_seconds:
                    return 0
        batch = list(self._buffer)
        self._catalog.write_data(batch)
        self._buffer = []
        self._buffer_started_at = None
        print(
            json.dumps(
                {
                    "event": "polymarket_5m_catalog_flush",
                    "reason": reason,
                    "rows": len(batch),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return len(batch)


def metadata_path_for_catalog(catalog_path: str | Path) -> Path:
    return Path(catalog_path) / "metadata" / DEFAULT_METADATA_FILE


def _extract_resolution(payload: dict[str, Any]) -> dict[str, Any]:
    outcomes = payload.get("outcomes")
    outcome_prices = payload.get("outcomePrices") or payload.get("outcome_prices")
    resolved_outcome = payload.get("resolvedOutcome") or payload.get("resolved_outcome")
    if (
        resolved_outcome is None
        and payload.get("closed") is True
        and isinstance(outcomes, list)
        and isinstance(outcome_prices, list)
        and len(outcomes) == len(outcome_prices)
        and outcomes
    ):
        try:
            prices = [float(value) for value in outcome_prices]
        except (TypeError, ValueError):
            pass
        else:
            winner = max(range(len(prices)), key=prices.__getitem__)
            resolved_outcome = outcomes[winner]

    return {
        "slug": payload.get("slug"),
        "question": payload.get("question"),
        "closed": payload.get("closed"),
        "resolved": payload.get("resolved"),
        "resolved_outcome": resolved_outcome,
        "end_date": payload.get("endDate") or payload.get("end_date"),
        "outcomes": outcomes,
        "outcome_prices": outcome_prices,
        "clob_token_ids": payload.get("clobTokenIds") or payload.get("clob_token_ids"),
    }


def _last_resolution_by_slug(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            slug = str(row.get("market_slug") or row.get("slug") or "").strip()
            if slug:
                latest[slug] = row
    return latest


def _append_jsonl_if_changed(*, path: Path, payload: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    slug = str(payload.get("market_slug") or "").strip()
    if slug:
        latest = _last_resolution_by_slug(path).get(slug)
        if isinstance(latest, dict):
            if (
                latest.get("resolved_outcome") == payload.get("resolved_outcome")
                and latest.get("closed") == payload.get("closed")
                and latest.get("resolved") == payload.get("resolved")
            ):
                return False
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")
    return True


def write_market_resolution(
    *,
    catalog_path: str | Path,
    asset: str,
    session: Any,
    payload: dict[str, Any],
) -> bool:
    resolution = _extract_resolution(payload)
    resolution.update(
        {
            "event": "polymarket_5m_resolution",
            "asset": asset,
            "market_slug": session.slug,
            "condition_id": session.condition_id,
            "recorded_at": datetime.now(tz=UTC).isoformat(),
            "up_token_id": session.token_ids["up"],
            "down_token_id": session.token_ids["down"],
        },
    )
    return _append_jsonl_if_changed(
        path=metadata_path_for_catalog(catalog_path),
        payload=resolution,
    )


def _fetch_gamma_market_payload(*, gamma_host: str, slug: str, timeout: float) -> dict[str, Any]:
    base = str(gamma_host).rstrip("/")
    url = f"{base}/markets/slug/{quote(slug)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=float(timeout)) as response:
        payload = json.load(response)
    if isinstance(payload, list):
        if not payload:
            raise ValueError(f"market {slug!r} not found")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError("unexpected Gamma market payload")
    return payload


async def _write_resolution_snapshot(
    *,
    catalog_path: str | Path,
    asset: str,
    session: Any,
    gamma_host: str,
    timeout: float,
) -> None:
    payload = await asyncio.to_thread(
        _fetch_gamma_market_payload,
        gamma_host=gamma_host,
        slug=session.slug,
        timeout=timeout,
    )
    write_market_resolution(
        catalog_path=catalog_path,
        asset=asset,
        session=session,
        payload=payload,
    )


def _is_recoverable_stream_error(exc: Exception) -> bool:
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return True
    return "ConnectionClosed" in type(exc).__name__


def _is_recoverable_asset_loop_error(exc: Exception) -> bool:
    return isinstance(exc, (ConnectionError, OSError, TimeoutError))


def _is_recoverable_resolution_error(exc: Exception) -> bool:
    return isinstance(exc, (RuntimeError, ConnectionError, OSError, TimeoutError))


async def _finalize_market(
    *,
    writer: _BufferedCatalogWriter,
    catalog_path: str | Path,
    session: Any,
    gamma_host: str,
    timeout: float,
    reason: str,
    reconnect_delay: float,
    resolution_retries: int = DEFAULT_RESOLUTION_RETRIES,
) -> None:
    writer.flush(force=True, reason=reason)
    for attempt in range(1, max(1, resolution_retries) + 1):
        try:
            await _write_resolution_snapshot(
                catalog_path=catalog_path,
                asset=session.asset,
                session=session,
                gamma_host=gamma_host,
                timeout=timeout,
            )
            return
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "attempt": attempt,
                        "event": "polymarket_5m_resolution_error",
                        "market_slug": session.slug,
                        "reason": str(exc),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if attempt >= max(1, resolution_retries):
                return
            await asyncio.sleep(reconnect_delay)


async def _record_one_market(
    *,
    catalog: Any,
    session: Any,
    gamma_host: str,
    wss_url: str,
    timeout: float,
    max_ticks: int,
    total_ticks: int,
    catalog_path: str | Path = DEFAULT_CATALOG_PATH,
    reconnect_delay: float = 1.0,
    flush_rows: int = 5000,
    flush_seconds: float = 60.0,
) -> int:
    token_state = build_token_state(session)
    subscribe = {
        "type": "market",
        "assets_ids": [
            session.token_ids["up"],
            session.token_ids["down"],
        ],
        "custom_feature_enabled": True,
    }
    writer = _BufferedCatalogWriter(
        catalog=catalog,
        flush_rows=flush_rows,
        flush_seconds=flush_seconds,
    )
    poll_timeout = min(1.0, float(flush_seconds)) if float(flush_seconds) > 0 else 1.0

    while True:
        if datetime.now(tz=UTC) >= session.end_time:
            await _finalize_market(
                writer=writer,
                catalog_path=catalog_path,
                session=session,
                gamma_host=gamma_host,
                timeout=timeout,
                reason="market_end",
                reconnect_delay=reconnect_delay,
            )
            return total_ticks

        try:
            async with connect(wss_url, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(subscribe))
                while True:
                    if datetime.now(tz=UTC) >= session.end_time:
                        await _finalize_market(
                            writer=writer,
                            catalog_path=catalog_path,
                            session=session,
                            gamma_host=gamma_host,
                            timeout=timeout,
                            reason="market_end",
                            reconnect_delay=reconnect_delay,
                        )
                        return total_ticks

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=poll_timeout)
                    except TimeoutError:
                        writer.flush_if_needed()
                        continue
                    except StopAsyncIteration:
                        break

                    payload = json.loads(raw)
                    messages = payload if isinstance(payload, list) else [payload]
                    batch = quote_ticks_from_messages(
                        token_state=token_state,
                        messages=messages,
                        ts_ns=_now_ns(),
                    )
                    if not batch:
                        continue

                    writer.add(batch)
                    total_ticks += len(batch)
                    if max_ticks and total_ticks >= max_ticks:
                        writer.flush(force=True, reason="max_ticks")
                        return total_ticks
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _is_recoverable_stream_error(exc):
                raise
            print(
                json.dumps(
                    {
                        "event": "polymarket_5m_reconnect",
                        "market_slug": session.slug,
                        "reason": str(exc),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            await asyncio.sleep(reconnect_delay)
            continue

        await asyncio.sleep(reconnect_delay)


async def _run_asset_loop(
    *,
    catalog: ParquetDataCatalog,
    catalog_path: str | Path,
    asset: str,
    gamma_host: str,
    wss_url: str,
    timeout: float,
    reconnect_delay: float,
    flush_rows: int,
    flush_seconds: float,
    max_ticks: int,
) -> int:
    total_ticks = 0
    http_client = HttpClient(timeout_secs=max(1, int(timeout)))
    while True:
        try:
            session = await resolve_crypto_5m_session(
                asset=asset,
                http_client=http_client,
                gamma_base_url=gamma_host,
                timeout=timeout,
            )
            print(
                json.dumps(
                    {
                        "event": "polymarket_5m_market_start",
                        "asset": asset,
                        "market_slug": session.slug,
                        "end_time": session.end_time.isoformat(),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _is_recoverable_resolution_error(exc):
                print(
                    json.dumps(
                        {
                            "event": "polymarket_5m_asset_loop_stopped",
                            "asset": asset,
                            "reason": str(exc),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return total_ticks
            print(
                json.dumps(
                    {
                        "event": "polymarket_5m_asset_loop_error",
                        "asset": asset,
                        "reason": str(exc),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            await asyncio.sleep(reconnect_delay)
            continue

        try:
            total_ticks = await _record_one_market(
                catalog=catalog,
                session=session,
                gamma_host=gamma_host,
                wss_url=wss_url,
                timeout=timeout,
                max_ticks=max_ticks,
                total_ticks=total_ticks,
                catalog_path=catalog_path,
                reconnect_delay=reconnect_delay,
                flush_rows=flush_rows,
                flush_seconds=flush_seconds,
            )
            if max_ticks and total_ticks >= max_ticks:
                return total_ticks
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _is_recoverable_asset_loop_error(exc):
                print(
                    json.dumps(
                        {
                            "event": "polymarket_5m_asset_loop_stopped",
                            "asset": asset,
                            "reason": str(exc),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                return total_ticks
            print(
                json.dumps(
                    {
                        "event": "polymarket_5m_asset_loop_error",
                        "asset": asset,
                        "reason": str(exc),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            await asyncio.sleep(reconnect_delay)
            continue

        await asyncio.sleep(reconnect_delay)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-path", default=DEFAULT_CATALOG_PATH)
    parser.add_argument("--assets", default=",".join(SUPPORTED_ASSETS))
    parser.add_argument("--gamma-host", default=DEFAULT_GAMMA_BASE_URL)
    parser.add_argument("--wss-url", default=DEFAULT_WSS_MARKET)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--reconnect-delay", type=float, default=1.0)
    parser.add_argument("--flush-rows", type=int, default=5000)
    parser.add_argument("--flush-seconds", type=float, default=60.0)
    parser.add_argument("--max-ticks", type=int, default=0)
    return parser


async def _run(args: argparse.Namespace) -> int:
    catalog = ParquetDataCatalog(str(args.catalog_path))
    assets = tuple(
        asset.strip().upper()
        for asset in str(args.assets).split(",")
        if asset.strip()
    )
    tasks = [
        asyncio.create_task(
            _run_asset_loop(
                catalog=catalog,
                catalog_path=args.catalog_path,
                asset=asset,
                gamma_host=args.gamma_host,
                wss_url=args.wss_url,
                timeout=args.timeout,
                reconnect_delay=args.reconnect_delay,
                flush_rows=args.flush_rows,
                flush_seconds=args.flush_seconds,
                max_ticks=args.max_ticks,
            ),
        )
        for asset in assets
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
