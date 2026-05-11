from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]
COMPOSE_FILE = ROOT / ".docker" / "docker-compose.yml"


@pytest.fixture
def event_loop(session_event_loop):
    return session_event_loop


def test_compose_includes_binance_and_kraken_data_services() -> None:
    compose = COMPOSE_FILE.read_text()

    assert "binance-data:" in compose
    assert "nautilus-binance-data" in compose
    assert "/workspace/examples/live/binance/binance_data_tester.py" in compose

    assert "kraken-data:" in compose
    assert "nautilus-kraken-data" in compose
    assert "/workspace/examples/live/kraken/kraken_data_tester.py" in compose


def test_compose_does_not_advertise_coinbase_data_service_yet() -> None:
    compose = COMPOSE_FILE.read_text()

    assert "coinbase-data:" not in compose
