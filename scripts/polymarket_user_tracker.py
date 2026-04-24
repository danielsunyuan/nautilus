#!/usr/bin/env python3
"""
Polymarket user tracker — records positions, trades, and activity for tracked users.

Runs on a configurable interval (default 30 min). Each cycle:
  1. Reads tracked users from TRACKED_USERS_FILE (JSON list of {address, username, notes})
  2. For each user, fetches positions + recent trades from data-api
  3. Appends timestamped snapshots to per-user JSONL files
  4. Writes a summary log line

Data is stored in:
  {OUTPUT_DIR}/
    users.json                          <- tracked user list (editable)
    snapshots/{username}_positions.jsonl <- position snapshots over time
    snapshots/{username}_trades.jsonl    <- trade history (deduplicated)

Add new users by editing users.json — no restart needed.

Usage:
    python3 -m scripts.polymarket_user_tracker          # one-shot
    python3 -m scripts.polymarket_user_tracker --loop    # run every POLL_INTERVAL_SECS
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

DEFAULT_OUTPUT_DIR = os.environ.get(
    "TRACKER_OUTPUT_DIR",
    "/workspace/outputs/polymarket/user_tracking",
)
POLL_INTERVAL_SECS = int(os.environ.get("TRACKER_POLL_INTERVAL", "1800"))  # 30 min

# Seed users — written to users.json on first run if it doesn't exist
SEED_USERS = [
    {
        "address": "0x3a2c067d262e5b2b2100251359e4c7b877283d80",
        "username": "paulalolik",
        "notes": "Multi-band weather strategy, merge arb, ~$2500/day deployed",
    },
]


def _ensure_users_file(output_dir: Path) -> Path:
    """Create users.json with seed data if it doesn't exist."""
    path = output_dir / "users.json"
    if not path.exists():
        path.write_text(json.dumps(SEED_USERS, indent=2) + "\n")
        log.info("Created %s with %d seed user(s)", path, len(SEED_USERS))
    return path


def _load_users(output_dir: Path) -> list[dict]:
    """Load tracked users from users.json. Re-read every cycle so edits take effect."""
    path = _ensure_users_file(output_dir)
    return json.loads(path.read_text())


def _fetch_json(url: str, client: httpx.Client) -> list | dict | None:
    """Fetch JSON from a URL, return None on failure."""
    try:
        r = client.get(url, timeout=20)
        if r.status_code == 200:
            return r.json()
        log.debug("HTTP %d from %s", r.status_code, url.split("?")[0])
    except Exception as exc:
        log.debug("Fetch failed %s: %s", url.split("?")[0], exc)
    return None


def _fetch_positions(address: str, client: httpx.Client) -> list[dict]:
    """Fetch current positions for a user."""
    data = _fetch_json(f"{DATA_API}/positions?user={address}", client)
    return data if isinstance(data, list) else []


def _fetch_trades(address: str, client: httpx.Client, limit: int = 500) -> list[dict]:
    """Fetch recent trades for a user (paginated)."""
    all_trades = []
    for offset in range(0, limit, 100):
        batch = _fetch_json(
            f"{DATA_API}/activity?user={address}&offset={offset}&limit=100",
            client,
        )
        if not batch:
            break
        all_trades.extend(batch)
        if len(batch) < 100:
            break
    return all_trades


def _load_seen_trade_ids(path: Path) -> set[str]:
    """Load previously recorded trade IDs to avoid duplicates."""
    seen = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                tid = rec.get("id") or rec.get("trade_id") or rec.get("transactionHash", "")
                if tid:
                    seen.add(tid)
            except json.JSONDecodeError:
                pass
    return seen


def _snapshot_user(
    user: dict,
    output_dir: Path,
    client: httpx.Client,
) -> dict:
    """Fetch and record one user's data. Returns summary stats."""
    address = user["address"]
    username = user.get("username", address[:10])
    snap_dir = output_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(tz=UTC).isoformat()

    # --- Positions snapshot ---
    positions = _fetch_positions(address, client)
    pos_path = snap_dir / f"{username}_positions.jsonl"
    if positions:
        snapshot = {
            "timestamp": now,
            "address": address,
            "username": username,
            "position_count": len(positions),
            "total_value": sum(float(p.get("currentValue", 0) or 0) for p in positions),
            "positions": positions,
        }
        with pos_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snapshot, sort_keys=True) + "\n")

    # --- Trades (deduplicated) ---
    trades = _fetch_trades(address, client)
    trades_path = snap_dir / f"{username}_trades.jsonl"
    seen_ids = _load_seen_trade_ids(trades_path)
    new_trades = []
    for t in trades:
        tid = t.get("id") or t.get("trade_id") or t.get("transactionHash", "")
        if tid and tid not in seen_ids:
            t["_recorded_at"] = now
            new_trades.append(t)
            seen_ids.add(tid)

    if new_trades:
        with trades_path.open("a", encoding="utf-8") as fh:
            for t in new_trades:
                fh.write(json.dumps(t, sort_keys=True) + "\n")

    total_value = sum(float(p.get("currentValue", 0) or 0) for p in positions)
    weather_count = sum(
        1 for p in positions
        if any(kw in (p.get("title", "") + p.get("slug", "")).lower()
               for kw in ("temperature", "weather", "highest", "lowest"))
    )

    return {
        "username": username,
        "positions": len(positions),
        "weather_positions": weather_count,
        "total_value": round(total_value, 2),
        "new_trades": len(new_trades),
    }


def run_cycle(output_dir: Path) -> None:
    """Run one tracking cycle for all users."""
    users = _load_users(output_dir)
    if not users:
        log.warning("No users to track — add entries to %s/users.json", output_dir)
        return

    log.info("Tracking %d user(s)...", len(users))
    with httpx.Client(timeout=20) as client:
        for user in users:
            try:
                stats = _snapshot_user(user, output_dir, client)
                log.info(
                    "  %s: %d positions (%d weather), $%.2f value, %d new trades",
                    stats["username"],
                    stats["positions"],
                    stats["weather_positions"],
                    stats["total_value"],
                    stats["new_trades"],
                )
            except Exception as exc:
                log.error("  %s: FAILED — %s", user.get("username", "?"), exc)

    log.info("Cycle complete.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECS,
                        help=f"Poll interval in seconds (default {POLL_INTERVAL_SECS})")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help=f"Output directory (default {DEFAULT_OUTPUT_DIR})")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.loop:
        log.info("Starting user tracker loop (interval=%ds)", args.interval)
        while True:
            try:
                run_cycle(output_dir)
            except Exception as exc:
                log.error("Cycle failed: %s", exc)
            time.sleep(args.interval)
    else:
        run_cycle(output_dir)


if __name__ == "__main__":
    main()
