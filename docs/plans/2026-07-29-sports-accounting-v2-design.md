# Sports Accounting V2 Design

**Date:** 2026-07-29
**Status:** Approved for implementation
**Scope:** Historical simulation and sandbox paper infrastructure only

## Objective

Make the Polymarket sports paper results trustworthy enough to support a new
forward tennis-moneyline evaluation. This change fixes measurement defects; it
does not restart services, select a profitable strategy, or authorize live
execution.

## Design

The existing runtime creates one Nautilus strategy for every
market-and-preset pair, but result extraction ignores the actual strategy ID.
It finds a position by instrument and then credits every compatible preset.
V2 will key positions by `(instrument_id, strategy_id)` using the runtime's
post-registration `StrategyId`. A position can therefore produce exactly one
strategy-result row. Rows gain stable `position_id` and `strategy_id` fields so
later reporting can prove uniqueness rather than infer it from price bands.

Settlement rows will distinguish gross P&L, entry fee, and net P&L. The
settlement calculation will use an explicitly supplied fee rather than silently
assuming zero. New rows must carry the actual entry commission extracted from
the Nautilus position when available. Legacy rows without a fee remain readable
but are marked `fee_status=missing`; their P&L is not silently relabelled net.
The report will aggregate `net_pnl` for v2 rows and clearly count rows whose fee
is missing.

External bookmaker confirmation will become fail closed. A CLV-gated preset
cannot enter when the odds API key, event match, or implied probability is
missing. This prevents a nominally confirmed strategy from degenerating into
the old price-band strategy during data outages.

## Forward observation contract

The next research stage will use append-only observations, one row per
market/checkpoint:

- stable event, condition, token, and outcome identifiers;
- sport, competition, start time, market type, and authoritative resolution
  source;
- Polymarket venue timestamp, bid, ask, sizes, walked ask, tick, minimum size,
  and fee schedule;
- raw bookmaker quotes plus a de-vigged consensus probability;
- fixed checkpoint name and intended decision timestamp;
- data-source freshness and explicit missing-data reasons;
- eventual CLOB winner evidence and named-source outcome.

No observation row is a trade. A later frozen strategy may consume the archive
only after development/validation dates and an untouched forward window are
registered.

## Error handling and safety

- Ambiguous strategy attribution produces no credited fill.
- Missing fees remain explicit and exclude a row from fee-net claims.
- Missing bookmaker data blocks CLV-confirmed entries.
- Settlement authority migration is documented but not changed in this slice;
  replacing Gamma settlement requires its own fixtures and CLOB evidence tests.
- No Compose, credentials, remote services, collectors, or live execution
  settings change.

## Verification

Focused unit tests must prove:

1. one position is credited only to its actual strategy;
2. two strategies on one instrument remain distinguishable;
3. gross, fee, and net settlement arithmetic reconcile exactly;
4. missing fees are visible in reports;
5. CLV gating fails closed;
6. all existing sports tests remain green in the compiled Nautilus image.
