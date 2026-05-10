#!/usr/bin/env python3
"""
Replay Ogma Polymarket universe JSONL into a deterministic quote backtest.

This is intentionally a data-first harness: it validates the collector stream boundary and
produces repeatable trades from archived JSONL before promoting the same data into a full
Nautilus catalog/engine workflow.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import defaultdict
from collections import deque
from collections.abc import Iterable
from collections.abc import Iterator
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BacktestConfig:
    category: str = "btc_rounds"
    slug_regex: str | None = None
    lookback_seconds: float = 10.0
    entry_move: float = 0.04
    hold_seconds: float = 60.0
    max_spread: float = 0.03
    stake: float = 1.0
    max_events: int = 0
    close_open_on_end: bool = True


@dataclass(frozen=True)
class Quote:
    ts: datetime
    token: str
    slug: str
    category: str
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2.0, 6)

    @property
    def spread(self) -> float:
        return round(self.ask - self.bid, 6)


@dataclass
class Position:
    token: str
    slug: str
    entry_ts: datetime
    entry_price: float
    entry_mid: float
    size: float


@dataclass(frozen=True)
class Trade:
    token: str
    slug: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    hold_seconds: float
    entry_mid: float
    exit_mid: float


@dataclass(frozen=True)
class BacktestResult:
    summary: dict[str, Any]
    trades: list[Trade]


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def iter_jsonl_events(paths: Iterable[str | Path], *, strict: bool = False) -> Iterator[dict[str, Any]]:
    for raw_path in paths:
        path = Path(raw_path)
        with _open_text(path) as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    if strict:
                        raise ValueError(f"invalid JSON in {path}:{line_no}") from None
                    continue
                if isinstance(row, dict):
                    yield row


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0.0 or parsed >= 1.0:
        return None
    return parsed


def quote_from_event(event: dict[str, Any], *, config: BacktestConfig, ts: datetime | None = None) -> Quote | None:
    if event.get("t") not in {"book", "delta"}:
        return None
    category = str(event.get("cat") or "").strip()
    if config.category and category != config.category:
        return None

    slug = str(event.get("slug") or "").strip()
    if config.slug_regex and not re.search(config.slug_regex, slug):
        return None

    token = str(event.get("token") or "").strip()
    if not token:
        return None

    event_ts = ts or _parse_ts(event.get("ts"))
    if event_ts is None:
        return None

    bid = _parse_float(event.get("bb"))
    ask = _parse_float(event.get("ba"))
    if bid is None or ask is None:
        return None
    if bid >= ask:
        return None

    quote = Quote(ts=event_ts, token=token, slug=slug, category=category, bid=bid, ask=ask)
    if quote.spread <= 0.0 or quote.spread > config.max_spread:
        return None
    return quote


def _seconds_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds())


def _summarize(*, events_seen: int, quotes_seen: int, trades: list[Trade], open_positions: int) -> dict[str, Any]:
    pnl = [trade.pnl for trade in trades]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in pnl:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)

    by_slug: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "net_pnl": 0.0})
    for trade in trades:
        cell = by_slug[trade.slug]
        cell["trades"] += 1
        cell["net_pnl"] += trade.pnl

    return {
        "events_seen": events_seen,
        "quotes_seen": quotes_seen,
        "closed_trades": len(trades),
        "open_positions": open_positions,
        "net_pnl": round(sum(pnl), 6),
        "avg_pnl": round(sum(pnl) / len(pnl), 6) if pnl else 0.0,
        "win_rate": round(len(wins) / len(pnl), 6) if pnl else 0.0,
        "gross_profit": round(sum(wins), 6),
        "gross_loss": round(sum(losses), 6),
        "max_drawdown": round(max_drawdown, 6),
        "by_slug": {
            slug: {"trades": cell["trades"], "net_pnl": round(cell["net_pnl"], 6)}
            for slug, cell in sorted(by_slug.items())
        },
    }


def _close_trade(position: Position, quote: Quote) -> Trade:
    pnl = (quote.bid - position.entry_price) * position.size
    return Trade(
        token=position.token,
        slug=position.slug,
        entry_ts=position.entry_ts.isoformat(),
        exit_ts=quote.ts.isoformat(),
        entry_price=round(position.entry_price, 6),
        exit_price=round(quote.bid, 6),
        size=round(position.size, 6),
        pnl=round(pnl, 6),
        hold_seconds=round(_seconds_between(position.entry_ts, quote.ts), 6),
        entry_mid=round(position.entry_mid, 6),
        exit_mid=round(quote.mid, 6),
    )


def run_backtest(events: Iterable[dict[str, Any]], config: BacktestConfig) -> BacktestResult:  # noqa: C901
    history: dict[str, deque[Quote]] = defaultdict(deque)
    positions: dict[str, Position] = {}
    last_quote: dict[str, Quote] = {}
    trades: list[Trade] = []
    events_seen = 0
    quotes_seen = 0

    for event in events:
        if config.max_events > 0 and events_seen >= config.max_events:
            break
        events_seen += 1

        quote = quote_from_event(event, config=config)
        if quote is None:
            continue

        quotes_seen += 1
        last_quote[quote.token] = quote
        token_history = history[quote.token]
        token_history.append(quote)
        lookback_cutoff = quote.ts.timestamp() - float(config.lookback_seconds)
        while token_history and token_history[0].ts.timestamp() < lookback_cutoff:
            token_history.popleft()

        position = positions.get(quote.token)
        if position is not None:
            if _seconds_between(position.entry_ts, quote.ts) >= config.hold_seconds:
                trades.append(_close_trade(position, quote))
                positions.pop(quote.token, None)
            continue

        if len(token_history) < 2:
            continue
        reference = token_history[0]
        if quote.mid - reference.mid < config.entry_move:
            continue

        size = float(config.stake)
        positions[quote.token] = Position(
            token=quote.token,
            slug=quote.slug,
            entry_ts=quote.ts,
            entry_price=quote.ask,
            entry_mid=quote.mid,
            size=size,
        )

    if config.close_open_on_end:
        for token, position in list(positions.items()):
            quote = last_quote.get(token)
            if quote is None or quote.ts <= position.entry_ts:
                continue
            trades.append(_close_trade(position, quote))
            positions.pop(token, None)

    summary = _summarize(
        events_seen=events_seen,
        quotes_seen=quotes_seen,
        trades=trades,
        open_positions=len(positions),
    )
    return BacktestResult(summary=summary, trades=trades)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay Ogma Polymarket universe JSONL and run a deterministic quote backtest.",
    )
    parser.add_argument("paths", nargs="+", help="Input .jsonl, .jsonl.gz, or .open files.")
    parser.add_argument("--category", default="btc_rounds")
    parser.add_argument("--slug-regex", default=None)
    parser.add_argument("--lookback-seconds", type=float, default=10.0)
    parser.add_argument("--entry-move", type=float, default=0.04)
    parser.add_argument("--hold-seconds", type=float, default=60.0)
    parser.add_argument("--max-spread", type=float, default=0.03)
    parser.add_argument("--stake", type=float, default=1.0)
    parser.add_argument("--max-events", type=int, default=0)
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--trades-out", default=None)
    parser.add_argument("--strict-json", action="store_true")
    return parser


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = BacktestConfig(
        category=args.category,
        slug_regex=args.slug_regex,
        lookback_seconds=args.lookback_seconds,
        entry_move=args.entry_move,
        hold_seconds=args.hold_seconds,
        max_spread=args.max_spread,
        stake=args.stake,
        max_events=args.max_events,
    )
    result = run_backtest(iter_jsonl_events(args.paths, strict=args.strict_json), config)
    payload = {
        "config": asdict(config),
        "summary": result.summary,
        "trades": [asdict(trade) for trade in result.trades],
    }

    if args.json_out:
        _write_json(Path(args.json_out), payload)
    if args.trades_out:
        _write_json(Path(args.trades_out), payload["trades"])

    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
