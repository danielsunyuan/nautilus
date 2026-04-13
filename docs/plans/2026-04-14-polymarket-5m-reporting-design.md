# Polymarket 5m Reporting Design

## Objective

Add Nautilus-native reporting for Polymarket 5-minute paper trading with a
machine-readable summary as the source of truth and Markdown as a rendered view.

The reporting system should:

- read daemon JSONL outputs from `outputs/polymarket/runs/`,
- build a canonical JSON summary suitable for Notion or frontend consumption,
- render a Markdown report equivalent in shape to the current `RESULTS.md`,
- remain honest about provisional fields where daemon output has not yet reached
  full `quant` parity.

## Recommended Approach

Use a two-stage reporting pipeline:

1. aggregate raw JSONL run files into a canonical summary JSON document,
2. render Markdown from the summary JSON document.

This keeps business logic out of presentation formatting and gives downstream
consumers one stable contract.

## Scope Boundaries

Leg 5 is reporting only.

Included:

- summary JSON generation,
- Markdown rendering from summary JSON,
- file/session inventory,
- strategy leaderboard and totals,
- explicit completeness metadata and caveats,
- tests for aggregation and rendering behavior.

Excluded:

- redesigning Leg 4 run output semantics beyond thin interpretation adapters,
- changing recorder or catalog storage,
- frontend, Notion, or API delivery layers,
- live reporting services or dashboards,
- parity-harness logic from Leg 7.

## Reporting Architecture

The reporting entrypoint should live under `examples/live/polymarket/` and scan
`outputs/polymarket/runs/` for daemon JSONL files. It should parse lifecycle
events and strategy result rows, then construct a canonical summary object with
stable machine-readable keys.

Markdown rendering should be a separate step operating on that summary object,
not on the raw JSONL files. This keeps aggregation logic centralized and makes
the JSON summary suitable for future syncs into Notion or a custom frontend.

## Output Files

Canonical machine-readable outputs:

- `outputs/polymarket/reports/summary_latest.json`
- `outputs/polymarket/reports/summary_{timestamp}.json`

Rendered Markdown output:

- `outputs/polymarket/reports/RESULTS.md`

The summary JSON should include:

- `schema_version`
- `generated_at`
- `report_info`
- `sessions`
- `leaderboard`
- `totals`
- `notes`
- `data_quality`

## Canonical Data Model

The JSON summary should preserve numeric values as numbers, not presentation
strings. Dollar signs and percentages belong only in rendered Markdown.

Each leaderboard row should contain stable keys such as:

- `rank`
- `loop`
- `strategy_name`
- `rounds`
- `trades`
- `wins`
- `losses`
- `no_trade`
- `target_exits`
- `stop_losses`
- `settled_wins`
- `settled_losses`
- `win_rate`
- `avg_entry_price`
- `net_pnl`
- `roi`

The `sessions` section should include the contributing files and basic counts
per source file so downstream systems can trace provenance.

The `data_quality` or `notes` section must explicitly mark which metrics are
exact and which are provisional based on the current daemon schema.

## Aggregation Semantics

Aggregation should follow the intent of `quant/scripts/generate_results_md.py`
where current Nautilus data can support it.

Rules:

- `rounds` should mean rounds represented by the included source files for the
  loop, consistent with the current `RESULTS.md` explanation.
- `round_skipped` and `error` events are diagnostics, not trades.
- `trades`, `wins`, `losses`, `target_exits`, `stop_losses`, `settled_wins`,
  and `settled_losses` should only be counted from fields that exist in the
  daemon result rows.
- Missing or provisional values must be surfaced explicitly instead of guessed.
- `avg_entry_price` should be computed only from actual entries.
- `net_pnl` and `roi` should remain numeric and deterministic.
- Leaderboard ordering should sort by `net_pnl` descending, then `roi`
  descending, then `strategy_name`.

## Testing Plan

Required coverage:

- report file discovery and source-file inventory,
- stable grouping by loop or preset set,
- leaderboard ordering,
- totals aggregation,
- separation of numeric JSON values from formatted Markdown values,
- explicit handling of incomplete/provisional daemon metrics,
- rendering shape for the Markdown output.

Test location:

- `tests/unit_tests/examples/test_polymarket_crypto_5m_reporting.py`

## Proposed Implementation Surface

- `examples/live/polymarket/polymarket_crypto_5m_reporting.py`
- optional helper functions in the same module unless growth justifies a split
- `tests/unit_tests/examples/test_polymarket_crypto_5m_reporting.py`

## Acceptance Fit

This design fits TASK-007 in `TODO.md`:

- Nautilus-native reporting,
- machine-readable source of truth,
- Markdown parity view,
- leaderboard and totals,
- explicit handling of stop-loss and settlement accounting where available.

It leaves frontend, Notion integration, and full behavioral parity expansion to
later legs.
