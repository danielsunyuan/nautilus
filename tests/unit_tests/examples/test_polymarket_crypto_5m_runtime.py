from __future__ import annotations

import asyncio
from argparse import Namespace
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from decimal import Decimal
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


def test_candidate_crypto_5m_market_slugs_include_current_next_then_previous_window() -> None:
    now = datetime(2026, 4, 12, 12, 7, 11, tzinfo=UTC)

    assert crypto_5m.candidate_crypto_5m_market_slugs(asset="BTC", now=now) == [
        "btc-updown-5m-1775995500",
        "btc-updown-5m-1775995800",
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
    assert session.round_start == datetime(2026, 4, 13, 7, 20, tzinfo=UTC)
    assert session.end_time == datetime(2026, 4, 13, 7, 25, tzinfo=UTC)


def test_parse_crypto_5m_market_prefers_slug_derived_end_time_over_stale_gamma_end_date() -> None:
    session = crypto_5m.parse_crypto_5m_market(
        _market_payload(
            slug="btc-updown-5m-1775995200",
            endDateIso="2026-04-12T00:00:00Z",
        ),
        asset="BTC",
    )

    assert session.round_start == datetime(2026, 4, 12, 12, 0, tzinfo=UTC)
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


def test_validate_crypto_5m_market_rejects_expired_markets() -> None:
    session = crypto_5m.parse_crypto_5m_market(
        _market_payload(endDateIso="2026-04-12T12:05:00Z"),
        asset="BTC",
    )

    with pytest.raises(ValueError, match="expired"):
        crypto_5m.validate_crypto_5m_market(
            session,
            now=datetime(2026, 4, 13, 7, 26, tzinfo=UTC),
        )


def test_resolve_crypto_5m_session_rejects_expired_previous_window_fallback() -> None:
    now = datetime(2026, 4, 12, 12, 7, 11, tzinfo=UTC)
    http_client = MagicMock()
    http_client.get = AsyncMock(
        side_effect=[
            _response(404, {"error": "not found"}),
            _response(404, {"error": "not found"}),
            _response(200, _market_payload(slug="btc-updown-5m-1775995200")),
        ],
    )

    with pytest.raises(RuntimeError, match="could not resolve a live 5m market"):
        asyncio.run(
            crypto_5m.resolve_crypto_5m_session(
                asset="BTC",
                http_client=http_client,
                now=now,
            ),
        )

    assert http_client.get.await_count == 3


def test_resolve_crypto_5m_session_falls_forward_when_current_market_inactive() -> None:
    now = datetime(2026, 4, 12, 12, 7, 11, tzinfo=UTC)
    http_client = MagicMock()
    http_client.get = AsyncMock(
        side_effect=[
            _response(200, _market_payload(slug="btc-updown-5m-1775995500", active=False)),
            _response(200, _market_payload(slug="btc-updown-5m-1775995800")),
        ],
    )

    session = asyncio.run(
        crypto_5m.resolve_crypto_5m_session(
            asset="BTC",
            http_client=http_client,
            now=now,
        ),
    )

    assert session.slug == "btc-updown-5m-1775995800"
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
                preset_set="quant",
                order_qty=Decimal("5"),
                execution_cutoff_seconds=15.0,
            )

    async def _fake_resolve_session(asset: str, gamma_host: str, timeout: float):
        captured["resolve_args"] = (asset, gamma_host, timeout)
        return session

    async def _fake_run_single_round(**kwargs):
        captured["round_kwargs"] = kwargs
        return [{"event": "strategy_result", "strategy_name": "entry_95"}]

    monkeypatch.setattr(smoke_script, "_build_parser", lambda: _Parser())
    monkeypatch.setattr(smoke_script, "_resolve_session", _fake_resolve_session)
    monkeypatch.setattr(smoke_script, "run_single_round", _fake_run_single_round)

    result = smoke_script.main()

    assert result == 0
    assert captured["resolve_args"] == ("BTC", "https://gamma.test", 10.0)
    assert captured["round_kwargs"]["session"] == session
    assert captured["round_kwargs"]["asset"] == "BTC"
    assert captured["round_kwargs"]["preset_set"] == "quant"
    assert captured["round_kwargs"]["side"] == "up"
    assert captured["round_kwargs"]["order_qty"] == Decimal("5")
    assert captured["round_kwargs"]["execution_cutoff_seconds"] == 15.0


def test_fetch_crypto_5m_market_rejects_invalid_base_url_scheme() -> None:
    http_client = MagicMock()

    with pytest.raises(ValueError, match="gamma_base_url"):
        asyncio.run(
            crypto_5m.fetch_crypto_5m_market(
                slug="btc-updown-5m-1776064800",
                http_client=http_client,
                gamma_base_url="file:///tmp/evil",
                timeout=10.0,
            ),
        )
