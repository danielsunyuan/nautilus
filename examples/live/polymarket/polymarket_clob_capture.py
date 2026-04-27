from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from decimal import Decimal
import gzip
import json
from pathlib import Path
from typing import Any
from typing import TextIO
import uuid

import aiohttp
import httpx


GAMMA_API_URL = "https://gamma-api.polymarket.com"
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass(frozen=True, kw_only=True)
class PolymarketCaptureConfig:
    output_root: str
    event_slugs: tuple[str, ...] = ()
    market_slugs: tuple[str, ...] = ()
    token_ids: tuple[str, ...] = ()
    subscription_batch_size: int = 50
    compress_raw: bool = False
    compress_normalized: bool = False
    heartbeat_interval_secs: float = 30.0
    reconnect_delay_secs: float = 5.0
    max_reconnect_delay_secs: float = 60.0
    gamma_api_url: str = GAMMA_API_URL
    market_ws_url: str = MARKET_WS_URL


@dataclass(frozen=True)
class BookLevel:
    price: str
    size: str


@dataclass(frozen=True)
class PolymarketMarketMetadata:
    token_id: str
    condition_id: str
    market_slug: str
    outcome: str
    question: str
    event_slug: str | None = None
    event_title: str | None = None
    minimum_tick_size: str | None = None
    minimum_order_size: str | None = None
    end_date_iso: str | None = None


@dataclass(frozen=True)
class RawEventEnvelope:
    session_id: str
    receive_ts_ns: int
    source_channel: str
    token_id: str | None
    event_type: str
    market: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: str | None = None


@dataclass(frozen=True)
class DerivedDepthSnapshot:
    token_id: str
    market: str
    event_ts_ms: int
    receive_ts_ns: int
    gap: bool
    heartbeat: bool
    best_bid_price: str | None
    best_bid_size: str | None
    best_ask_price: str | None
    best_ask_size: str | None
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]


@dataclass(frozen=True)
class DerivedTradeEvent:
    token_id: str
    market: str
    event_ts_ms: int
    receive_ts_ns: int
    price: str
    size: str
    side: str | None = None


@dataclass(frozen=True)
class CaptureSessionMarker:
    session_id: str
    event: str
    started_at_ns: int
    token_id: str | None = None
    reason: str | None = None


def _utc_date_from_ns(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=UTC).strftime("%Y-%m-%d")


def _open_jsonl(path: Path, compress: bool) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compress:
        return gzip.open(path.with_suffix(path.suffix + ".gz"), "at", encoding="utf-8")
    return path.open("a", encoding="utf-8")


def _write_jsonl(handle: TextIO, payload: object | dict[str, Any]) -> None:
    if isinstance(payload, dict):
        data = payload
    else:
        data = asdict(payload)
    handle.write(json.dumps(data, sort_keys=True) + "\n")
    handle.flush()


class RawArchiveWriter:
    def __init__(self, config: PolymarketCaptureConfig) -> None:
        self.root = Path(config.output_root) / "raw"
        self.compress = config.compress_raw

    def write(self, record: RawEventEnvelope) -> Path:
        date_part = _utc_date_from_ns(record.receive_ts_ns)
        path = self.root / date_part / f"{record.session_id}.jsonl"
        with _open_jsonl(path, self.compress) as handle:
            _write_jsonl(handle, record)
        return path


class NormalizedDatasetWriter:
    def __init__(self, config: PolymarketCaptureConfig) -> None:
        self.root = Path(config.output_root) / "normalized"
        self.compress = config.compress_normalized

    def _write_kind(self, kind: str, ts_ns: int, record: object) -> Path:
        date_part = _utc_date_from_ns(ts_ns)
        path = self.root / kind / date_part / f"{kind}.jsonl"
        with _open_jsonl(path, self.compress) as handle:
            _write_jsonl(handle, record)
        return path

    def write_metadata(self, record: PolymarketMarketMetadata, ts_ns: int | None = None) -> Path:
        return self._write_kind("metadata", ts_ns or int(datetime.now(tz=UTC).timestamp() * 1e9), record)

    def write_depth(self, record: DerivedDepthSnapshot) -> Path:
        return self._write_kind("depth", record.receive_ts_ns, record)

    def write_trade(self, record: DerivedTradeEvent) -> Path:
        return self._write_kind("trades", record.receive_ts_ns, record)

    def write_session_marker(self, record: CaptureSessionMarker) -> Path:
        return self._write_kind("sessions", record.started_at_ns, record)


def _as_tuple_levels(levels: Iterable[tuple[Decimal, Decimal]]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=str(price), size=str(size)) for price, size in levels)


class InMemoryBook:
    def __init__(self, token_id: str, market: str, max_depth: int = 10) -> None:
        self.token_id = token_id
        self.market = market
        self.max_depth = max_depth
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._seeded = False
        self._gap = False
        self._last_fingerprint: tuple[tuple[str, str], ...] | None = None

    def mark_gap(self, reason: str | None = None) -> CaptureSessionMarker:
        self._seeded = False
        self._gap = True
        return CaptureSessionMarker(
            session_id="",
            event="gap",
            started_at_ns=int(datetime.now(tz=UTC).timestamp() * 1e9),
            token_id=self.token_id,
            reason=reason,
        )

    def apply_snapshot(
        self,
        *,
        bids: Sequence[tuple[str, str]],
        asks: Sequence[tuple[str, str]],
        event_ts_ms: int,
        receive_ts_ns: int,
        heartbeat: bool,
        gap: bool,
    ) -> DerivedDepthSnapshot | None:
        self._bids = {Decimal(price): Decimal(size) for price, size in bids if Decimal(size) > 0}
        self._asks = {Decimal(price): Decimal(size) for price, size in asks if Decimal(size) > 0}
        self._seeded = True
        self._gap = gap
        return self._emit_snapshot(event_ts_ms=event_ts_ms, receive_ts_ns=receive_ts_ns, heartbeat=heartbeat)

    def apply_price_change(
        self,
        *,
        side: str,
        price: str,
        size: str,
        event_ts_ms: int,
        receive_ts_ns: int,
    ) -> DerivedDepthSnapshot | None:
        if not self._seeded:
            return None

        book = self._bids if side.upper() == "BUY" else self._asks
        price_decimal = Decimal(price)
        size_decimal = Decimal(size)
        if size_decimal <= 0:
            book.pop(price_decimal, None)
        else:
            book[price_decimal] = size_decimal

        return self._emit_snapshot(event_ts_ms=event_ts_ms, receive_ts_ns=receive_ts_ns, heartbeat=False)

    def emit_heartbeat(self, *, event_ts_ms: int, receive_ts_ns: int) -> DerivedDepthSnapshot | None:
        if not self._seeded:
            return None
        return self._emit_snapshot(event_ts_ms=event_ts_ms, receive_ts_ns=receive_ts_ns, heartbeat=True)

    def _sorted_bids(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self._bids.items(), key=lambda item: item[0], reverse=True)[: self.max_depth]

    def _sorted_asks(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self._asks.items(), key=lambda item: item[0])[: self.max_depth]

    def _emit_snapshot(
        self,
        *,
        event_ts_ms: int,
        receive_ts_ns: int,
        heartbeat: bool,
    ) -> DerivedDepthSnapshot | None:
        bids = self._sorted_bids()
        asks = self._sorted_asks()
        fingerprint = tuple(
            [("B", str(price), str(size)) for price, size in bids]
            + [("A", str(price), str(size)) for price, size in asks]
        )
        if not heartbeat and fingerprint == self._last_fingerprint:
            return None
        self._last_fingerprint = fingerprint

        best_bid = bids[0] if bids else None
        best_ask = asks[0] if asks else None
        snapshot = DerivedDepthSnapshot(
            token_id=self.token_id,
            market=self.market,
            event_ts_ms=event_ts_ms,
            receive_ts_ns=receive_ts_ns,
            gap=self._gap,
            heartbeat=heartbeat,
            best_bid_price=str(best_bid[0]) if best_bid else None,
            best_bid_size=str(best_bid[1]) if best_bid else None,
            best_ask_price=str(best_ask[0]) if best_ask else None,
            best_ask_size=str(best_ask[1]) if best_ask else None,
            bids=_as_tuple_levels(bids),
            asks=_as_tuple_levels(asks),
        )
        self._gap = False
        return snapshot


@dataclass
class PolymarketCapturePipeline:
    raw_writer: RawArchiveWriter
    normalized_writer: NormalizedDatasetWriter
    books: dict[str, InMemoryBook] | None = None

    def __post_init__(self) -> None:
        if self.books is None:
            self.books = {}

    def register_metadata(self, markets: Sequence[PolymarketMarketMetadata], *, receive_ts_ns: int) -> None:
        for market in markets:
            self.normalized_writer.write_metadata(market, ts_ns=receive_ts_ns)

    def record_session(self, marker: CaptureSessionMarker) -> None:
        self.normalized_writer.write_session_marker(marker)

    def mark_gap(self, *, token_id: str, reason: str, session_id: str, receive_ts_ns: int) -> None:
        book = self.books.get(token_id)
        if book is not None:
            book.mark_gap(reason=reason)
        self.record_session(
            CaptureSessionMarker(
                session_id=session_id,
                event="gap",
                started_at_ns=receive_ts_ns,
                token_id=token_id,
                reason=reason,
            ),
        )

    def handle_message(self, *, message: dict[str, Any], session_id: str, receive_ts_ns: int) -> None:
        token_id = message.get("asset_id")
        event_type = str(message.get("event_type", "unknown"))
        market = message.get("market")
        self.raw_writer.write(
            RawEventEnvelope(
                session_id=session_id,
                receive_ts_ns=receive_ts_ns,
                source_channel="market",
                token_id=token_id,
                event_type=event_type,
                market=market,
                sequence=str(message.get("hash")) if message.get("hash") is not None else None,
                payload=message,
            ),
        )

        if not token_id or not market:
            return

        book = self.books.setdefault(token_id, InMemoryBook(token_id=token_id, market=market))
        timestamp = int(message.get("timestamp", "0"))

        if event_type == "book":
            snapshot = book.apply_snapshot(
                bids=[(level["price"], level["size"]) for level in message.get("bids", [])],
                asks=[(level["price"], level["size"]) for level in message.get("asks", [])],
                event_ts_ms=timestamp,
                receive_ts_ns=receive_ts_ns,
                heartbeat=False,
                gap=book._gap,
            )
            if snapshot is not None:
                self.normalized_writer.write_depth(snapshot)
        elif event_type == "price_change":
            if "price_changes" in message:
                for change in message["price_changes"]:
                    snapshot = book.apply_price_change(
                        side=change["side"],
                        price=change["price"],
                        size=change["size"],
                        event_ts_ms=timestamp,
                        receive_ts_ns=receive_ts_ns,
                    )
                    if snapshot is not None:
                        self.normalized_writer.write_depth(snapshot)
            else:
                snapshot = book.apply_price_change(
                    side=str(message["side"]),
                    price=str(message["price"]),
                    size=str(message["size"]),
                    event_ts_ms=timestamp,
                    receive_ts_ns=receive_ts_ns,
                )
                if snapshot is not None:
                    self.normalized_writer.write_depth(snapshot)
        elif event_type == "last_trade_price":
            self.normalized_writer.write_trade(
                DerivedTradeEvent(
                    token_id=token_id,
                    market=market,
                    event_ts_ms=timestamp,
                    receive_ts_ns=receive_ts_ns,
                    price=str(message["price"]),
                    size=str(message["size"]),
                    side=message.get("side"),
                ),
            )

    def emit_heartbeats(self, *, receive_ts_ns: int) -> None:
        event_ts_ms = receive_ts_ns // 1_000_000
        for book in self.books.values():
            snapshot = book.emit_heartbeat(event_ts_ms=event_ts_ms, receive_ts_ns=receive_ts_ns)
            if snapshot is not None:
                self.normalized_writer.write_depth(snapshot)


class MarketSelectionResolver:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self.http_client = http_client

    async def resolve_markets(self, config: PolymarketCaptureConfig) -> list[PolymarketMarketMetadata]:
        seen: dict[str, PolymarketMarketMetadata] = {}

        for slug in config.market_slugs:
            for market in await self._resolve_market_slug(
                gamma_api_url=config.gamma_api_url,
                slug=slug,
                event_slug=None,
                event_title=None,
            ):
                seen[market.token_id] = market

        for slug in config.event_slugs:
            event = await self._fetch_json(f"{config.gamma_api_url}/events/slug/{slug}")
            for market_ref in event.get("markets", []):
                for market in await self._resolve_market_slug(
                    gamma_api_url=config.gamma_api_url,
                    slug=market_ref["slug"],
                    event_slug=event.get("slug"),
                    event_title=event.get("title"),
                ):
                    seen[market.token_id] = market

        if config.token_ids:
            unresolved = set(config.token_ids) - set(seen)
            if unresolved:
                await self._resolve_token_ids(config=config, unresolved=unresolved, seen=seen)

        return list(seen.values())

    async def _resolve_market_slug(
        self,
        *,
        gamma_api_url: str,
        slug: str,
        event_slug: str | None,
        event_title: str | None,
    ) -> list[PolymarketMarketMetadata]:
        market = await self._fetch_json(f"{gamma_api_url}/markets/slug/{slug}")
        token_ids = market.get("clobTokenIds", [])
        outcomes = self._extract_outcomes(market)
        return [
            PolymarketMarketMetadata(
                token_id=str(token_id),
                condition_id=str(market.get("conditionId") or market.get("condition_id")),
                market_slug=str(market.get("slug") or slug),
                outcome=outcomes[idx] if idx < len(outcomes) else f"outcome_{idx}",
                question=str(market.get("question", "")),
                event_slug=event_slug,
                event_title=event_title,
                minimum_tick_size=str(market["minimum_tick_size"]) if market.get("minimum_tick_size") is not None else None,
                minimum_order_size=str(market["minimum_order_size"]) if market.get("minimum_order_size") is not None else None,
                end_date_iso=market.get("end_date_iso"),
            )
            for idx, token_id in enumerate(token_ids)
        ]

    async def _resolve_token_ids(
        self,
        *,
        config: PolymarketCaptureConfig,
        unresolved: set[str],
        seen: dict[str, PolymarketMarketMetadata],
    ) -> None:
        offset = 0
        limit = 100
        while unresolved:
            markets = await self._fetch_json(
                f"{config.gamma_api_url}/markets",
                params={"limit": str(limit), "offset": str(offset), "active": "true", "closed": "false", "archived": "false"},
            )
            if not markets:
                break
            for market in markets:
                market_token_ids = [str(token_id) for token_id in market.get("clobTokenIds", [])]
                overlap = unresolved.intersection(market_token_ids)
                if not overlap:
                    continue
                outcomes = self._extract_outcomes(market)
                for idx, token_id in enumerate(market_token_ids):
                    if token_id not in overlap:
                        continue
                    seen[token_id] = PolymarketMarketMetadata(
                        token_id=token_id,
                        condition_id=str(market.get("conditionId") or market.get("condition_id")),
                        market_slug=str(market.get("slug") or market.get("market_slug")),
                        outcome=outcomes[idx] if idx < len(outcomes) else f"outcome_{idx}",
                        question=str(market.get("question", "")),
                    )
                    unresolved.discard(token_id)
            offset += limit

    async def _fetch_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        response = await self.http_client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _extract_outcomes(market: dict[str, Any]) -> list[str]:
        if isinstance(market.get("outcomes"), list):
            return [str(item) for item in market["outcomes"]]
        tokens = market.get("tokens")
        if isinstance(tokens, list):
            extracted = []
            for token in tokens:
                outcome = token.get("outcome")
                if outcome is not None:
                    extracted.append(str(outcome))
            if extracted:
                return extracted
        return []


class PolymarketCaptureService:
    def __init__(
        self,
        *,
        config: PolymarketCaptureConfig,
        pipeline: PolymarketCapturePipeline,
        resolver: MarketSelectionResolver,
    ) -> None:
        self.config = config
        self.pipeline = pipeline
        self.resolver = resolver

    async def run(self) -> None:
        markets = await self.resolver.resolve_markets(self.config)
        now_ns = int(datetime.now(tz=UTC).timestamp() * 1_000_000_000)
        self.pipeline.register_metadata(markets, receive_ts_ns=now_ns)
        token_ids = [market.token_id for market in markets]
        while True:
            session_id = uuid.uuid4().hex
            started_at_ns = int(datetime.now(tz=UTC).timestamp() * 1_000_000_000)
            self.pipeline.record_session(
                CaptureSessionMarker(session_id=session_id, event="start", started_at_ns=started_at_ns),
            )
            try:
                await self._run_session(session_id=session_id, token_ids=token_ids)
            except Exception as exc:
                failure_ns = int(datetime.now(tz=UTC).timestamp() * 1_000_000_000)
                for token_id in token_ids:
                    self.pipeline.mark_gap(
                        token_id=token_id,
                        reason=f"reconnect:{exc}",
                        session_id=session_id,
                        receive_ts_ns=failure_ns,
                    )
                await asyncio.sleep(self.config.reconnect_delay_secs)

    async def _run_session(self, *, session_id: str, token_ids: Sequence[str]) -> None:
        heartbeat_interval = self.config.heartbeat_interval_secs
        next_heartbeat = asyncio.get_running_loop().time() + heartbeat_interval

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.config.market_ws_url, heartbeat=heartbeat_interval) as ws:
                for batch_start in range(0, len(token_ids), self.config.subscription_batch_size):
                    batch = list(token_ids[batch_start : batch_start + self.config.subscription_batch_size])
                    await ws.send_json({"assets_ids": batch, "type": "market"})

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        receive_ts_ns = int(datetime.now(tz=UTC).timestamp() * 1_000_000_000)
                        payload = json.loads(msg.data)
                        if isinstance(payload, list):
                            for item in payload:
                                if isinstance(item, dict):
                                    self.pipeline.handle_message(
                                        message=item,
                                        session_id=session_id,
                                        receive_ts_ns=receive_ts_ns,
                                    )
                        elif isinstance(payload, dict):
                            self.pipeline.handle_message(
                                message=payload,
                                session_id=session_id,
                                receive_ts_ns=receive_ts_ns,
                            )
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise RuntimeError("Polymarket WebSocket error")

                    now = asyncio.get_running_loop().time()
                    if now >= next_heartbeat:
                        self.pipeline.emit_heartbeats(
                            receive_ts_ns=int(datetime.now(tz=UTC).timestamp() * 1_000_000_000),
                        )
                        next_heartbeat = now + heartbeat_interval


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--event-slug", action="append", default=[])
    parser.add_argument("--market-slug", action="append", default=[])
    parser.add_argument("--token-id", action="append", default=[])
    parser.add_argument("--subscription-batch-size", type=int, default=50)
    parser.add_argument("--heartbeat-interval-secs", type=float, default=30.0)
    parser.add_argument("--compress-raw", action="store_true")
    parser.add_argument("--compress-normalized", action="store_true")
    return parser


async def _run_from_args(args: argparse.Namespace) -> None:
    config = PolymarketCaptureConfig(
        output_root=args.output_root,
        event_slugs=tuple(args.event_slug),
        market_slugs=tuple(args.market_slug),
        token_ids=tuple(args.token_id),
        subscription_batch_size=args.subscription_batch_size,
        heartbeat_interval_secs=args.heartbeat_interval_secs,
        compress_raw=args.compress_raw,
        compress_normalized=args.compress_normalized,
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        service = PolymarketCaptureService(
            config=config,
            pipeline=PolymarketCapturePipeline(
                raw_writer=RawArchiveWriter(config),
                normalized_writer=NormalizedDatasetWriter(config),
            ),
            resolver=MarketSelectionResolver(client),
        )
        await service.run()


if __name__ == "__main__":
    asyncio.run(_run_from_args(_build_parser().parse_args()))
