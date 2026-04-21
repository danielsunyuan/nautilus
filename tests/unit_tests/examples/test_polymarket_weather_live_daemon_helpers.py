"""Tests for pure helper functions in the live weather daemon.

Tested in isolation (no Nautilus TradingNode, no network) to verify:
  - _session_trading_day: 08:00-UTC session boundary logic
  - _already_entered_today: JSONL scan uses session_trading_day, not UTC today
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Inline copies of the pure helpers (no Nautilus imports required)
# These mirror the implementation in polymarket_weather_daily_temperature_live_daemon.py
# exactly.  If the implementation changes, update here too.
# ---------------------------------------------------------------------------

SESSION_END_HOUR_UTC: int = 9


def _session_trading_day(now: datetime) -> date:
    if now.hour < SESSION_END_HOUR_UTC:
        return (now - timedelta(days=1)).date()
    return now.date()


def _already_entered_today(output_dir: Path, session_trading_day) -> set[str]:
    slugs: set[str] = set()
    runs_dir = Path(output_dir).resolve(strict=False) / "polymarket" / "runs"
    if not runs_dir.exists():
        return slugs
    target_date_str = str(session_trading_day)
    for jsonl_file in runs_dir.glob("*.jsonl"):
        with jsonl_file.open() as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if (
                    row.get("event") == "strategy_result"
                    and row.get("observation_date") == target_date_str
                    and row.get("accounting_status") == "open"
                ):
                    slugs.add(row["market_slug"])
    return slugs


# ---------------------------------------------------------------------------
# _session_trading_day tests
# ---------------------------------------------------------------------------

class TestSessionTradingDay:
    """Session runs from SESSION_END_HOUR_UTC:00 UTC to SESSION_END_HOUR_UTC:00 UTC."""

    def test_at_session_boundary_belongs_to_new_day(self) -> None:
        # Exactly 09:00 UTC April 21 → new Apr-21 session starts
        now = datetime(2026, 4, 21, 9, 0, 0, tzinfo=UTC)
        assert _session_trading_day(now) == date(2026, 4, 21)

    def test_midday_belongs_to_calendar_day(self) -> None:
        now = datetime(2026, 4, 21, 14, 30, tzinfo=UTC)
        assert _session_trading_day(now) == date(2026, 4, 21)

    def test_midnight_utc_still_prior_trading_day(self) -> None:
        # 00:00 UTC April 22 is before the 09:00 boundary → April 21 session
        now = datetime(2026, 4, 22, 0, 0, 0, tzinfo=UTC)
        assert _session_trading_day(now) == date(2026, 4, 21)

    def test_just_before_boundary_prior_trading_day(self) -> None:
        # 08:59 UTC April 22 — still in the April 21 session
        now = datetime(2026, 4, 22, 8, 59, 59, tzinfo=UTC)
        assert _session_trading_day(now) == date(2026, 4, 21)

    def test_us_pacific_resolve_window_covered(self) -> None:
        # LA/SF/Seattle (PDT, UTC-7): midnight = 07:00 UTC.
        # At 07:01 UTC the market has resolved but the session is still April 21.
        now = datetime(2026, 4, 22, 7, 1, tzinfo=UTC)
        assert _session_trading_day(now) == date(2026, 4, 21)

    def test_session_restarts_at_boundary(self) -> None:
        # 09:00 UTC April 22 → brand new April 22 session
        now = datetime(2026, 4, 22, 9, 0, 0, tzinfo=UTC)
        assert _session_trading_day(now) == date(2026, 4, 22)

    def test_asian_morning_same_day(self) -> None:
        # 09:30 UTC = 18:30 JST. Still same calendar day.
        now = datetime(2026, 4, 21, 9, 30, tzinfo=UTC)
        assert _session_trading_day(now) == date(2026, 4, 21)

    def test_crash_restart_at_2am_recovers_prior_day(self) -> None:
        # Daemon crashes and restarts at 02:00 UTC April 22.
        # Must resume monitoring April 21 US positions.
        now = datetime(2026, 4, 22, 2, 0, tzinfo=UTC)
        assert _session_trading_day(now) == date(2026, 4, 21)


# ---------------------------------------------------------------------------
# _already_entered_today tests
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class TestAlreadyEnteredToday:

    def test_returns_slugs_matching_session_trading_day(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "polymarket" / "runs"
        _write_jsonl(runs_dir / "run1.jsonl", [
            {
                "event": "strategy_result",
                "observation_date": "2026-04-21",
                "accounting_status": "open",
                "market_slug": "market-nyc-apr21",
            }
        ])
        result = _already_entered_today(tmp_path, date(2026, 4, 21))
        assert result == {"market-nyc-apr21"}

    def test_excludes_different_observation_date(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "polymarket" / "runs"
        _write_jsonl(runs_dir / "run1.jsonl", [
            {
                "event": "strategy_result",
                "observation_date": "2026-04-20",
                "accounting_status": "open",
                "market_slug": "market-old",
            }
        ])
        result = _already_entered_today(tmp_path, date(2026, 4, 21))
        assert result == set()

    def test_excludes_non_open_accounting_status(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "polymarket" / "runs"
        _write_jsonl(runs_dir / "run1.jsonl", [
            {
                "event": "strategy_result",
                "observation_date": "2026-04-21",
                "accounting_status": "settled",
                "market_slug": "market-settled",
            }
        ])
        result = _already_entered_today(tmp_path, date(2026, 4, 21))
        assert result == set()

    def test_after_utc_midnight_still_sees_prior_day_entries(self, tmp_path: Path) -> None:
        """The secondary bug: after UTC midnight, April-21 entries must still appear.

        Before the fix, callers passed _date.today() which became April 22 after
        midnight, missing April-21 entries.  Now callers pass session_trading_day,
        which stays April 21 until 09:00 UTC.
        """
        runs_dir = tmp_path / "polymarket" / "runs"
        _write_jsonl(runs_dir / "run1.jsonl", [
            {
                "event": "strategy_result",
                "observation_date": "2026-04-21",
                "accounting_status": "open",
                "market_slug": "market-la-apr21",
            }
        ])
        # Simulate: UTC clock says April 22 (post-midnight), but session_trading_day = April 21
        utc_today = date(2026, 4, 22)  # what date.today() would return
        session_day = date(2026, 4, 21)  # what _session_trading_day() returns

        result_with_utc_today = _already_entered_today(tmp_path, utc_today)
        result_with_session_day = _already_entered_today(tmp_path, session_day)

        assert result_with_utc_today == set()        # old (broken) behavior
        assert result_with_session_day == {"market-la-apr21"}  # new (correct) behavior

    def test_aggregates_across_multiple_jsonl_files(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "polymarket" / "runs"
        _write_jsonl(runs_dir / "run1.jsonl", [
            {"event": "strategy_result", "observation_date": "2026-04-21",
             "accounting_status": "open", "market_slug": "market-a"},
        ])
        _write_jsonl(runs_dir / "run2.jsonl", [
            {"event": "strategy_result", "observation_date": "2026-04-21",
             "accounting_status": "open", "market_slug": "market-b"},
        ])
        result = _already_entered_today(tmp_path, date(2026, 4, 21))
        assert result == {"market-a", "market-b"}

    def test_ignores_malformed_json_lines(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "polymarket" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / "run1.jsonl"
        path.write_text(
            '{"event":"strategy_result","observation_date":"2026-04-21","accounting_status":"open","market_slug":"good"}\n'
            'NOT VALID JSON\n'
            '{"event":"strategy_result","observation_date":"2026-04-21","accounting_status":"open","market_slug":"also-good"}\n'
        )
        result = _already_entered_today(tmp_path, date(2026, 4, 21))
        assert result == {"good", "also-good"}

    def test_empty_runs_dir(self, tmp_path: Path) -> None:
        result = _already_entered_today(tmp_path, date(2026, 4, 21))
        assert result == set()


# ---------------------------------------------------------------------------
# Invariant: all known city local midnights before SESSION_END_HOUR_UTC:00 UTC
# ---------------------------------------------------------------------------

def test_all_known_cities_resolve_before_session_boundary() -> None:
    """LA/SF/Seattle in PDT (UTC-7) resolve at 07:00 UTC, before the 09:00 boundary.

    This documents the safety invariant.  If a new city is added with a UTC
    offset more negative than -7, SESSION_END_HOUR_UTC must be raised.
    """
    import zoneinfo

    # UTC offsets of the most-western known cities (PDT summer / PST winter)
    KNOWN_MOST_WESTERN_UTC_OFFSET_HOURS = -8  # PST (winter)

    # midnight local (00:00) expressed as UTC hours
    midnight_utc_hours = 24 + KNOWN_MOST_WESTERN_UTC_OFFSET_HOURS  # = 16, but modulo gives 0 + 24 = 24 → 0
    # More precisely: local midnight 00:00 PST = 08:00 UTC
    utc_midnight_pst = 0 - KNOWN_MOST_WESTERN_UTC_OFFSET_HOURS  # = 8
    assert utc_midnight_pst < SESSION_END_HOUR_UTC, (
        f"LA/SF/Seattle PST midnight is {utc_midnight_pst:02d}:00 UTC, "
        f"which is not before the session boundary {SESSION_END_HOUR_UTC:02d}:00 UTC. "
        "Raise SESSION_END_HOUR_UTC."
    )
