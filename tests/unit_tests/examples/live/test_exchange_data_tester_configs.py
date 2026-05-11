from __future__ import annotations

import pytest

from examples.live.binance.binance_data_tester import build_runtime as build_binance_runtime
from examples.live.kraken.kraken_data_tester import build_runtime as build_kraken_runtime


@pytest.fixture
def event_loop(session_event_loop):
    return session_event_loop


def test_binance_runtime_builds_spot_instrument_id() -> None:
    runtime = build_binance_runtime(account_type_name="spot", symbol="BTCUSDT")

    assert runtime.instrument_id.value == "BTCUSDT.BINANCE"
    assert runtime.subscribe_bars is True


def test_binance_runtime_builds_futures_instrument_id() -> None:
    runtime = build_binance_runtime(account_type_name="usdt_futures", symbol="BTCUSDT-PERP")

    assert runtime.instrument_id.value == "BTCUSDT-PERP.BINANCE"
    assert runtime.subscribe_bars is False


def test_binance_runtime_rejects_unsupported_account_type() -> None:
    with pytest.raises(ValueError, match="Unsupported Binance account type"):
        build_binance_runtime(account_type_name="margin", symbol="BTCUSDT")


def test_kraken_runtime_builds_spot_instrument_id() -> None:
    runtime = build_kraken_runtime(product_type_name="spot", symbol="BTC/USD")

    assert runtime.instrument_id.value == "BTC/USD.KRAKEN"
    assert runtime.subscribe_bars is True
    assert runtime.subscribe_mark_prices is False


def test_kraken_runtime_builds_futures_instrument_id() -> None:
    runtime = build_kraken_runtime(product_type_name="futures", symbol="PI_XBTUSD")

    assert runtime.instrument_id.value == "PI_XBTUSD.KRAKEN"
    assert runtime.subscribe_bars is False
    assert runtime.subscribe_mark_prices is True


def test_kraken_runtime_rejects_unsupported_product_type() -> None:
    with pytest.raises(ValueError, match="Unsupported Kraken product type"):
        build_kraken_runtime(product_type_name="options", symbol="BTC/USD")
