from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def _without_repo_root_on_sys_path():
    original = list(sys.path)
    sys.path = [
        entry
        for entry in original
        if Path(entry or ".").resolve() != ROOT
    ]
    try:
        yield
    finally:
        sys.path = original


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        with _without_repo_root_on_sys_path():
            spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


crypto_5m = _load_module(
    "nautilus_trader.adapters.polymarket.common.crypto_5m",
    ROOT / "nautilus_trader" / "adapters" / "polymarket" / "common" / "crypto_5m.py",
)
recorder = _load_module(
    "examples.live.polymarket.polymarket_crypto_5m_recorder",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_crypto_5m_recorder.py",
)


def _session():
    return crypto_5m.parse_crypto_5m_market(
        {
            "slug": "btc-updown-5m-1776064800",
            "conditionId": "condition-123",
            "question": "Will BTC be up or down in the next 5 minutes?",
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["up-token", "down-token"],
            "active": True,
            "closed": False,
            "archived": False,
            "acceptingOrders": True,
            "endDateIso": "2099-04-12T12:05:00Z",
        },
        asset="BTC",
    )


class _FakeConnectionClosed(Exception):
    pass


class _FakeCatalog:
    def __init__(self) -> None:
        self.batches: list[list[object]] = []

    def write_data(self, batch: list[object]) -> None:
        self.batches.append(batch)


class _FakeWebSocket:
    def __init__(self, *, messages: list[object] | None = None, error: Exception | None = None) -> None:
        self._messages = list(messages or [])
        self._error = error
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        if self._error is not None:
            error = self._error
            self._error = None
            raise error
        if not self._messages:
            raise StopAsyncIteration
        item = self._messages.pop(0)
        if isinstance(item, Exception):
            raise item
        return str(item)


class _FakeConnectFactory:
    def __init__(self, sockets: list[_FakeWebSocket]) -> None:
        self._sockets = list(sockets)

    def __call__(self, *args: object, **kwargs: object):
        socket = self._sockets.pop(0)

        class _ConnectionManager:
            async def __aenter__(self_inner) -> _FakeWebSocket:
                return socket

            async def __aexit__(self_inner, exc_type, exc, tb) -> bool:
                return False

        return _ConnectionManager()


class QuoteTickConversionTests(unittest.TestCase):
    def test_quote_ticks_from_messages_preserve_sizes(self) -> None:
        session = _session()
        token_state = recorder.build_token_state(session)
        tick_batch = recorder.quote_ticks_from_messages(
            token_state=token_state,
            messages=[
                {
                    "asset_id": "up-token",
                    "best_bid": "0.41",
                    "best_ask": "0.59",
                    "best_bid_size": "12",
                    "best_ask_size": "15",
                }
            ],
            ts_ns=123,
        )

        self.assertEqual(len(tick_batch), 1)
        tick = tick_batch[0]
        self.assertEqual(str(tick.instrument_id), str(session.instrument_ids["up"]))
        self.assertEqual(str(tick.bid_price), "0.41")
        self.assertEqual(str(tick.ask_price), "0.59")
        self.assertEqual(str(tick.bid_size), "12")
        self.assertEqual(str(tick.ask_size), "15")

    def test_quote_ticks_preserve_precise_sizes_without_float_rounding(self) -> None:
        session = _session()
        token_state = recorder.build_token_state(session)

        tick_batch = recorder.quote_ticks_from_messages(
            token_state=token_state,
            messages=[
                {
                    "asset_id": "up-token",
                    "best_bid": "0.41",
                    "best_ask": "0.59",
                    "best_bid_size": "12.0000001",
                    "best_ask_size": "15.0000002",
                }
            ],
            ts_ns=123,
        )

        self.assertEqual(str(tick_batch[0].bid_size), "12.0000001")
        self.assertEqual(str(tick_batch[0].ask_size), "15.0000002")

    def test_quote_ticks_skip_incomplete_quotes_without_fabricating_zero_prices(self) -> None:
        session = _session()
        token_state = recorder.build_token_state(session)

        tick_batch = recorder.quote_ticks_from_messages(
            token_state=token_state,
            messages=[
                {
                    "asset_id": "up-token",
                    "best_bid": "0.41",
                    "best_bid_size": "12",
                }
            ],
            ts_ns=123,
        )

        self.assertEqual(tick_batch, [])


class BufferedCatalogWriterTests(unittest.TestCase):
    def test_flushes_when_time_threshold_is_hit(self) -> None:
        catalog = _FakeCatalog()
        times = iter([100.0, 161.0])
        writer = recorder._BufferedCatalogWriter(
            catalog=catalog,
            flush_rows=10,
            flush_seconds=60.0,
            now_fn=lambda: next(times),
        )

        writer.add([object()])
        self.assertEqual(len(catalog.batches), 0)

        writer.add([object()])

        self.assertEqual(len(catalog.batches), 1)
        self.assertEqual(len(catalog.batches[0]), 2)

    def test_failed_flush_keeps_buffered_rows(self) -> None:
        class _FlakyCatalog(_FakeCatalog):
            def __init__(self) -> None:
                super().__init__()
                self.fail = True

            def write_data(self, batch: list[object]) -> None:
                if self.fail:
                    self.fail = False
                    raise RuntimeError("disk full")
                super().write_data(batch)

        catalog = _FlakyCatalog()
        writer = recorder._BufferedCatalogWriter(
            catalog=catalog,
            flush_rows=1,
            flush_seconds=60.0,
        )

        with self.assertRaisesRegex(RuntimeError, "disk full"):
            writer.add([object()])

        self.assertEqual(len(writer._buffer), 1)
        writer.flush(force=True)
        self.assertEqual(len(catalog.batches), 1)
        self.assertEqual(len(catalog.batches[0]), 1)


class RecordOneMarketReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_one_market_retries_after_connection_closed(self) -> None:
        catalog = _FakeCatalog()
        session = _session()
        disconnect = _FakeConnectionClosed("internal error")
        recovered_message = json.dumps(
            {
                "asset_id": "up-token",
                "best_bid": "0.41",
                "best_ask": "0.59",
                "best_bid_size": "12",
                "best_ask_size": "15",
            }
        )
        connect_factory = _FakeConnectFactory(
            [
                _FakeWebSocket(error=disconnect),
                _FakeWebSocket(messages=[recovered_message]),
            ],
        )

        with (
            patch.object(recorder, "connect", new=connect_factory),
            patch.object(recorder.asyncio, "sleep", new=AsyncMock()),
            patch.object(recorder, "_write_resolution_snapshot", new=AsyncMock()),
        ):
            total_ticks = await recorder._record_one_market(
                catalog=catalog,
                session=session,
                gamma_host="https://gamma.test",
                wss_url="wss://ws.test",
                timeout=10.0,
                max_ticks=1,
                total_ticks=0,
            )

        self.assertEqual(total_ticks, 1)
        self.assertEqual(len(catalog.batches), 1)
        self.assertEqual(len(catalog.batches[0]), 1)

    async def test_record_one_market_raises_non_recoverable_errors(self) -> None:
        catalog = _FakeCatalog()
        session = _session()
        connect_factory = _FakeConnectFactory([_FakeWebSocket(messages=["{bad-json"])])

        with (
            patch.object(recorder, "connect", new=connect_factory),
            patch.object(
                recorder.asyncio,
                "sleep",
                new=AsyncMock(side_effect=AssertionError("unexpected reconnect")),
            ),
        ):
            with self.assertRaises(json.JSONDecodeError):
                await recorder._record_one_market(
                    catalog=catalog,
                    session=session,
                    gamma_host="https://gamma.test",
                    wss_url="wss://ws.test",
                    timeout=10.0,
                    max_ticks=1,
                    total_ticks=0,
                )

    async def test_record_one_market_retries_resolution_snapshot_after_close(self) -> None:
        catalog = _FakeCatalog()
        session = replace(_session(), end_time=datetime.now(tz=UTC) - timedelta(seconds=1))
        write_resolution_snapshot = AsyncMock(
            side_effect=[
                RuntimeError("gamma unavailable"),
                RuntimeError("gamma unavailable"),
                None,
            ],
        )
        sleep_mock = AsyncMock()

        with (
            patch.object(
                recorder,
                "_write_resolution_snapshot",
                new=write_resolution_snapshot,
            ),
            patch.object(recorder.asyncio, "sleep", new=sleep_mock),
        ):
            total_ticks = await recorder._record_one_market(
                catalog=catalog,
                session=session,
                gamma_host="https://gamma.test",
                wss_url="wss://ws.test",
                timeout=10.0,
                max_ticks=0,
                total_ticks=0,
            )

        self.assertEqual(total_ticks, 0)
        self.assertEqual(write_resolution_snapshot.await_count, 3)
        self.assertEqual(sleep_mock.await_count, 2)


class AssetLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_asset_loop_retries_after_session_resolution_error(self) -> None:
        catalog = _FakeCatalog()
        session = _session()

        with (
            patch.object(recorder, "HttpClient", return_value=object()),
            patch.object(
                recorder,
                "resolve_crypto_5m_session",
                new=AsyncMock(side_effect=[RuntimeError("gamma down"), session]),
            ) as resolve_session,
            patch.object(
                recorder,
                "_record_one_market",
                new=AsyncMock(return_value=1),
            ) as record_market,
            patch.object(recorder.asyncio, "sleep", new=AsyncMock()),
        ):
            total_ticks = await recorder._run_asset_loop(
                catalog=catalog,
                catalog_path="/tmp/catalog",
                asset="BTC",
                gamma_host="https://gamma.test",
                wss_url="wss://ws.test",
                timeout=10.0,
                reconnect_delay=0.0,
                flush_rows=10,
                flush_seconds=60.0,
                max_ticks=1,
            )

        self.assertEqual(total_ticks, 1)
        self.assertEqual(resolve_session.await_count, 2)
        self.assertEqual(record_market.await_count, 1)

    async def test_run_asset_loop_stops_after_non_recoverable_error(self) -> None:
        catalog = _FakeCatalog()
        session = _session()

        with (
            patch.object(recorder, "HttpClient", return_value=object()),
            patch.object(
                recorder,
                "resolve_crypto_5m_session",
                new=AsyncMock(return_value=session),
            ) as resolve_session,
            patch.object(
                recorder,
                "_record_one_market",
                new=AsyncMock(side_effect=ValueError("bad quote payload")),
            ) as record_market,
            patch.object(
                recorder.asyncio,
                "sleep",
                new=AsyncMock(side_effect=AssertionError("unexpected retry")),
            ),
        ):
            total_ticks = await recorder._run_asset_loop(
                catalog=catalog,
                catalog_path="/tmp/catalog",
                asset="BTC",
                gamma_host="https://gamma.test",
                wss_url="wss://ws.test",
                timeout=10.0,
                reconnect_delay=0.0,
                flush_rows=10,
                flush_seconds=60.0,
                max_ticks=0,
            )

        self.assertEqual(total_ticks, 0)
        self.assertEqual(resolve_session.await_count, 1)
        self.assertEqual(record_market.await_count, 1)

    async def test_run_asset_loop_stops_after_runtime_error_from_market_recorder(self) -> None:
        catalog = _FakeCatalog()
        session = _session()

        with (
            patch.object(recorder, "HttpClient", return_value=object()),
            patch.object(
                recorder,
                "resolve_crypto_5m_session",
                new=AsyncMock(return_value=session),
            ) as resolve_session,
            patch.object(
                recorder,
                "_record_one_market",
                new=AsyncMock(side_effect=RuntimeError("recorder misconfigured")),
            ) as record_market,
            patch.object(
                recorder.asyncio,
                "sleep",
                new=AsyncMock(side_effect=AssertionError("unexpected retry")),
            ),
        ):
            total_ticks = await recorder._run_asset_loop(
                catalog=catalog,
                catalog_path="/tmp/catalog",
                asset="BTC",
                gamma_host="https://gamma.test",
                wss_url="wss://ws.test",
                timeout=10.0,
                reconnect_delay=0.0,
                flush_rows=10,
                flush_seconds=60.0,
                max_ticks=0,
            )

        self.assertEqual(total_ticks, 0)
        self.assertEqual(resolve_session.await_count, 1)
        self.assertEqual(record_market.await_count, 1)


class ResolutionWriterTests(unittest.TestCase):
    def test_write_market_resolution_appends_jsonl_once(self) -> None:
        session = _session()
        payload = {
            "slug": session.slug,
            "question": session.question,
            "closed": True,
            "resolved": True,
            "resolvedOutcome": "Up",
            "endDate": "2026-04-10T00:00:00Z",
            "outcomes": ["Up", "Down"],
            "outcomePrices": ["1", "0"],
            "clobTokenIds": ["up-token", "down-token"],
        }

        with tempfile.TemporaryDirectory() as tmp:
            wrote_first = recorder.write_market_resolution(
                catalog_path=tmp,
                asset="BTC",
                session=session,
                payload=payload,
            )
            wrote_second = recorder.write_market_resolution(
                catalog_path=tmp,
                asset="BTC",
                session=session,
                payload=payload,
            )

            self.assertTrue(wrote_first)
            self.assertFalse(wrote_second)

            metadata_path = recorder.metadata_path_for_catalog(tmp)
            with open(metadata_path, "r", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle.read().splitlines() if line.strip()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_slug"], session.slug)
        self.assertEqual(rows[0]["resolved_outcome"], "Up")
        self.assertEqual(rows[0]["outcome_prices"], ["1", "0"])
