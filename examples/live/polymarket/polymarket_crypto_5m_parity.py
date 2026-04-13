#!/usr/bin/env python3
"""
Replay-based parity harness for Polymarket 5-minute strategy decisions.

The reference evaluator mirrors the current `quant` first-wave preset semantics
in a deterministic, dependency-light form so the Nautilus test environment can
verify parity without mounting sibling repositories.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
import importlib.util
from pathlib import Path
import sys
from typing import Any
from typing import Literal

try:
    from examples.live.polymarket.crypto_5m_strategy_library import FlowImbalanceTracker
    from examples.live.polymarket.crypto_5m_strategy_library import PolymarketCrypto5mSignalEngine
    from examples.live.polymarket.crypto_5m_strategy_library import PolymarketCrypto5mStrategyPreset
    from examples.live.polymarket.crypto_5m_strategy_library import effective_stop_loss_price
    from examples.live.polymarket.crypto_5m_strategy_library import first_wave_strategy_presets
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.crypto_5m_strategy_library"
    module_path = Path(__file__).resolve().with_name("crypto_5m_strategy_library.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    FlowImbalanceTracker = module.FlowImbalanceTracker
    PolymarketCrypto5mSignalEngine = module.PolymarketCrypto5mSignalEngine
    PolymarketCrypto5mStrategyPreset = module.PolymarketCrypto5mStrategyPreset
    effective_stop_loss_price = module.effective_stop_loss_price
    first_wave_strategy_presets = module.first_wave_strategy_presets


@dataclass(frozen=True)
class QuoteSnapshot:
    best_bid: float
    best_ask: float
    best_bid_size: float
    best_ask_size: float


@dataclass(frozen=True)
class ReplaySnapshot:
    timestamp: datetime
    quotes: dict[str, QuoteSnapshot]
    reference_mid_price: float | None = None


@dataclass(frozen=True)
class ReplaySession:
    asset: str
    market_slug: str
    round_start: datetime
    market_end: datetime
    snapshots: list[ReplaySnapshot]
    winning_side: Literal["up", "down"]


@dataclass(frozen=True)
class ParityThresholds:
    entry_match_rate: float = 1.0
    side_match_rate: float = 1.0
    timing_bucket_match_rate: float = 1.0
    exit_reason_match_rate: float = 1.0
    outcome_accounting_match_rate: float = 1.0


def _infer_token_sides(session: ReplaySession) -> dict[str, Literal["up", "down"]]:
    first_snapshot = session.snapshots[0]
    mapping: dict[str, Literal["up", "down"]] = {}
    for token_id in first_snapshot.quotes:
        lower = token_id.lower()
        mapping[token_id] = "down" if "down" in lower or lower.startswith("no") else "up"
    return mapping


def _position_label(side: Literal["up", "down"]) -> str:
    return "YES" if side == "up" else "NO"


def _timing_bucket(entry_time: datetime | None, round_start: datetime) -> int | None:
    if entry_time is None:
        return None
    elapsed = max(0.0, (entry_time - round_start).total_seconds())
    return int(elapsed // 60)


def _update_position(
    *,
    position: dict[str, Any],
    quote: QuoteSnapshot,
    exit_price: float,
    stop_loss_price: float | None,
    now: datetime,
) -> str | None:
    best_bid = float(quote.best_bid)
    position["min_bid_seen"] = min(float(position.get("min_bid_seen", best_bid)), best_bid)
    position["max_bid_seen"] = max(float(position.get("max_bid_seen", best_bid)), best_bid)
    if stop_loss_price is not None and best_bid <= float(stop_loss_price):
        position["status"] = "closed"
        position["close_time"] = now
        position["close_reason"] = "stop_loss_exit"
        position["profit"] = round(float(position["shares"]) * best_bid - float(position["stake_usd"]), 10)
        return "stop_loss_exit"
    if best_bid >= float(exit_price):
        position["status"] = "closed"
        position["close_time"] = now
        position["close_reason"] = "target_exit"
        position["profit"] = round(float(position["shares"]) * float(exit_price) - float(position["stake_usd"]), 10)
        return "target_exit"
    return None


def _resolve_position(*, position: dict[str, Any], winning_side: Literal["up", "down"], now: datetime) -> str:
    won = str(position["side"]) == winning_side
    proceeds = float(position["shares"]) if won else 0.0
    position["status"] = "closed"
    position["close_time"] = now
    position["close_reason"] = "settled_win" if won else "settled_loss"
    position["profit"] = round(proceeds - float(position["stake_usd"]), 10)
    return str(position["close_reason"])


def _candidate_decision(
    *,
    preset: PolymarketCrypto5mStrategyPreset,
    session: ReplaySession,
    token_sides: dict[str, Literal["up", "down"]],
) -> dict[str, Any]:
    engine = PolymarketCrypto5mSignalEngine(preset=preset, token_sides=token_sides)
    position: dict[str, Any] | None = None
    outcome: str | None = None

    for snapshot in session.snapshots:
        for token_id, quote in snapshot.quotes.items():
            engine.record_top_of_book(
                token_id=token_id,
                best_bid=quote.best_bid,
                best_ask=quote.best_ask,
                best_bid_size=quote.best_bid_size,
                best_ask_size=quote.best_ask_size,
                now=snapshot.timestamp,
            )
        if snapshot.reference_mid_price is not None:
            engine.record_reference_mid_price(mid_price=snapshot.reference_mid_price, now=snapshot.timestamp)

        if position is None:
            signal = engine.entry_signal(now=snapshot.timestamp, market_end=session.market_end)
            if signal is not None:
                position = {
                    "token_id": signal.token_id,
                    "side": signal.side,
                    "entry_time": snapshot.timestamp,
                    "entry_price": float(signal.ask_price),
                    "stake_usd": float(signal.ask_price),
                    "shares": 1.0,
                    "status": "open",
                    "min_bid_seen": float(snapshot.quotes[signal.token_id].best_bid),
                    "max_bid_seen": float(snapshot.quotes[signal.token_id].best_bid),
                }
            continue

        if position["status"] == "open":
            stop_loss = effective_stop_loss_price(
                preset=preset,
                entry_price=float(position["entry_price"]),
                max_bid_seen=float(position["max_bid_seen"]),
            )
            quote = snapshot.quotes[str(position["token_id"])]
            outcome = _update_position(
                position=position,
                quote=quote,
                exit_price=float(preset.exit_price),
                stop_loss_price=stop_loss,
                now=snapshot.timestamp,
            )

    if position is not None and position["status"] == "open":
        outcome = _resolve_position(position=position, winning_side=session.winning_side, now=session.market_end)

    return {
        "entered": position is not None,
        "side": None if position is None else str(position["side"]),
        "entry_timing_bucket": _timing_bucket(None if position is None else position["entry_time"], session.round_start),
        "exit_reason": outcome,
        "profit": None if position is None else round(float(position.get("profit") or 0.0), 10),
    }


def _refresh_threshold_tracking(
    *,
    state: dict[str, dict[str, Any]],
    now: datetime,
    entry_price: float,
    exit_price: float,
) -> None:
    for row in state.values():
        ask = row.get("best_ask")
        if ask is None:
            row["threshold_since"] = None
            continue
        in_threshold = float(ask) >= float(entry_price) and float(ask) < float(exit_price) and float(ask) < 1.0
        if in_threshold:
            row["threshold_since"] = row.get("threshold_since") or now
        else:
            row["threshold_since"] = None


def _maybe_enter_position(
    *,
    state: dict[str, dict[str, Any]],
    preset: PolymarketCrypto5mStrategyPreset,
    now: datetime,
    session: ReplaySession,
) -> dict[str, Any] | None:
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for token_id, row in state.items():
        ask = row.get("best_ask")
        bid = row.get("best_bid")
        if ask is None or bid is None:
            continue
        if float(ask) < float(preset.entry_price) or float(ask) >= float(preset.exit_price) or float(ask) >= 1.0:
            continue
        if (now - session.round_start).total_seconds() < float(preset.min_seconds_after_open):
            continue
        if (session.market_end - now).total_seconds() <= float(preset.min_seconds_before_close):
            continue
        if (float(ask) - float(bid)) > float(preset.max_spread):
            continue
        if float(preset.min_supported_bid_price) > 0 and float(bid) < float(preset.min_supported_bid_price):
            continue
        if float(preset.min_best_bid_size) > 0 and float(row.get("best_bid_size") or 0.0) < float(preset.min_best_bid_size):
            continue
        threshold_since = row.get("threshold_since")
        if threshold_since is None:
            continue
        if (now - threshold_since).total_seconds() < float(preset.min_threshold_seconds):
            continue
        candidates.append((float(ask), token_id, row))

    if not candidates:
        return None

    ask, token_id, row = min(candidates, key=lambda item: (item[0], item[1]))
    return {
        "token_id": token_id,
        "side": "up" if row["label"] == "YES" else "down",
        "label": row["label"],
        "entry_time": now,
        "entry_price": ask,
        "stake_usd": ask,
        "shares": 1.0,
        "status": "open",
        "min_bid_seen": float(row["best_bid"]),
        "max_bid_seen": float(row["best_bid"]),
    }


def _reference_gate(
    *,
    preset: PolymarketCrypto5mStrategyPreset,
    position: dict[str, Any] | None,
    token_row: dict[str, Any] | None,
    flow_trackers: dict[str, FlowImbalanceTracker] | None,
    now: datetime,
) -> bool:
    if position is None or token_row is None:
        return False
    bid = token_row.get("best_bid")
    ask = token_row.get("best_ask")
    bid_size = token_row.get("best_bid_size")
    ask_size = token_row.get("best_ask_size")
    if preset.mode == "basic":
        return True
    if preset.mode == "microprice":
        if bid is None or ask is None or bid_size is None or ask_size is None:
            return False
        total = float(bid_size) + float(ask_size)
        if total < float(preset.min_total_top_size):
            return False
        midpoint = (float(ask) + float(bid)) / 2.0
        imbalance = (float(bid_size) - float(ask_size)) / total
        micro = midpoint + 0.5 * imbalance * (float(ask) - float(bid))
        return micro >= float(ask) - float(preset.microprice_epsilon)
    if preset.mode == "support_ratio":
        if bid_size is None or ask_size is None:
            return False
        total = float(bid_size) + float(ask_size)
        if total < float(preset.min_total_top_size):
            return False
        return (float(bid_size) / max(1e-9, float(ask_size))) >= float(preset.support_ratio)
    if preset.mode == "quote_stability":
        stable_since = token_row.get("stable_since")
        return stable_since is not None and (now - stable_since).total_seconds() >= float(preset.stability_seconds)
    if preset.mode == "flow_imbalance":
        if flow_trackers is None:
            return False
        tracker = flow_trackers.get(str(position["token_id"]))
        if tracker is None:
            return False
        signal = tracker.signal(now=now.timestamp())
        if not signal.sufficient:
            return False
        if position["label"] == "YES":
            return signal.imbalance >= float(preset.flow_min_imbalance)
        return signal.imbalance <= -float(preset.flow_min_imbalance)
    return True


def _reference_decision(
    *,
    preset: PolymarketCrypto5mStrategyPreset,
    session: ReplaySession,
    token_sides: dict[str, Literal["up", "down"]],
) -> dict[str, Any]:
    state = {
        token_id: {
            "label": _position_label(side),
            "best_bid": None,
            "best_ask": None,
            "best_bid_size": None,
            "best_ask_size": None,
            "threshold_since": None,
            "stable_since": None,
            "last_bid": None,
            "last_ask": None,
        }
        for token_id, side in token_sides.items()
    }
    flow_trackers = (
        {
            token_id: FlowImbalanceTracker(
                window_seconds=float(preset.flow_window_seconds),
                min_samples=int(preset.flow_min_samples),
            )
            for token_id in token_sides
        }
        if preset.mode == "flow_imbalance"
        else None
    )
    position: dict[str, Any] | None = None
    outcome: str | None = None

    for snapshot in session.snapshots:
        for token_id, quote in snapshot.quotes.items():
            row = state[token_id]
            row["best_bid"] = float(quote.best_bid)
            row["best_ask"] = float(quote.best_ask)
            row["best_bid_size"] = float(quote.best_bid_size)
            row["best_ask_size"] = float(quote.best_ask_size)

        _refresh_threshold_tracking(
            state=state,
            now=snapshot.timestamp,
            entry_price=float(preset.entry_price),
            exit_price=float(preset.exit_price),
        )

        for row in state.values():
            bid = row.get("best_bid")
            ask = row.get("best_ask")
            if row.get("stable_since") is None:
                row["stable_since"] = snapshot.timestamp
            if bid is not None and row.get("last_bid") is not None and float(bid) < float(row["last_bid"]):
                row["stable_since"] = snapshot.timestamp
            if ask is not None and row.get("last_ask") is not None and float(ask) > float(row["last_ask"]):
                row["stable_since"] = snapshot.timestamp
            row["last_bid"] = bid
            row["last_ask"] = ask

        if flow_trackers is not None:
            for token_id, quote in snapshot.quotes.items():
                flow_trackers[token_id].add_sample(
                    bid_size=float(quote.best_bid_size),
                    ask_size=float(quote.best_ask_size),
                    timestamp=snapshot.timestamp.timestamp(),
                )

        if position is None:
            position = _maybe_enter_position(state=state, preset=preset, now=snapshot.timestamp, session=session)
            if position is not None:
                token_row = state.get(str(position["token_id"]))
                if not _reference_gate(
                    preset=preset,
                    position=position,
                    token_row=token_row,
                    flow_trackers=flow_trackers,
                    now=snapshot.timestamp,
                ):
                    position = None
            continue

        if position["status"] == "open":
            quote = snapshot.quotes[str(position["token_id"])]
            stop_loss = effective_stop_loss_price(
                preset=preset,
                entry_price=float(position["entry_price"]),
                max_bid_seen=float(position["max_bid_seen"]),
            )
            outcome = _update_position(
                position=position,
                quote=quote,
                exit_price=float(preset.exit_price),
                stop_loss_price=stop_loss,
                now=snapshot.timestamp,
            )

    if position is not None and position["status"] == "open":
        outcome = _resolve_position(position=position, winning_side=session.winning_side, now=session.market_end)

    return {
        "entered": position is not None,
        "side": None if position is None else str(position["side"]),
        "entry_timing_bucket": _timing_bucket(None if position is None else position["entry_time"], session.round_start),
        "exit_reason": outcome,
        "profit": None if position is None else round(float(position.get("profit") or 0.0), 10),
    }


def _comparison(reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, bool]:
    entered_match = bool(reference["entered"]) == bool(candidate["entered"])
    if not reference["entered"] and not candidate["entered"]:
        return {
            "entry_match": entered_match,
            "side_match": True,
            "timing_bucket_match": True,
            "exit_reason_match": True,
            "outcome_accounting_match": True,
        }
    return {
        "entry_match": entered_match,
        "side_match": reference["side"] == candidate["side"],
        "timing_bucket_match": reference["entry_timing_bucket"] == candidate["entry_timing_bucket"],
        "exit_reason_match": reference["exit_reason"] == candidate["exit_reason"],
        "outcome_accounting_match": reference["profit"] == candidate["profit"],
    }


def replay_session_parity(
    session: ReplaySession,
    *,
    presets: tuple[PolymarketCrypto5mStrategyPreset, ...] | None = None,
) -> dict[str, Any]:
    token_sides = _infer_token_sides(session)
    selected = presets or first_wave_strategy_presets()
    decisions: list[dict[str, Any]] = []
    summary = {
        "total_presets": len(selected),
        "matched_entry": 0,
        "matched_side": 0,
        "matched_timing_bucket": 0,
        "matched_exit_reason": 0,
        "matched_outcome_accounting": 0,
    }

    for preset in selected:
        reference = _reference_decision(preset=preset, session=session, token_sides=token_sides)
        candidate = _candidate_decision(preset=preset, session=session, token_sides=token_sides)
        comparison = _comparison(reference, candidate)
        summary["matched_entry"] += int(comparison["entry_match"])
        summary["matched_side"] += int(comparison["side_match"])
        summary["matched_timing_bucket"] += int(comparison["timing_bucket_match"])
        summary["matched_exit_reason"] += int(comparison["exit_reason_match"])
        summary["matched_outcome_accounting"] += int(comparison["outcome_accounting_match"])
        decisions.append(
            {
                "strategy_name": preset.name,
                "reference": reference,
                "candidate": candidate,
                "comparison": comparison,
            },
        )

    return {
        "asset": session.asset,
        "market_slug": session.market_slug,
        "summary": summary,
        "decisions": decisions,
    }


def assess_parity(*, summary: dict[str, Any], thresholds: ParityThresholds) -> dict[str, Any]:
    total = max(1, int(summary["total_presets"]))
    metrics = {
        "entry_match_rate": {
            "actual": summary["matched_entry"] / total,
            "threshold": thresholds.entry_match_rate,
        },
        "side_match_rate": {
            "actual": summary["matched_side"] / total,
            "threshold": thresholds.side_match_rate,
        },
        "timing_bucket_match_rate": {
            "actual": summary["matched_timing_bucket"] / total,
            "threshold": thresholds.timing_bucket_match_rate,
        },
        "exit_reason_match_rate": {
            "actual": summary["matched_exit_reason"] / total,
            "threshold": thresholds.exit_reason_match_rate,
        },
        "outcome_accounting_match_rate": {
            "actual": summary["matched_outcome_accounting"] / total,
            "threshold": thresholds.outcome_accounting_match_rate,
        },
    }
    for row in metrics.values():
        row["passed"] = float(row["actual"]) >= float(row["threshold"])
    return {"passed": all(row["passed"] for row in metrics.values()), "metrics": metrics}
