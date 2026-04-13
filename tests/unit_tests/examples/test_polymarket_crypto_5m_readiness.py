from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
import importlib.util
from pathlib import Path
import sys


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


readiness = _load_module(
    "examples.live.polymarket.polymarket_crypto_5m_readiness",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_crypto_5m_readiness.py",
)


def test_summarize_latency_observations_computes_percentiles_and_headroom() -> None:
    summary = readiness.summarize_latency_observations(
        [
            readiness.LatencyObservation(signal_to_order_ms=180.0, boundary_headroom_ms=2500.0),
            readiness.LatencyObservation(signal_to_order_ms=220.0, boundary_headroom_ms=1800.0),
            readiness.LatencyObservation(signal_to_order_ms=260.0, boundary_headroom_ms=1600.0),
        ],
    )

    assert summary["samples"] == 3
    assert summary["signal_to_order_p50_ms"] == 220.0
    assert summary["signal_to_order_p95_ms"] == 260.0
    assert summary["min_boundary_headroom_ms"] == 1600.0


def test_assess_live_readiness_passes_when_all_checks_meet_thresholds() -> None:
    report = readiness.assess_live_readiness(
        generated_at=datetime(2026, 4, 14, 14, 0, tzinfo=UTC),
        latency_summary={
            "samples": 3,
            "signal_to_order_p50_ms": 220.0,
            "signal_to_order_p95_ms": 260.0,
            "min_boundary_headroom_ms": 1600.0,
        },
        disconnect_summary={
            "events": 2,
            "recoveries": 2,
            "max_recovery_seconds": 4.0,
        },
        cancel_replace_supported=True,
        kill_switch_configured=True,
        routing_assumption="Sandbox execution only; live routing unresolved and intentionally gated.",
        parity_passed=True,
    )

    assert report["passed"] is True
    assert report["checks"]["signal_latency"]["passed"] is True
    assert report["checks"]["disconnect_recovery"]["passed"] is True
    assert report["checks"]["kill_switch"]["passed"] is True


def test_assess_live_readiness_fails_on_disconnect_and_kill_switch_gaps() -> None:
    report = readiness.assess_live_readiness(
        generated_at=datetime(2026, 4, 14, 14, 0, tzinfo=UTC),
        latency_summary={
            "samples": 1,
            "signal_to_order_p50_ms": 900.0,
            "signal_to_order_p95_ms": 900.0,
            "min_boundary_headroom_ms": 500.0,
        },
        disconnect_summary={
            "events": 2,
            "recoveries": 1,
            "max_recovery_seconds": 14.0,
        },
        cancel_replace_supported=None,
        kill_switch_configured=False,
        routing_assumption="Unknown",
        parity_passed=False,
    )

    assert report["passed"] is False
    assert report["checks"]["signal_latency"]["passed"] is False
    assert report["checks"]["boundary_headroom"]["passed"] is False
    assert report["checks"]["disconnect_recovery"]["passed"] is False
    assert report["checks"]["cancel_replace_semantics"]["passed"] is False
    assert report["checks"]["kill_switch"]["passed"] is False
    assert report["checks"]["parity_gate"]["passed"] is False


def test_render_readiness_markdown_includes_blockers() -> None:
    report = readiness.assess_live_readiness(
        generated_at=datetime(2026, 4, 14, 14, 0, tzinfo=UTC),
        latency_summary={
            "samples": 1,
            "signal_to_order_p50_ms": 900.0,
            "signal_to_order_p95_ms": 900.0,
            "min_boundary_headroom_ms": 500.0,
        },
        disconnect_summary={
            "events": 1,
            "recoveries": 0,
            "max_recovery_seconds": None,
        },
        cancel_replace_supported=None,
        kill_switch_configured=False,
        routing_assumption="Unknown",
        parity_passed=False,
    )

    markdown = readiness.render_readiness_markdown(report)

    assert "# Polymarket 5m Live Readiness Gate" in markdown
    assert "## Blocking Checks" in markdown
    assert "signal_latency" in markdown
    assert "kill_switch" in markdown
