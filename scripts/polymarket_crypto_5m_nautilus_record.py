from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from scripts.polymarket_btc_hourly_watch import DEFAULT_GAMMA_HOST, gamma_market_snapshot
from scripts.polymarket_crypto_5m_probs_ws import (
    DEFAULT_WSS_MARKET,
    SUPPORTED_ASSETS,
    current_crypto_5m_market_slug,
    market_end_from_slug,
)

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog


DEFAULT_RESOLUTIONS_PATH = "/data/nautilus_catalog/metadata/polymarket_5m_resolutions.jsonl"


def _gamma_retry_delay_seconds(*, attempt: int) -> float:
    base = float(os.environ.get("POLYMARKET_5M_GAMMA_RETRY_BASE_SECONDS", "3"))
    cap = float(os.environ.get("POLYMARKET_5M_GAMMA_RETRY_MAX_SECONDS", "120"))
    exp = min(attempt - 1, 8)
    return min(cap, base * (2**exp))


async def _gamma_market_snapshot_retry(*, gamma_host: str, market_slug: str, timeout: float) -> dict[str, Any]:
    """Fetch Gamma market metadata; retry on transient DNS / VPN / 5xx failures."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await asyncio.to_thread(
                gamma_market_snapshot,
                gamma_host=gamma_host,
                slug=market_slug,
                timeout=timeout,
            )
        except ValueError:
            raise
        except urllib.error.HTTPError as exc:
            if int(exc.code) == 404:
                raise
            if int(exc.code) < 500 and int(exc.code) not in (408, 425, 429):
                raise
            reason = f"http_{exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = type(exc).__name__
        delay = _gamma_retry_delay_seconds(attempt=attempt)
        print(
            json.dumps(
                {
                    "event": "polymarket_5m_gamma_retry",
                    "market_slug": market_slug,
                    "attempt": attempt,
                    "sleep_seconds": delay,
                    "reason": reason,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        await asyncio.sleep(delay)


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


def _pm_instrument_id(*, asset: str, token_id: str, label: str) -> InstrumentId:
    symbol = f"PM-{asset.upper()}-5M-{label.upper()}-{token_id}".replace(".", "_")
    return InstrumentId.from_str(f"{symbol}.POLYMARKET")


@dataclass(frozen=True)
class _TokenState:
    instrument_id: InstrumentId
    label: str
    best_bid: float | None = None
    best_ask: float | None = None
    best_bid_size: float | None = None
    best_ask_size: float | None = None


class _BufferedCatalogWriter:
    def __init__(
        self,
        *,
        catalog: ParquetDataCatalog,
        flush_rows: int,
        flush_seconds: float,
        now_fn: Any = None,
    ) -> None:
        self._catalog = catalog
        self._flush_rows = max(1, int(flush_rows))
        self._flush_seconds = max(0.0, float(flush_seconds))
        self._now_fn = now_fn or time.monotonic
        self._buffer: list[QuoteTick] = []
        self._buffer_started_at: float | None = None

    def add(self, ticks: list[QuoteTick]) -> int:
        if not ticks:
            return 0
        now = float(self._now_fn())
        if self._buffer_started_at is None:
            self._buffer_started_at = now
        self._buffer.extend(ticks)
        return self.flush_if_needed(now=now)

    def flush_if_needed(self, *, now: float | None = None) -> int:
        if not self._buffer:
            return 0
        current = float(self._now_fn()) if now is None else float(now)
        if len(self._buffer) >= self._flush_rows:
            return self.flush(force=True, now=current, reason="row_threshold")
        if self._flush_seconds > 0 and self._buffer_started_at is not None:
            if current - self._buffer_started_at >= self._flush_seconds:
                return self.flush(force=True, now=current, reason="time_threshold")
        return 0

    def flush(self, *, force: bool = False, now: float | None = None, reason: str = "manual") -> int:
        if not self._buffer:
            return 0
        current = float(self._now_fn()) if now is None else float(now)
        if not force:
            if len(self._buffer) < self._flush_rows:
                if self._flush_seconds <= 0 or self._buffer_started_at is None:
                    return 0
                if current - self._buffer_started_at < self._flush_seconds:
                    return 0
        batch = self._buffer
        self._buffer = []
        self._buffer_started_at = None
        self._catalog.write_data(batch)
        print(
            json.dumps(
                {
                    "event": "polymarket_5m_flush",
                    "rows": len(batch),
                    "reason": reason,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return len(batch)


def _resolution_out_path() -> Path:
    return Path(os.environ.get("POLYMARKET_5M_RESOLUTIONS_PATH", DEFAULT_RESOLUTIONS_PATH))


def _last_resolution_by_slug(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                slug = str(row.get("market_slug") or row.get("slug") or "").strip()
                if not slug:
                    continue
                latest[slug] = row
    except Exception:
        return latest
    return latest


def _append_jsonl_if_changed(*, path: Path, payload: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    slug = str(payload.get("market_slug") or "").strip()
    if slug:
        latest = _last_resolution_by_slug(path).get(slug)
        if isinstance(latest, dict):
            prev_outcome = latest.get("resolved_outcome")
            next_outcome = payload.get("resolved_outcome")
            prev_closed = latest.get("closed")
            next_closed = payload.get("closed")
            # If nothing materially changed, skip the write.
            if prev_outcome == next_outcome and prev_closed == next_closed:
                return False
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True))
        f.write("\n")
    return True


def _extract_resolution(payload: dict[str, Any]) -> dict[str, Any]:
    # Gamma payload fields vary; keep a stable normalized subset + preserve a few raw fields.
    def pick_str(*names: str) -> str | None:
        for n in names:
            v = payload.get(n)
            if v in (None, ""):
                continue
            return str(v)
        return None

    def pick_bool(*names: str) -> bool | None:
        for n in names:
            v = payload.get(n)
            if isinstance(v, bool):
                return v
        return None

    resolved_outcome = pick_str(
        "resolvedOutcome",
        "resolved_outcome",
        "winningOutcome",
        "winning_outcome",
        "outcome",
        "result",
    )
    def as_list(v: Any) -> list[Any] | None:
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, list) else None
            except Exception:
                return None
        return None

    outcomes = as_list(payload.get("outcomes"))
    outcome_prices = as_list(payload.get("outcomePrices") or payload.get("outcome_prices"))
    closed = payload.get("closed")

    # For these 5m crypto markets, Gamma often signals the winner via outcomePrices ["1","0"] when closed,
    # without a separate "resolvedOutcome" field.
    if resolved_outcome is None and closed is True and isinstance(outcomes, list) and isinstance(outcome_prices, list):
        try:
            if len(outcomes) == len(outcome_prices) and outcomes:
                prices = [float(p) for p in outcome_prices]
                best_idx = max(range(len(prices)), key=lambda i: prices[i])
                resolved_outcome = str(outcomes[best_idx])
        except Exception:
            pass

    clob_token_ids = as_list(payload.get("clobTokenIds") or payload.get("clob_token_ids"))
    return {
        "slug": pick_str("slug") or "",
        "question": payload.get("question"),
        "active": payload.get("active"),
        "closed": closed,
        "resolved": pick_bool("resolved") or payload.get("resolved"),
        "resolved_outcome": resolved_outcome,
        "end_date": payload.get("endDate") or payload.get("end_date"),
        "outcomes": outcomes,
        "outcome_prices": outcome_prices,
        "clob_token_ids": clob_token_ids,
        "updated_at": payload.get("updatedAt") or payload.get("updated_at"),
    }


def _gamma_market_raw(*, gamma_host: str, slug: str, timeout: float) -> dict[str, Any]:
    import urllib.request
    from urllib.parse import quote

    base = str(gamma_host).rstrip("/")
    url = f"{base}/markets/slug/{quote(str(slug))}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
        payload = json.load(resp)
    if not isinstance(payload, dict):
        raise ValueError("unexpected Gamma market payload")
    return payload


def write_market_resolution(
    *,
    asset: str,
    market_slug: str,
    gamma_host: str,
    timeout: float,
    up_token_id: str,
    down_token_id: str,
) -> bool:
    raw = _gamma_market_raw(gamma_host=gamma_host, slug=market_slug, timeout=timeout)
    resolution = _extract_resolution(raw)
    resolution.update(
        {
            "event": "polymarket_5m_resolution",
            "asset": asset,
            "market_slug": market_slug,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "up_token_id": up_token_id,
            "down_token_id": down_token_id,
        }
    )
    out = _resolution_out_path()
    wrote = _append_jsonl_if_changed(path=out, payload=resolution)
    if wrote:
        print(json.dumps({"event": "polymarket_5m_resolution_written", "path": str(out), "market_slug": market_slug}, sort_keys=True), flush=True)
    return wrote


async def _write_resolution_with_retry(
    *,
    asset: str,
    market_slug: str,
    gamma_host: str,
    timeout: float,
    up_token_id: str,
    down_token_id: str,
) -> None:
    max_wait_seconds = float(os.environ.get("POLYMARKET_5M_RESOLUTION_MAX_WAIT_SECONDS", "900"))
    poll_seconds = float(os.environ.get("POLYMARKET_5M_RESOLUTION_POLL_SECONDS", "15"))
    deadline = time.monotonic() + max(0.0, max_wait_seconds)

    last_written = False
    while True:
        try:
            wrote = write_market_resolution(
                asset=asset,
                market_slug=market_slug,
                gamma_host=gamma_host,
                timeout=timeout,
                up_token_id=up_token_id,
                down_token_id=down_token_id,
            )
            last_written = last_written or wrote
            raw = _gamma_market_raw(gamma_host=gamma_host, slug=market_slug, timeout=timeout)
            extracted = _extract_resolution(raw)
            if extracted.get("resolved_outcome") is not None or extracted.get("closed") is True:
                return
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "polymarket_5m_resolution_error",
                        "asset": asset,
                        "market_slug": market_slug,
                        "reason": str(exc),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        if time.monotonic() >= deadline:
            if last_written:
                return
            # Even if we couldn't fetch anything, don't spin forever.
            return

        await asyncio.sleep(max(1.0, poll_seconds))


def _parse_best_bid_ask(message: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    def as_float(v: Any) -> float | None:
        if v in (None, ""):
            return None
        try:
            return float(v)
        except Exception:
            return None

    return (
        as_float(message.get("best_bid")),
        as_float(message.get("best_ask")),
        as_float(message.get("best_bid_size")),
        as_float(message.get("best_ask_size")),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Record Polymarket 5m crypto top-of-book updates into a NautilusTrader ParquetDataCatalog."
    )
    p.add_argument("--catalog-path", default=os.environ.get("POLYMARKET_NAUTILUS_CATALOG", "/data/nautilus_catalog"))
    p.add_argument("--assets", default=os.environ.get("POLYMARKET_5M_ASSETS", "BTC,ETH,SOL,XRP,DOGE,BNB,HYPE"))
    p.add_argument("--gamma-host", default=os.environ.get("POLYMARKET_5M_GAMMA_HOST", DEFAULT_GAMMA_HOST))
    p.add_argument("--wss-url", default=os.environ.get("POLYMARKET_5M_WSS_URL", DEFAULT_WSS_MARKET))
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--reconnect-delay", type=float, default=float(os.environ.get("POLYMARKET_5M_RECONNECT_DELAY", "1.0")))
    p.add_argument("--flush-rows", type=int, default=int(os.environ.get("POLYMARKET_5M_FLUSH_ROWS", "20000")))
    p.add_argument(
        "--flush-seconds",
        type=float,
        default=float(os.environ.get("POLYMARKET_5M_FLUSH_SECONDS", "300")),
    )
    p.add_argument(
        "--catalog-max-rows-per-group",
        type=int,
        default=int(os.environ.get("POLYMARKET_NAUTILUS_MAX_ROWS_PER_GROUP", "20000")),
    )
    p.add_argument("--max-ticks", type=int, default=0, help="Stop after N QuoteTicks (0 = run forever).")
    return p


async def _record_one_market(
    *,
    catalog: ParquetDataCatalog,
    asset: str,
    market_slug: str,
    gamma_host: str,
    wss_url: str,
    timeout: float,
    max_ticks: int,
    total_ticks: int,
    reconnect_delay: float = 1.0,
    flush_rows: int = 5000,
    flush_seconds: float = 60.0,
) -> int:
    market = await _gamma_market_snapshot_retry(
        gamma_host=gamma_host, market_slug=market_slug, timeout=timeout
    )
    # gamma_market_snapshot returns dict with "up"/"down" token_id
    up_token = str(market["up"]["token_id"])
    down_token = str(market["down"]["token_id"])
    token_map: dict[str, _TokenState] = {
        up_token: _TokenState(instrument_id=_pm_instrument_id(asset=asset, token_id=up_token, label="YES"), label="YES"),
        down_token: _TokenState(
            instrument_id=_pm_instrument_id(asset=asset, token_id=down_token, label="NO"), label="NO"
        ),
    }

    subscribe = {"type": "market", "assets_ids": [up_token, down_token], "custom_feature_enabled": True}
    writer = _BufferedCatalogWriter(
        catalog=catalog,
        flush_rows=flush_rows,
        flush_seconds=flush_seconds,
    )
    poll_timeout = min(1.0, float(flush_seconds)) if float(flush_seconds) > 0 else 1.0

    while True:
        if datetime.now(timezone.utc) >= market_end_from_slug(market_slug):
            writer.flush(force=True, reason="market_end")
            await _write_resolution_with_retry(
                asset=asset,
                market_slug=market_slug,
                gamma_host=gamma_host,
                timeout=timeout,
                up_token_id=up_token,
                down_token_id=down_token,
            )
            return total_ticks

        try:
            async with connect(wss_url, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(subscribe))
                while True:
                    now = datetime.now(timezone.utc)
                    if now >= market_end_from_slug(market_slug):
                        writer.flush(force=True, reason="market_end")
                        await _write_resolution_with_retry(
                            asset=asset,
                            market_slug=market_slug,
                            gamma_host=gamma_host,
                            timeout=timeout,
                            up_token_id=up_token,
                            down_token_id=down_token,
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
                    tick_batch: list[QuoteTick] = []
                    ts = _now_ns()

                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        token_id = str(message.get("asset_id") or message.get("asset") or "").strip()
                        if token_id not in token_map:
                            continue

                        bid, ask, bid_sz, ask_sz = _parse_best_bid_ask(message)
                        if bid is None and ask is None:
                            continue

                        state = token_map[token_id]
                        # Keep last known sizes if sizes missing.
                        bid_sz_final = bid_sz if bid_sz is not None else state.best_bid_size
                        ask_sz_final = ask_sz if ask_sz is not None else state.best_ask_size

                        # Nautilus requires Price/Quantity objects.
                        # Prices are probabilities in [0, 1]; we store with 6dp.
                        bid_price = Price(bid if bid is not None else 0.0, 6)
                        ask_price = Price(ask if ask is not None else 0.0, 6)
                        bid_size_obj = Quantity(bid_sz_final if bid_sz_final is not None else 0.0, 6)
                        ask_size_obj = Quantity(ask_sz_final if ask_sz_final is not None else 0.0, 6)

                        tick_batch.append(
                            QuoteTick(
                                instrument_id=state.instrument_id,
                                bid_price=bid_price,
                                ask_price=ask_price,
                                bid_size=bid_size_obj,
                                ask_size=ask_size_obj,
                                ts_event=ts,
                                ts_init=ts,
                            )
                        )

                    if tick_batch:
                        writer.add(tick_batch)
                        total_ticks += len(tick_batch)
                        if max_ticks and total_ticks >= max_ticks:
                            writer.flush(force=True, reason="max_ticks")
                            return total_ticks
        except ConnectionClosed as exc:
            print(
                json.dumps(
                    {
                        "event": "polymarket_5m_reconnect",
                        "asset": asset,
                        "market_slug": market_slug,
                        "reason": str(exc),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            await asyncio.sleep(reconnect_delay)
            continue

        await asyncio.sleep(reconnect_delay)

    return total_ticks


async def _run(args: argparse.Namespace) -> int:
    assets = tuple(a.strip().upper() for a in str(args.assets).split(",") if a.strip())
    for a in assets:
        if a not in SUPPORTED_ASSETS:
            raise SystemExit(f"unsupported asset {a!r} (supported: {', '.join(SUPPORTED_ASSETS)})")

    catalog = ParquetDataCatalog(
        path=str(args.catalog_path),
        fs_protocol="file",
        max_rows_per_group=int(args.catalog_max_rows_per_group),
    )

    if int(args.max_ticks):
        total_ticks = 0
        while True:
            for asset in assets:
                market_slug = current_crypto_5m_market_slug(asset=asset, now=datetime.now(timezone.utc))
                try:
                    total_ticks = await _record_one_market(
                        catalog=catalog,
                        asset=asset,
                        market_slug=market_slug,
                        gamma_host=str(args.gamma_host),
                        wss_url=str(args.wss_url),
                        timeout=float(args.timeout),
                        max_ticks=int(args.max_ticks),
                        total_ticks=total_ticks,
                        reconnect_delay=float(args.reconnect_delay),
                        flush_rows=int(args.flush_rows),
                        flush_seconds=float(args.flush_seconds),
                    )
                except Exception as exc:
                    print(
                        json.dumps(
                            {
                                "event": "polymarket_5m_max_ticks_loop_error",
                                "asset": asset,
                                "market_slug": market_slug,
                                "reason": str(exc),
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    await asyncio.sleep(float(os.environ.get("POLYMARKET_5M_ASSET_LOOP_ERROR_SLEEP", "30")))
                    continue
                if args.max_ticks and total_ticks >= int(args.max_ticks):
                    return 0
            await asyncio.sleep(0.5)

    tasks = [
        asyncio.create_task(
            _run_asset_loop(
                catalog=catalog,
                asset=asset,
                gamma_host=str(args.gamma_host),
                wss_url=str(args.wss_url),
                timeout=float(args.timeout),
                reconnect_delay=float(args.reconnect_delay),
                flush_rows=int(args.flush_rows),
                flush_seconds=float(args.flush_seconds),
            )
        )
        for asset in assets
    ]
    await asyncio.gather(*tasks)
    return 0


async def _run_asset_loop(
    *,
    catalog: ParquetDataCatalog,
    asset: str,
    gamma_host: str,
    wss_url: str,
    timeout: float,
    reconnect_delay: float,
    flush_rows: int,
    flush_seconds: float,
) -> int:
    total_ticks = 0
    while True:
        market_slug = current_crypto_5m_market_slug(asset=asset, now=datetime.now(timezone.utc))
        try:
            total_ticks = await _record_one_market(
                catalog=catalog,
                asset=asset,
                market_slug=market_slug,
                gamma_host=gamma_host,
                wss_url=wss_url,
                timeout=timeout,
                max_ticks=0,
                total_ticks=total_ticks,
                reconnect_delay=reconnect_delay,
                flush_rows=flush_rows,
                flush_seconds=flush_seconds,
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "polymarket_5m_asset_loop_error",
                        "asset": asset,
                        "market_slug": market_slug,
                        "reason": str(exc),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            await asyncio.sleep(float(os.environ.get("POLYMARKET_5M_ASSET_LOOP_ERROR_SLEEP", "30")))
            continue
        await asyncio.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
