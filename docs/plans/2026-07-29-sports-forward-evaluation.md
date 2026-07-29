# Sports Forward Evaluation Implementation Plan

**Date:** 2026-07-29
**Status:** Registered before the first complete observation
**Scope:** Atlas research processing and Nautilus sandbox-paper gating only

## Success criteria

1. Read plain or gzip `sports_forward_observation.v1` rows without changing
   the OGMA archive.
2. Enrich rows from exact CLOB `/markets/{condition_id}` winner-token records.
3. Exclude duplicate, late, stale, incomplete, malformed, or unresolved rows
   with explicit reasons.
4. Reproduce five-share entry P/L using the captured walked ask and the
   captured CLOB fee parameters, rounded to five decimal USDC places.
5. Report the registered 30-calendar-day and 300-resolved-row gates, including
   the ATP/WTA, checkpoint, week, tournament, and concentration requirements.
6. Produce no order, execution-client, or live-capital behavior.

## Registered candidate rule

The primary candidate is fixed before eligible data exists:

- ATP and WTA singles moneylines only;
- checkpoints `t_minus_60m` and `t_minus_15m`;
- exactly five shares;
- one candidate token per condition and checkpoint, chosen by the highest
  fee-adjusted expected edge;
- fee-adjusted expected edge =
  `consensus_probability - walked_ask - entry_fee_per_share`;
- entry only when that edge is at least `0.05`;
- walked ask must be in `[0.50, 0.981)`;
- bid/ask spread must be no more than `0.02`;
- every registered completeness rule must pass.

Ties are broken by token ID. Thresholds are diagnostics only until a later
protocol is registered; the primary rule above will not be changed after the
first complete row.

## Fee and settlement accounting

For captured share quantity `C`, walked price `p`, rate `r`, and exponent `e`:

`entry_fee = round(C * r * (p * (1 - p)) ** e, 5)`

The current sports curve has `e=1`, which is the documented
`C * feeRate * p * (1-p)` formula. The exponent remains captured and applied
so historical rows retain the market-specific curve. Settlement is one USDC
per winning share and zero for the losing token. No redemption fee is assumed.

## Tasks and verification

1. Add pure observation eligibility and fee functions.
   Verify with malformed, stale, late, and fee-rounding unit tests.
2. Add exact CLOB resolution records and derived enrichment.
   Verify token identity wins even when labels differ and ambiguous evidence
   remains unresolved.
3. Add deterministic candidate selection and grouped evaluation.
   Verify one token per condition/checkpoint and exact gross/fee/net P/L.
4. Add a CLI for `resolve-once`, `status`, and `evaluate`.
   Verify JSON output and append-only resolution behavior.
5. Deploy the merged SHA on Atlas and schedule read-only monitoring.
   Verify mirror health plus a dry run against the current incomplete rows.

The strategy-freeze decision remains blocked until both the elapsed-time and
resolved-sample gates pass.
