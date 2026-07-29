# Polymarket Sports Forward-Observation Contract

`sports_forward_observation.v1` is the append-only data contract for the next
sports research stage. Each row describes one outcome token at one
pre-registered checkpoint. It is an observation, not an order or permission to
trade.

The implementation lives in
`examples/live/polymarket/sports_observation_schema.py`. It records:

- stable event, condition, token, and outcome identifiers;
- sport, competition, start time, market type, and named resolution source;
- checkpoint and intended decision time;
- Polymarket quote time, best bid and ask with sizes, walked ask, tick size,
  minimum order size, and the venue fee rate;
- raw bookmaker quotes and the de-vigged consensus probability;
- the age of every data source and explicit missing-data reasons; and
- eventual CLOB winner evidence paired with the named-source outcome.

## Checkpoint semantics

Checkpoint names such as `t_minus_60m` must be fixed before the forward window
starts. `intended_decision_time` is the time at which a hypothetical strategy
would have made its decision. `venue_timestamp` and every bookmaker
`observed_at` value identify the actual source samples available at that time.
All timestamps are UTC ISO-8601 strings.

Collectors should append at most one row per
`(condition_id, token_id, checkpoint)`. Corrections are new rows in a separate,
versioned repair process; historical observation rows are not overwritten.

## Missing-data semantics

Absent venue depth, walked price, bookmaker quotes, or consensus probability
must be represented by `null` or an empty quote list and at least one specific
`missing_data_reasons` value. Examples include
`bookmaker_quotes_unavailable`, `venue_book_stale`, and
`outcome_match_ambiguous`. Downstream CLV-confirmed strategies must fail closed
when required data is missing or stale.

`source_freshness` stores age in seconds at the intended decision time. A
source whose age cannot be established uses `age_seconds=null` and a matching
missing-data reason.

## Resolution and research boundary

Before resolution, both `clob_winner_evidence` and `named_source_outcome` are
`null`. CLOB settlement is authoritative and comes from
`GET /markets/{condition_id}`. A resolution is accepted only when the response
has the requested condition ID, `closed=true`, and exactly one token with
`winner=true`. The recorded winner is identified by its exact token ID; outcome
labels are retained only as evidence and are never used to determine win or
loss.

Network errors, malformed payloads, condition mismatches, open markets, zero or
multiple winners, and observations without the held token ID remain
unresolved. No price or outcome-name fallback is permitted.

`named_source_outcome` is independent reconciliation evidence. It is populated
when the registered ATP or WTA source becomes available and is compared with
the CLOB winner in derived research data; it does not override CLOB settlement.

This schema adds no network collection, trading, deployment, or live-order
behavior. Development and validation dates must be registered before analysis,
with a separate untouched forward window reserved for evaluation.
