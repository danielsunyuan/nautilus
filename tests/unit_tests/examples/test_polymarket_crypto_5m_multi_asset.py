from __future__ import annotations

import asyncio
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
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


recorder = _load_module(
    "examples.live.polymarket.polymarket_crypto_5m_recorder",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_crypto_5m_recorder.py",
)


def test_parse_assets_normalizes_requested_symbols() -> None:
    assets = recorder.parse_assets(" btc, eth ,doge ")

    assert assets == ("BTC", "ETH", "DOGE")


def test_parse_assets_rejects_unsupported_symbols() -> None:
    try:
        recorder.parse_assets("BTC,ABC")
    except ValueError as exc:
        assert "unsupported assets" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected unsupported asset error")


def test_parse_assets_defaults_to_all_supported_assets_when_blank() -> None:
    assert recorder.parse_assets("") == recorder.SUPPORTED_ASSETS


def test_run_schedules_one_asset_loop_per_requested_asset() -> None:
    args = type(
        "Args",
        (),
        {
            "catalog_path": "/tmp/catalog",
            "assets": "BTC,ETH,SOL",
            "gamma_host": "https://gamma.test",
            "wss_url": "wss://ws.test",
            "timeout": 10.0,
            "reconnect_delay": 1.0,
            "flush_rows": 100,
            "flush_seconds": 5.0,
            "max_ticks": 10,
        },
    )()
    captured: list[dict[str, object]] = []

    async def _fake_run_asset_loop(**kwargs):
        captured.append(kwargs)
        return 1

    with (
        patch.object(recorder, "ParquetDataCatalog", return_value=MagicMock()),
        patch.object(recorder, "_run_asset_loop", new=AsyncMock(side_effect=_fake_run_asset_loop)),
    ):
        result = asyncio.run(recorder._run(args))

    assert result == 0
    assert [row["asset"] for row in captured] == ["BTC", "ETH", "SOL"]
    assert all(row["catalog_path"] == "/tmp/catalog" for row in captured)
    assert all(row["max_ticks"] == 10 for row in captured)
