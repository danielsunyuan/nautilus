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
`null`. Once resolved, they must be populated together so CLOB settlement
evidence can be checked against the named authoritative source.

This schema adds no network collection, trading, deployment, or live-order
behavior. Development and validation dates must be registered before analysis,
with a separate untouched forward window reserved for evaluation.
