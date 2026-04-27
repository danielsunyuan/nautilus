from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from examples.live.polymarket.polymarket_clob_capture import CaptureSessionMarker
from examples.live.polymarket.polymarket_clob_capture import InMemoryBook
from examples.live.polymarket.polymarket_clob_capture import MarketSelectionResolver
from examples.live.polymarket.polymarket_clob_capture import PolymarketCaptureConfig
from examples.live.polymarket.polymarket_clob_capture import PolymarketCapturePipeline
from examples.live.polymarket.polymarket_clob_capture import RawArchiveWriter
from examples.live.polymarket.polymarket_clob_capture import NormalizedDatasetWriter
from examples.live.polymarket.polymarket_clob_export import NautilusExportResult
from examples.live.polymarket.polymarket_clob_export import load_capture_records


ROOT = Path("/Users/duan/Documents/Finance/Entropy Labs/nautilus")
RESOURCE_ROOT = ROOT / "tests/integration_tests/adapters/polymarket/resources"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _mock_transport(routes: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = routes[str(request.url)]
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_resolver_expands_event_slugs_to_token_metadata() -> None:
    async def _run() -> list[Any]:
        event_payload = _load_json(RESOURCE_ROOT / "http_responses/event.json")
        market_payload = _load_json(RESOURCE_ROOT / "http_responses/market_slug.json")
        market_payloads = {}
        for index, market_ref in enumerate(event_payload["markets"]):
            payload = dict(market_payload)
            payload["slug"] = market_ref["slug"]
            payload["question"] = market_ref["question"]
            payload["conditionId"] = market_ref["conditionId"]
            payload["clobTokenIds"] = [
                f"{index + 1}1731245504777162452560887531170605263832832998797939951071198553974989813715",
                f"{index + 1}2650408556394197703520321372193619642402793204437834561777315032709183100311",
            ]
            market_payloads[market_ref["slug"]] = payload
        routes = {
            "https://gamma-api.polymarket.com/events/slug/highest-temperature-in-nyc-on-january-26": event_payload,
            "https://gamma-api.polymarket.com/markets/slug/highest-temperature-in-nyc-on-january-26-25forbelow": market_payloads["highest-temperature-in-nyc-on-january-26-25forbelow"],
            "https://gamma-api.polymarket.com/markets/slug/highest-temperature-in-nyc-on-january-26-26-27f": market_payloads["highest-temperature-in-nyc-on-january-26-26-27f"],
            "https://gamma-api.polymarket.com/markets/slug/highest-temperature-in-nyc-on-january-26-28-29f": market_payloads["highest-temperature-in-nyc-on-january-26-28-29f"],
        }
        async with httpx.AsyncClient(transport=_mock_transport(routes)) as client:
            resolver = MarketSelectionResolver(client)
            config = PolymarketCaptureConfig(
                output_root="/tmp/polymarket-capture",
                event_slugs=("highest-temperature-in-nyc-on-january-26",),
            )
            return await resolver.resolve_markets(config)

    records = asyncio.run(_run())

    assert len(records) == 6
    assert records[0].event_slug == "highest-temperature-in-nyc-on-january-26"
    assert records[0].token_id
    assert records[0].condition_id.startswith("0x")


def test_book_requires_reseed_after_gap_before_emitting_depth() -> None:
    book = InMemoryBook(token_id="token-1", market="market-1")

    first = book.apply_snapshot(
        bids=[("0.45", "100"), ("0.44", "50")],
        asks=[("0.55", "80"), ("0.56", "120")],
        event_ts_ms=1000,
        receive_ts_ns=1,
        heartbeat=False,
        gap=False,
    )
    book.mark_gap(reason="reconnect")
    second = book.apply_price_change(
        side="BUY",
        price="0.46",
        size="25",
        event_ts_ms=1001,
        receive_ts_ns=2,
    )
    third = book.apply_snapshot(
        bids=[("0.46", "25"), ("0.45", "100")],
        asks=[("0.55", "80"), ("0.56", "120")],
        event_ts_ms=1002,
        receive_ts_ns=3,
        heartbeat=False,
        gap=False,
    )

    assert first is not None
    assert second is None
    assert third is not None
    assert third.gap is False
    assert third.bids[0].price == "0.46"


def test_pipeline_writes_raw_events_and_normalized_outputs(tmp_path: Path) -> None:
    config = PolymarketCaptureConfig(output_root=str(tmp_path))
    raw_writer = RawArchiveWriter(config)
    normalized_writer = NormalizedDatasetWriter(config)
    pipeline = PolymarketCapturePipeline(raw_writer=raw_writer, normalized_writer=normalized_writer)

    pipeline.handle_message(
        message={
            "event_type": "book",
            "market": "market-1",
            "asset_id": "token-1",
            "timestamp": "1728799418260",
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "80"}],
        },
        session_id="session-1",
        receive_ts_ns=100,
    )
    pipeline.handle_message(
        message={
            "event_type": "last_trade_price",
            "market": "market-1",
            "asset_id": "token-1",
            "timestamp": "1728799418261",
            "price": "0.52",
            "size": "10",
            "side": "BUY",
        },
        session_id="session-1",
        receive_ts_ns=101,
    )

    raw_records = list(load_capture_records(tmp_path / "raw"))
    depth_records = list(load_capture_records(tmp_path / "normalized" / "depth"))
    trade_records = list(load_capture_records(tmp_path / "normalized" / "trades"))

    assert len(raw_records) == 2
    assert len(depth_records) == 1
    assert len(trade_records) == 1
    assert trade_records[0]["price"] == "0.52"


def test_export_loader_reads_all_normalized_record_kinds(tmp_path: Path) -> None:
    normalized_root = tmp_path / "normalized"
    marker = CaptureSessionMarker(
        session_id="s1",
        event="start",
        started_at_ns=1,
    )
    config = PolymarketCaptureConfig(output_root=str(tmp_path))
    writer = NormalizedDatasetWriter(config)
    writer.write_session_marker(marker)

    result = load_capture_records(normalized_root / "sessions")

    assert list(result)[0]["session_id"] == "s1"
    assert NautilusExportResult(depth_count=0, trade_count=0, quote_count=0, instrument_count=0)
