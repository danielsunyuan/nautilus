from __future__ import annotations

import asyncio
from argparse import Namespace
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
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


crypto_5m = _load_module(
    "nautilus_trader.adapters.polymarket.common.crypto_5m",
    ROOT / "nautilus_trader" / "adapters" / "polymarket" / "common" / "crypto_5m.py",
)
daemon = _load_module(
    "examples.live.polymarket.polymarket_crypto_5m_paper_daemon",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_crypto_5m_paper_daemon.py",
)


def _session(*, slug: str, end_time: datetime):
    return crypto_5m.parse_crypto_5m_market(
        {
            "slug": slug,
            "conditionId": f"condition-{slug}",
            "question": "Will BTC be up or down in the next 5 minutes?",
            "outcomes": ["Up", "Down"],
            "clobTokenIds": [f"{slug}-up", f"{slug}-down"],
            "active": True,
            "closed": False,
            "archived": False,
            "acceptingOrders": True,
            "endDateIso": end_time.isoformat().replace("+00:00", "Z"),
        },
        asset="BTC",
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_run_daemon_once_writes_round_boundary_and_result_records(tmp_path: Path) -> None:
    first_end = datetime(2026, 4, 14, 12, 5, tzinfo=UTC)
    session = _session(slug="btc-updown-5m-1776168000", end_time=first_end)
    output_path = tmp_path / "outputs" / "polymarket" / "runs" / "overnight_quant_20260414T120000Z.jsonl"
    round_result = [
        {
            "event": "strategy_result",
            "strategy_name": "entry_95",
            "pnl": "1.25",
            "exit_reason": "target",
        }
    ]

    resolve_session = AsyncMock(return_value=session)
    run_round = AsyncMock(return_value=round_result)
    sleep_until_next_round = AsyncMock()
    writer = daemon.JsonlRunWriter(output_path)

    asyncio.run(
        daemon.run_daemon(
            asset="BTC",
            preset_set="quant",
            resolve_session=resolve_session,
            run_round=run_round,
            sleep_until_next_round=sleep_until_next_round,
            writer=writer,
            now_fn=lambda: first_end - timedelta(minutes=4, seconds=30),
            max_rounds=1,
        ),
    )

    rows = _read_jsonl(output_path)

    assert [row["event"] for row in rows] == ["round_start", "strategy_result", "round_end"]
    assert rows[0]["slug"] == session.slug
    assert rows[1]["strategy_name"] == "entry_95"
    assert rows[2]["slug"] == session.slug
    assert sleep_until_next_round.await_count == 0


def test_run_daemon_rolls_to_next_round_until_max_rounds(tmp_path: Path) -> None:
    first_end = datetime(2026, 4, 14, 12, 5, tzinfo=UTC)
    second_end = datetime(2026, 4, 14, 12, 10, tzinfo=UTC)
    sessions = [
        _session(slug="btc-updown-5m-1776168000", end_time=first_end),
        _session(slug="btc-updown-5m-1776168300", end_time=second_end),
    ]
    output_path = tmp_path / "runs.jsonl"
    resolve_session = AsyncMock(side_effect=sessions)
    run_round = AsyncMock(
        side_effect=[
            [{"event": "strategy_result", "strategy_name": "entry_95"}],
            [{"event": "strategy_result", "strategy_name": "support_ratio_95"}],
        ],
    )
    sleep_until_next_round = AsyncMock()

    asyncio.run(
        daemon.run_daemon(
            asset="BTC",
            preset_set="quant",
            resolve_session=resolve_session,
            run_round=run_round,
            sleep_until_next_round=sleep_until_next_round,
            writer=daemon.JsonlRunWriter(output_path),
            now_fn=lambda: first_end - timedelta(minutes=4),
            max_rounds=2,
        ),
    )

    rows = _read_jsonl(output_path)
    assert [row["slug"] for row in rows if row["event"] == "round_start"] == [sessions[0].slug, sessions[1].slug]
    assert sleep_until_next_round.await_count == 1


def test_run_daemon_writes_round_skipped_when_session_resolution_fails(tmp_path: Path) -> None:
    output_path = tmp_path / "runs.jsonl"
    resolve_session = AsyncMock(side_effect=RuntimeError("gamma unavailable"))
    sleep_until_next_round = AsyncMock()
    backoff_sleep = AsyncMock()

    asyncio.run(
        daemon.run_daemon(
            asset="BTC",
            preset_set="quant",
            resolve_session=resolve_session,
            run_round=AsyncMock(),
            sleep_until_next_round=sleep_until_next_round,
            writer=daemon.JsonlRunWriter(output_path),
            now_fn=lambda: datetime(2026, 4, 14, 12, 1, tzinfo=UTC),
            max_rounds=1,
            backoff_sleep=backoff_sleep,
        ),
    )

    rows = _read_jsonl(output_path)
    assert rows[0]["event"] == "error"
    assert rows[1]["event"] == "round_skipped"
    assert "gamma unavailable" in rows[1]["reason"]
    assert backoff_sleep.await_count == 1
    assert sleep_until_next_round.await_count == 0


def test_run_daemon_writes_error_and_continues_after_recoverable_round_error(tmp_path: Path) -> None:
    first_end = datetime(2026, 4, 14, 12, 5, tzinfo=UTC)
    second_end = datetime(2026, 4, 14, 12, 10, tzinfo=UTC)
    sessions = [
        _session(slug="btc-updown-5m-1776168000", end_time=first_end),
        _session(slug="btc-updown-5m-1776168300", end_time=second_end),
    ]
    output_path = tmp_path / "runs.jsonl"
    resolve_session = AsyncMock(side_effect=sessions)
    run_round = AsyncMock(
        side_effect=[
            daemon.RecoverableDaemonError("websocket disconnect"),
            [{"event": "strategy_result", "strategy_name": "entry_95"}],
        ],
    )
    sleep_until_next_round = AsyncMock()
    backoff_sleep = AsyncMock()

    asyncio.run(
        daemon.run_daemon(
            asset="BTC",
            preset_set="quant",
            resolve_session=resolve_session,
            run_round=run_round,
            sleep_until_next_round=sleep_until_next_round,
            writer=daemon.JsonlRunWriter(output_path),
            now_fn=lambda: datetime(2026, 4, 14, 12, 1, tzinfo=UTC),
            max_rounds=2,
            backoff_sleep=backoff_sleep,
        ),
    )

    rows = _read_jsonl(output_path)

    assert [row["event"] for row in rows] == [
        "round_start",
        "error",
        "round_skipped",
        "round_start",
        "strategy_result",
        "round_end",
    ]
    assert rows[1]["reason"] == "websocket disconnect"
    assert rows[-1]["slug"] == sessions[1].slug
    assert backoff_sleep.await_count == 1


def test_daemon_main_wires_parser_and_async_runner(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _Parser:
        def parse_args(self) -> Namespace:
            return Namespace(
                asset="BTC",
                preset_set="quant",
                output_dir=str(tmp_path / "outputs"),
                gamma_host="https://gamma.test",
                timeout=15.0,
                max_rounds=1,
                reconnect_delay=2.0,
                execution_cutoff_seconds=15.0,
            )

    async def _fake_run_main_loop(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(daemon, "_build_parser", lambda: _Parser())
    monkeypatch.setattr(daemon, "_run_main_loop", _fake_run_main_loop)

    result = daemon.main()

    assert result == 0
    assert captured["asset"] == "BTC"
    assert captured["preset_set"] == "quant"
    assert Path(captured["output_dir"]).name == "outputs"
    assert captured["gamma_host"] == "https://gamma.test"


def test_build_daemon_node_configures_isolated_runtime_namespaces() -> None:
    config = daemon.build_daemon_node_config(
        instrument_ids=["instrument-1", "instrument-2"],
        trader_id="PAPER-5M-DAEMON",
        cache_host="redis",
        cache_port=6379,
    )

    assert config.trader_id.value == "PAPER-5M-DAEMON"
    assert config.cache.database.host == "redis"
    assert config.message_bus.database.port == 6379
    assert config.message_bus.streams_prefix == "polymarket-5m"
    assert config.message_bus.use_instance_id is True


def test_build_output_path_uses_recommended_run_directory() -> None:
    now = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)

    path = daemon.build_output_path(
        output_dir=Path("/tmp/outputs"),
        preset_set="quant",
        now=now,
    )

    assert path == Path("/tmp/outputs/polymarket/runs/overnight_quant_20260414T120000Z.jsonl")


def test_build_output_path_rejects_escape_in_preset_set() -> None:
    try:
        daemon.build_output_path(
            output_dir=Path("/tmp/outputs"),
            preset_set="../quant",
            now=datetime(2026, 4, 14, 12, 0, tzinfo=UTC),
        )
    except ValueError as exc:
        assert "preset_set" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected preset_set validation failure")


def test_run_daemon_uses_jittered_backoff_delay(tmp_path: Path) -> None:
    output_path = tmp_path / "runs.jsonl"
    resolve_session = AsyncMock(side_effect=RuntimeError("gamma unavailable"))
    backoff_sleep = AsyncMock()

    with patch.object(daemon.random, "uniform", return_value=0.5):
        asyncio.run(
            daemon.run_daemon(
                asset="BTC",
                preset_set="quant",
                resolve_session=resolve_session,
                run_round=AsyncMock(),
                sleep_until_next_round=AsyncMock(),
                writer=daemon.JsonlRunWriter(output_path),
                now_fn=lambda: datetime(2026, 4, 14, 12, 1, tzinfo=UTC),
                max_rounds=1,
                backoff_sleep=backoff_sleep,
                reconnect_delay=2.0,
            ),
        )

    backoff_sleep.assert_awaited_once_with(2.5)


def test_strategy_presets_for_supported_sets_cover_all_named_variants() -> None:
    assert daemon._strategy_presets_for_set("quant")
    assert daemon._strategy_presets_for_set("grid")
    assert daemon._strategy_presets_for_set("all")
    assert daemon._strategy_presets_for_set("advanced")
    assert daemon._strategy_presets_for_set("momentum")
    assert daemon._strategy_presets_for_set("flow")

    try:
        daemon._strategy_presets_for_set("unknown")
    except ValueError as exc:
        assert "unsupported preset set" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected unsupported preset set to fail")
