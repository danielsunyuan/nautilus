#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
import uuid

import httpx


log = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = "/workspace/outputs"
DEFAULT_CLOB_BASE_URL = "https://clob.polymarket.com"
RESOLVED_THRESHOLD = 0.99


@dataclass(frozen=True, slots=True)
class UnresolvedEntry:
    market_slug: str
    token_id: str
    token_side: str
    strategy_name: str
    city: str
    threshold: str
    band_type: str
    forecast_source: str
    model_yes_probability: float | None
    market_yes_price: float | None
    edge: float | None
    confidence: float | None
    entry_price: float
    shares: float
    stake: float
    source_file: str
    entry_time: str


@dataclass(frozen=True, slots=True)
class MarketResolution:
    token_id: str
    resolved: bool
    settlement_price: float | None
    settlement_source: str


class JsonlRunWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()


def _entry_id(row: dict[str, Any]) -> str:
    slug = str(row.get("market_slug") or "")
    entry_time = str(row.get("entry_time") or row.get("timestamp") or "")
    token_side = str(row.get("token_side") or row.get("selected_side") or "")
    strategy_name = str(row.get("strategy_name") or row.get("preset_name") or "")
    return f"{slug}|{entry_time}|{token_side}|{strategy_name}"


def _jsonl_files(jsonl_dir: Path) -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []
    for path in sorted(jsonl_dir.glob("weather_ensemble_*.jsonl")):
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    settlement_file = jsonl_dir / "weather_ensemble_settlement.jsonl"
    if settlement_file.exists() and settlement_file not in seen:
        files.append(settlement_file)
    return files


def _read_all_rows(jsonl_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    if not jsonl_dir.exists():
        return rows
    for path in _jsonl_files(jsonl_dir):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append((path.name, json.loads(line)))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return rows


def _collect_settled_entry_ids(rows: list[tuple[str, dict[str, Any]]]) -> set[str]:
    settled: set[str] = set()
    for _fname, row in rows:
        if row.get("event") != "settlement_update" or row.get("resolved") is not True:
            continue
        entry_id = row.get("entry_id")
        if entry_id:
            settled.add(str(entry_id))
        else:
            settled.add(_entry_id(row))
    return settled


def scan_unresolved_entries(jsonl_dir: Path) -> list[UnresolvedEntry]:
    rows = _read_all_rows(jsonl_dir)
    settled_entry_ids = _collect_settled_entry_ids(rows)
    latest: dict[str, UnresolvedEntry] = {}

    for fname, row in rows:
        if row.get("event") != "strategy_result":
            continue
        if row.get("resolved") is True:
            continue
        if row.get("accounting_status") not in (None, "", "open"):
            continue

        market_slug = str(row.get("market_slug") or "")
        token_id = str(row.get("token_id") or "")
        strategy_name = str(row.get("strategy_name") or row.get("preset_name") or "")
        token_side = str(row.get("token_side") or row.get("selected_side") or "").lower()
        entry_time = str(row.get("entry_time") or row.get("timestamp") or "")
        if not market_slug or not token_id or not strategy_name or not token_side or not entry_time:
            continue

        entry_id = row.get("entry_id")
        stable_id = str(entry_id) if entry_id else _entry_id(row)
        if stable_id in settled_entry_ids:
            continue

        try:
            entry_price = float(row.get("entry_price"))
            shares = float(row.get("shares"))
            stake = float(row.get("stake"))
        except (TypeError, ValueError):
            continue

        def _maybe_float(value: Any) -> float | None:
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        latest[stable_id] = UnresolvedEntry(
            market_slug=market_slug,
            token_id=token_id,
            token_side=token_side,
            strategy_name=strategy_name,
            city=str(row.get("city") or "unknown"),
            threshold=str(row.get("threshold") or "unknown"),
            band_type=str(row.get("band_type") or "unknown"),
            forecast_source=str(row.get("forecast_source") or "unknown"),
            model_yes_probability=_maybe_float(row.get("model_yes_probability")),
            market_yes_price=_maybe_float(row.get("market_yes_price")),
            edge=_maybe_float(row.get("edge")),
            confidence=_maybe_float(row.get("confidence")),
            entry_price=entry_price,
            shares=shares,
            stake=stake,
            source_file=fname,
            entry_time=entry_time,
        )

    return list(latest.values())


def compute_settlement(entry: UnresolvedEntry, resolution: MarketResolution) -> dict[str, Any] | None:
    if not resolution.resolved or resolution.settlement_price is None:
        return None

    pnl = round((resolution.settlement_price - entry.entry_price) * entry.shares, 6)
    return {
        "run_id": f"weather-ensemble-settlement-{uuid.uuid4()}",
        "event": "settlement_update",
        "entry_id": f"{entry.market_slug}|{entry.entry_time}|{entry.token_side}|{entry.strategy_name}",
        "market_slug": entry.market_slug,
        "token_id": entry.token_id,
        "token_side": entry.token_side,
        "strategy_name": entry.strategy_name,
        "city": entry.city,
        "threshold": entry.threshold,
        "band_type": entry.band_type,
        "forecast_source": entry.forecast_source,
        "model_yes_probability": entry.model_yes_probability,
        "market_yes_price": entry.market_yes_price,
        "edge": entry.edge,
        "confidence": entry.confidence,
        "entry_time": entry.entry_time,
        "entry_price": entry.entry_price,
        "settlement_price": resolution.settlement_price,
        "settlement_source": resolution.settlement_source,
        "shares": entry.shares,
        "stake": entry.stake,
        "pnl": pnl,
        "resolved": True,
        "resolved_outcome": "win" if pnl > 0 else "loss",
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }


def _resolution_from_price(*, token_id: str, price: float, source: str) -> MarketResolution:
    if price >= RESOLVED_THRESHOLD:
        return MarketResolution(
            token_id=token_id,
            resolved=True,
            settlement_price=1.0,
            settlement_source=source,
        )
    if price <= (1.0 - RESOLVED_THRESHOLD):
        return MarketResolution(
            token_id=token_id,
            resolved=True,
            settlement_price=0.0,
            settlement_source=source,
        )
    return MarketResolution(
        token_id=token_id,
        resolved=False,
        settlement_price=None,
        settlement_source=source,
    )


async def fetch_token_resolution(
    *,
    token_id: str,
    http_client: httpx.AsyncClient,
    clob_base_url: str,
    timeout: float = 15.0,
) -> MarketResolution | None:
    try:
        response = await http_client.get(
            f"{clob_base_url}/midpoint",
            params={"token_id": token_id},
            timeout=timeout,
        )
        if response.status_code == 200:
            mid = response.json().get("mid")
            if mid is not None:
                return _resolution_from_price(
                    token_id=token_id,
                    price=float(mid),
                    source="clob_midpoint",
                )
        elif response.status_code != 404:
            log.warning("Unexpected midpoint status=%s for token_id=...%s", response.status_code, token_id[-8:])
            return None
    except Exception:
        log.warning("Failed midpoint fetch for token_id=...%s", token_id[-8:])
        return None

    try:
        response = await http_client.get(
            f"{clob_base_url}/last-trade-price",
            params={"token_id": token_id},
            timeout=timeout,
        )
        if response.status_code == 200:
            price = response.json().get("price")
            if price is not None:
                return _resolution_from_price(
                    token_id=token_id,
                    price=float(price),
                    source="clob_last_trade_price",
                )
        log.warning("Unexpected last-trade-price status=%s for token_id=...%s", response.status_code, token_id[-8:])
        return None
    except Exception:
        log.warning("Failed last-trade-price fetch for token_id=...%s", token_id[-8:])
        return None


async def run_settlement_loop(
    *,
    jsonl_dir: Path,
    writer: JsonlRunWriter,
    fetch_resolution: Callable[..., Any],
    poll_interval_seconds: float = 300.0,
    max_iterations: int = 0,
    now_fn: Callable[[], datetime] | None = None,
) -> None:
    _now = now_fn or (lambda: datetime.now(tz=UTC))
    iteration = 0
    while True:
        iteration += 1
        entries = scan_unresolved_entries(jsonl_dir)
        for entry in entries:
            resolution = await fetch_resolution(token_id=entry.token_id)
            settlement = compute_settlement(entry, resolution) if resolution is not None else None
            if settlement is None:
                continue
            settlement["timestamp"] = _now().isoformat()
            writer.write(settlement)

        if max_iterations > 0 and iteration >= max_iterations:
            break
        await asyncio.sleep(max(float(poll_interval_seconds), 30.0))


async def run_weather_ensemble_settlement(
    *,
    output_dir: str,
    clob_base_url: str,
    poll_interval: float,
) -> None:
    jsonl_dir = Path(output_dir) / "polymarket" / "weather_ensemble"
    writer = JsonlRunWriter(jsonl_dir / "weather_ensemble_settlement.jsonl")
    async with httpx.AsyncClient(timeout=20.0, verify=False) as http:
        async def _fetch_resolution(*, token_id: str) -> MarketResolution | None:
            return await fetch_token_resolution(
                token_id=token_id,
                http_client=http,
                clob_base_url=clob_base_url,
            )

        await run_settlement_loop(
            jsonl_dir=jsonl_dir,
            writer=writer,
            fetch_resolution=_fetch_resolution,
            poll_interval_seconds=poll_interval,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--clob-base-url", default=DEFAULT_CLOB_BASE_URL)
    parser.add_argument("--poll-interval", type=float, default=300.0)
    args = parser.parse_args()
    asyncio.run(
        run_weather_ensemble_settlement(
            output_dir=args.output_dir,
            clob_base_url=args.clob_base_url,
            poll_interval=args.poll_interval,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
