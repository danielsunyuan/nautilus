#!/usr/bin/env python3
"""
Latency and live-readiness gate for Polymarket 5-minute execution work.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LatencyObservation:
    signal_to_order_ms: float
    boundary_headroom_ms: float


@dataclass(frozen=True)
class ReadinessThresholds:
    max_signal_to_order_p95_ms: float = 750.0
    min_boundary_headroom_ms: float = 1500.0
    max_disconnect_recovery_seconds: float = 10.0
    require_cancel_replace_known: bool = True
    require_kill_switch: bool = True
    require_parity_passed: bool = True


def summarize_latency_observations(observations: list[LatencyObservation]) -> dict[str, Any]:
    if not observations:
        return {
            "samples": 0,
            "signal_to_order_p50_ms": None,
            "signal_to_order_p95_ms": None,
            "min_boundary_headroom_ms": None,
        }
    values = sorted(float(item.signal_to_order_ms) for item in observations)
    headroom = min(float(item.boundary_headroom_ms) for item in observations)
    mid = len(values) // 2
    p50 = values[mid]
    p95 = values[-1]
    return {
        "samples": len(values),
        "signal_to_order_p50_ms": round(p50, 3),
        "signal_to_order_p95_ms": round(p95, 3),
        "min_boundary_headroom_ms": round(headroom, 3),
    }


def assess_live_readiness(
    *,
    generated_at: datetime,
    latency_summary: dict[str, Any],
    disconnect_summary: dict[str, Any],
    cancel_replace_supported: bool | None,
    kill_switch_configured: bool,
    routing_assumption: str,
    parity_passed: bool,
    thresholds: ReadinessThresholds | None = None,
) -> dict[str, Any]:
    config = thresholds or ReadinessThresholds()
    checks = {
        "signal_latency": {
            "actual": latency_summary.get("signal_to_order_p95_ms"),
            "threshold": config.max_signal_to_order_p95_ms,
            "passed": (
                latency_summary.get("signal_to_order_p95_ms") is not None
                and float(latency_summary["signal_to_order_p95_ms"]) <= float(config.max_signal_to_order_p95_ms)
            ),
        },
        "boundary_headroom": {
            "actual": latency_summary.get("min_boundary_headroom_ms"),
            "threshold": config.min_boundary_headroom_ms,
            "passed": (
                latency_summary.get("min_boundary_headroom_ms") is not None
                and float(latency_summary["min_boundary_headroom_ms"]) >= float(config.min_boundary_headroom_ms)
            ),
        },
        "disconnect_recovery": {
            "actual": disconnect_summary.get("max_recovery_seconds"),
            "threshold": config.max_disconnect_recovery_seconds,
            "passed": (
                int(disconnect_summary.get("events") or 0) == int(disconnect_summary.get("recoveries") or 0)
                and disconnect_summary.get("max_recovery_seconds") is not None
                and float(disconnect_summary["max_recovery_seconds"]) <= float(config.max_disconnect_recovery_seconds)
            ),
        },
        "cancel_replace_semantics": {
            "actual": cancel_replace_supported,
            "threshold": True if config.require_cancel_replace_known else None,
            "passed": (cancel_replace_supported is True) if config.require_cancel_replace_known else True,
        },
        "kill_switch": {
            "actual": kill_switch_configured,
            "threshold": True if config.require_kill_switch else None,
            "passed": bool(kill_switch_configured) if config.require_kill_switch else True,
        },
        "parity_gate": {
            "actual": parity_passed,
            "threshold": True if config.require_parity_passed else None,
            "passed": bool(parity_passed) if config.require_parity_passed else True,
        },
        "routing_assumptions": {
            "actual": routing_assumption,
            "threshold": "documented",
            "passed": bool(str(routing_assumption).strip()) and str(routing_assumption).strip().lower() != "unknown",
        },
    }
    blockers = [name for name, row in checks.items() if not row["passed"]]
    return {
        "generated_at": generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "passed": not blockers,
        "blockers": blockers,
        "latency_summary": latency_summary,
        "disconnect_summary": disconnect_summary,
        "checks": checks,
        "routing_assumption": routing_assumption,
    }


def render_readiness_markdown(report: dict[str, Any]) -> str:
    md: list[str] = []
    md.append("# Polymarket 5m Live Readiness Gate")
    md.append("")
    md.append(f"> Generated at: {report['generated_at']}")
    md.append("")
    md.append(f"Overall status: {'PASS' if report['passed'] else 'BLOCKED'}")
    md.append("")
    md.append("## Checks")
    md.append("")
    md.append("| Check | Passed | Actual | Threshold |")
    md.append("|------|:------:|--------|-----------|")
    for name, row in report["checks"].items():
        md.append(f"| {name} | {'yes' if row['passed'] else 'no'} | {row['actual']} | {row['threshold']} |")
    if report["blockers"]:
        md.append("")
        md.append("## Blocking Checks")
        md.append("")
        for blocker in report["blockers"]:
            md.append(f"- {blocker}")
    md.append("")
    return "\n".join(md)


def write_readiness_outputs(*, report_root: str | Path, report: dict[str, Any]) -> dict[str, Path]:
    root = Path(report_root) / "polymarket" / "reports"
    root.mkdir(parents=True, exist_ok=True)
    timestamp = str(report["generated_at"]).replace("-", "").replace(":", "")
    timestamp = timestamp[:15] + "Z" if len(timestamp) >= 15 else timestamp
    json_path = root / "readiness_latest.json"
    stamped_json = root / f"readiness_{timestamp}.json"
    markdown_path = root / "READINESS.md"
    payload = json.dumps(report, indent=2, sort_keys=True)
    json_path.write_text(payload, encoding="utf-8")
    stamped_json.write_text(payload, encoding="utf-8")
    markdown_path.write_text(render_readiness_markdown(report), encoding="utf-8")
    return {
        "readiness_latest": json_path,
        "readiness_timestamped": stamped_json,
        "readiness_markdown": markdown_path,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", required=True, help="Path to readiness evidence JSON")
    parser.add_argument("--report-root", default="/workspace/outputs")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    report = assess_live_readiness(
        generated_at=datetime.now(tz=UTC),
        latency_summary=payload["latency_summary"],
        disconnect_summary=payload["disconnect_summary"],
        cancel_replace_supported=payload.get("cancel_replace_supported"),
        kill_switch_configured=bool(payload.get("kill_switch_configured")),
        routing_assumption=str(payload.get("routing_assumption") or ""),
        parity_passed=bool(payload.get("parity_passed")),
    )
    paths = write_readiness_outputs(report_root=args.report_root, report=report)
    print(json.dumps({key: str(value) for key, value in paths.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
