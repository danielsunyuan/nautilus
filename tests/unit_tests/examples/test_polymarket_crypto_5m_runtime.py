from __future__ import annotations

import asyncio
from argparse import Namespace
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import Mock

import msgspec
import pytest

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
    with _without_repo_root_on_sys_path():
        spec.loader.exec_module(module)
    if previous is None:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = previous
    return module


_crypto_5m_module_name = "nautilus_trader.adapters.polymarket.common.crypto_5m"
_previous_crypto_5m_module = sys.modules.get(_crypto_5m_module_name)
crypto_5m = _load_module(
    _crypto_5m_module_name,
    ROOT / "nautilus_trader" / "adapters" / "polymarket" / "common" / "crypto_5m.py",
)
sys.modules[_crypto_5m_module_name] = crypto_5m
slug_builders = _load_module(
    "examples.live.polymarket.slug_builders",
    ROOT / "examples" / "live" / "polymarket" / "slug_builders.py",
)
smoke_script = _load_module(
    "examples.live.polymarket.polymarket_crypto_5m_paper_smoke",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_crypto_5m_paper_smoke.py",
)
if _previous_crypto_5m_module is None:
    sys.modules.pop(_crypto_5m_module_name, None)
else:
    sys.modules[_crypto_5m_module_name] = _previous_crypto_5m_module


def _response(status: int, payload: object) -> Mock:
    response = Mock()
    response.status = status
    response.body = msgspec.json.encode(payload)
    return response


def _market_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "slug": "btc-updown-5m-1776064800",
        "conditionId": "condition-123",
        "question": "Will BTC be up or down in the next 5 minutes?",
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["up-token", "down-token"],
        "active": True,
        "closed": False,
        "archived": False,
        "acceptingOrders": True,
        "endDateIso": "2026-04-12T12:05:00Z",
    }
    payload.update(overrides)
    return payload


def test_current_crypto_5m_market_slug_rounds_down_to_current_window() -> None:
    now = datetime(2026, 4, 12, 12, 7, 11, tzinfo=UTC)

    assert crypto_5m.current_crypto_5m_market_slug(asset="BTC", now=now) == "btc-updown-5m-1775995500"


def test_candidate_crypto_5m_market_slugs_include_current_then_previous_window() -> None:
    now = datetime(2026, 4, 12, 12, 7, 11, tzinfo=UTC)

    assert crypto_5m.candidate_crypto_5m_market_slugs(asset="BTC", now=now) == [
        "btc-updown-5m-1775995500",
        "btc-updown-5m-1775995200",
    ]


def test_parse_crypto_5m_market_builds_token_and_instrument_maps() -> None:
    session = crypto_5m.parse_crypto_5m_market(_market_payload(), asset="BTC")

    assert session.asset == "BTC"
    assert session.slug == "btc-updown-5m-1776064800"
    assert session.condition_id == "condition-123"
    assert session.token_ids == {"up": "up-token", "down": "down-token"}
    assert str(session.instrument_ids["up"]) == "condition-123-up-token.POLYMARKET"
    assert str(session.instrument_ids["down"]) == "condition-123-down-token.POLYMARKET"
    assert session.end_time == datetime(2026, 4, 12, 12, 5, tzinfo=UTC)


def test_validate_crypto_5m_market_rejects_closed_or_non_accepting_markets() -> None:
    with pytest.raises(ValueError, match="closed"):
        crypto_5m.validate_crypto_5m_market(
            crypto_5m.parse_crypto_5m_market(_market_payload(closed=True), asset="BTC"),
        )

    with pytest.raises(ValueError, match="accepting orders"):
        crypto_5m.validate_crypto_5m_market(
            crypto_5m.parse_crypto_5m_market(_market_payload(acceptingOrders=False), asset="BTC"),
        )

    with pytest.raises(ValueError, match="inactive"):
        crypto_5m.validate_crypto_5m_market(
            crypto_5m.parse_crypto_5m_market(_market_payload(active=None), asset="BTC"),
        )


def test_resolve_crypto_5m_session_falls_back_to_previous_window_when_current_missing() -> None:
    now = datetime(2026, 4, 12, 12, 7, 11, tzinfo=UTC)
    http_client = MagicMock()
    http_client.get = AsyncMock(
        side_effect=[
            _response(404, {"error": "not found"}),
            _response(200, _market_payload(slug="btc-updown-5m-1775995200")),
        ],
    )

    session = asyncio.run(
        crypto_5m.resolve_crypto_5m_session(
            asset="BTC",
            http_client=http_client,
            now=now,
        ),
    )

    assert session.slug == "btc-updown-5m-1775995200"
    assert session.token_ids["up"] == "up-token"
    assert http_client.get.await_count == 2


def test_slug_builders_cover_btc_and_all_supported_assets_for_5m_rounds() -> None:
    btc_slugs = slug_builders.build_btc_updown_5m_slugs()
    multi_asset_slugs = slug_builders.build_crypto_updown_5m_slugs()

    assert btc_slugs
    assert all(slug.startswith("btc-updown-5m-") for slug in btc_slugs)
    assert any(slug.startswith("bnb-updown-5m-") for slug in multi_asset_slugs)
    assert any(slug.startswith("hype-updown-5m-") for slug in multi_asset_slugs)
    assert crypto_5m.SUPPORTED_ASSETS == ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")


def test_crypto_5m_smoke_script_main_wires_resolver_and_sandbox_execution(monkeypatch) -> None:
    session = crypto_5m.parse_crypto_5m_market(_market_payload(), asset="BTC")
    captured: dict[str, object] = {}

    class _Parser:
        def parse_args(self) -> Namespace:
            return Namespace(
                asset="BTC",
                gamma_host="https://gamma.test",
                timeout=10.0,
                side="up",
            )

    class _DummyNode:
        def __init__(self, config):
            captured["node_config"] = config
            self.trader = SimpleNamespace(add_strategy=lambda strategy: captured.setdefault("strategy", strategy))

        def add_data_client_factory(self, venue, factory) -> None:
            captured["data_factory"] = (venue, factory)

        def add_exec_client_factory(self, venue, factory) -> None:
            captured["exec_factory"] = (venue, factory)

        def build(self) -> None:
            captured["built"] = True

        def run(self) -> None:
            captured["ran"] = True

        def dispose(self) -> None:
            captured["disposed"] = True

    async def _fake_resolve_session(asset: str, gamma_host: str, timeout: float):
        captured["resolve_args"] = (asset, gamma_host, timeout)
        return session

    def _fake_exec_tester(*, config):
        captured["tester_config"] = config
        return SimpleNamespace(config=config)

    monkeypatch.setattr(smoke_script, "_build_parser", lambda: _Parser())
    monkeypatch.setattr(smoke_script, "_resolve_session", _fake_resolve_session)
    monkeypatch.setattr(smoke_script, "TradingNode", _DummyNode)
    monkeypatch.setattr(smoke_script, "ExecTester", _fake_exec_tester)

    result = smoke_script.main()

    assert result == 0
    assert captured["resolve_args"] == ("BTC", "https://gamma.test", 10.0)
    assert str(captured["tester_config"].instrument_id) == str(session.instrument_ids["up"])
    assert captured["node_config"].trader_id.value == "PAPER-5M-001"
    assert smoke_script.POLYMARKET in captured["node_config"].exec_clients
    assert captured["built"] is True
    assert captured["ran"] is True
    assert captured["disposed"] is True
