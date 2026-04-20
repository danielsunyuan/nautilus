#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
# -------------------------------------------------------------------------------------------------
"""
CLI runner for Polymarket Sports Markdown reporting.

Reads all JSONL files under --report-root/polymarket/sports/,
merges with settlement.jsonl in the same dir,
and writes SPORTS_RESULTS.md.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

try:
    from examples.live.polymarket.sports_report import (
        build_sports_summary,
        render_sports_markdown,
    )
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.sports_report"
    module_path = Path(__file__).resolve().with_name("sports_report.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    build_sports_summary = module.build_sports_summary
    render_sports_markdown = module.render_sports_markdown


DEFAULT_REPORT_ROOT = "/workspace/outputs"
DEFAULT_REPORT_MD = "/workspace/SPORTS_RESULTS.md"


def _read_jsonl_dir(jsonl_dir: Path) -> list[dict]:
    """Read all JSONL files in directory, return list of parsed rows."""
    rows: list[dict] = []
    if not jsonl_dir.exists():
        return rows
    for jsonl_file in sorted(jsonl_dir.glob("*.jsonl")):
        try:
            with jsonl_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return rows


def run_report(*, report_root: str, report_md: str) -> None:
    jsonl_dir = Path(report_root) / "polymarket" / "sports"
    rows = _read_jsonl_dir(jsonl_dir)

    if not rows:
        print(f"No JSONL data found in {jsonl_dir}")
        return

    summary = build_sports_summary(rows)
    markdown = render_sports_markdown(summary)

    out_path = Path(report_md)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    print(f"Wrote {out_path}")

    # Print brief console summary
    t = summary["totals"]
    print(
        f"  Resolved: {t['resolved_trades']} | "
        f"W={t['resolved_wins']} L={t['resolved_losses']} | "
        f"Unresolved={t['unresolved']} | "
        f"P/L=${t['net_pnl']:+.4f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SPORTS_RESULTS.md from JSONL data")
    parser.add_argument(
        "--report-root",
        default=DEFAULT_REPORT_ROOT,
        help="Root directory containing polymarket/sports/ JSONL files",
    )
    parser.add_argument(
        "--report-md",
        default=DEFAULT_REPORT_MD,
        help="Path to write the Markdown report",
    )
    args = parser.parse_args()
    run_report(report_root=args.report_root, report_md=args.report_md)


if __name__ == "__main__":
    main()
