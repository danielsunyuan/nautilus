from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

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


smoke = _load_module(
    "examples.live.polymarket.polymarket_crypto_5m_paper_smoke",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_crypto_5m_paper_smoke.py",
)


def test_run_single_round_uses_bounded_runtime_and_extracts_results(monkeypatch) -> None:
    session_end = datetime(2026, 4, 14, 12, 5, tzinfo=UTC)
    session = SimpleNamespace(
        slug="btc-updown-5m-1776168000",
        end_time=session_end,
        instrument_ids={"up": "instrument-up", "down": "instrument-down"},
    )
    preset = SimpleNamespace(
        name="entry_95",
        mode="basic",
        rationale="baseline",
        entry_price=0.95,
        exit_price=0.99,
        stop_loss_price=0.5,
    )
    node = MagicMock()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        smoke,
        "_strategy_presets_for_set",
        lambda preset_set: (preset,),
    )
    monkeypatch.setattr(
        smoke,
        "build_daemon_node_config",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(smoke, "TradingNode", lambda config: node)
    monkeypatch.setattr(
        smoke,
        "_build_paper_strategy",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        smoke,
        "extract_strategy_results",
        lambda **kwargs: [{"event": "strategy_result", "strategy_name": preset.name, "slug": session.slug}],
    )

    def _fake_run_node_for_duration(*, node, duration_seconds: float) -> None:
        captured["duration_seconds"] = duration_seconds

    monkeypatch.setattr(smoke, "_run_node_for_duration", _fake_run_node_for_duration)

    rows = asyncio.run(
        smoke.run_single_round(
            session=session,
            asset="BTC",
            preset_set="quant",
            side="up",
            order_qty=5,
            execution_cutoff_seconds=15.0,
            now_fn=lambda: session_end - timedelta(seconds=75),
        ),
    )

    assert rows == [{"event": "strategy_result", "strategy_name": "entry_95", "slug": session.slug}]
    assert abs(float(captured["duration_seconds"]) - 60.0) < 1e-9
    node.trader.add_strategy.assert_called_once()
    node.build.assert_called_once()
    node.add_data_client_factory.assert_called_once()
    node.add_exec_client_factory.assert_called_once()


def test_run_single_round_rejects_unknown_side() -> None:
    session = SimpleNamespace(
        slug="btc-updown-5m-1776168000",
        end_time=datetime(2026, 4, 14, 12, 5, tzinfo=UTC),
        instrument_ids={"up": "instrument-up", "down": "instrument-down"},
    )

    try:
        asyncio.run(
            smoke.run_single_round(
                session=session,
                asset="BTC",
                preset_set="quant",
                side="sideways",
                order_qty=5,
                execution_cutoff_seconds=15.0,
            ),
        )
    except ValueError as exc:
        assert "side" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected invalid side to fail")
