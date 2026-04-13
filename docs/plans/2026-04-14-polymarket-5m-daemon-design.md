# Polymarket 5m BTC Paper Daemon Design

## Objective

Add a long-running Nautilus paper trading daemon for BTC 5-minute Polymarket markets that:

- rolls from one market to the next until stopped,
- reuses the existing 5m session resolver and strategy library,
- writes run results to JSONL under `outputs/polymarket/runs/`,
- leaves time-series quote persistence to the existing recorder and catalog path,
- stays narrow to Leg 4 scope without expanding strategy semantics or reporting logic.

This design covers phase 1 paper execution only. Live execution remains out of scope.

## Recommended Approach

Use a single-process orchestrator entrypoint.

The daemon runs one long-lived Python process that owns the 5-minute round loop. It resolves
the active BTC market, launches a per-round paper execution flow using existing Nautilus-side
components, persists JSONL result records, waits for the next 5-minute boundary, and repeats.

This approach is preferred over a supervisor-plus-workers model or one-container-per-strategy
deployment because it keeps lifecycle control, logging, shutdown, and result persistence simple.
It also matches Nautilus guidance that one live trading node should run per process.

## Scope Boundaries

Leg 4 is orchestration only.

Included:

- daemon entrypoint and orchestration helpers,
- BTC-only round rollover,
- JSONL run persistence,
- session-boundary logging,
- graceful stop and restart behavior,
- compose wiring for a long-running daemon service,
- tests for lifecycle and persistence behavior.

Excluded:

- new strategy semantics,
- changes to the quote recorder or catalog storage model,
- reporting aggregation or `RESULTS.md` generation,
- multi-asset execution,
- live trading or latency optimization work.

## Runtime Architecture

The daemon is a long-running Python entrypoint under `examples/live/polymarket/`. At startup it
loads the configured strategy preset set from the existing strategy library, configures a single
paper trading node using Nautilus sandbox execution, and enters a loop.

For each iteration:

1. Resolve the current BTC 5-minute market using the reusable session resolver.
2. Validate that the market is open and accepting orders.
3. Run one round until the market end time or a configured execution cutoff.
4. Write strategy result records and round-boundary events to JSONL.
5. Sleep until the next 5-minute boundary with a small guard buffer.
6. Repeat until stopped.

Strategies run within the same process, but every result record is namespaced with a stable
`strategy_name`, `strategy_id`, and daemon `run_id`. The daemon does not persist in-memory paper
positions across process restarts. A restart begins a fresh daemon session with clear provenance.

## Output Design

Leg 4 stores run outputs only, not time-series market data.

Time-series quote data remains the responsibility of the Leg 2 recorder and is stored as Nautilus
`QuoteTick` data in the Parquet catalog, with Polymarket-specific metadata sidecars under the
catalog metadata directory.

The daemon writes append-only JSONL files under:

- `outputs/polymarket/runs/`

Recommended naming:

- `overnight_{preset_set}_{timestamp}.jsonl`

Each strategy result line should be self-contained and include at minimum:

- `run_id`
- `session_id`
- `event`
- `asset`
- `slug`
- `instrument_id`
- `strategy_name`
- `entry_price`
- `exit_price`
- `stop_loss_price`
- `entry_time`
- `exit_time`
- `entry_side`
- `shares`
- `stake`
- `pnl`
- `roi`
- `exit_reason`
- `settled`

Mode-specific diagnostics such as support ratio, microprice, momentum, or flow snapshots may be
included when available and should preserve parity with the migrated `quant` result shape where
practical.

The daemon also writes session-boundary records such as:

- `round_start`
- `round_end`
- `round_skipped`
- `error`

JSONL should flush on each write to reduce data loss during stop or crash scenarios.

## Failure Handling

The daemon should fail soft on operational venue issues and fail hard on configuration or code
errors.

Expected recoverable conditions:

- transient Gamma resolution failures,
- websocket disconnects,
- temporary Polymarket API errors,
- round-boundary race conditions,
- late round starts after recovery.

For recoverable issues, the daemon should:

1. write an `error` event with timestamps and context,
2. back off briefly,
3. re-resolve the BTC session,
4. either continue the current round if still valid or skip it explicitly.

If the usable entry window for a round is already missed, the daemon should emit a
`round_skipped` event instead of forcing a late paper entry.

Configuration errors, invalid preset wiring, serialization failures, or non-recoverable runtime
bugs should stop the process and surface clearly through logs and exit status.

## Shutdown And Restart Semantics

The daemon traps SIGINT and SIGTERM.

On shutdown it should:

1. stop accepting new rounds,
2. allow the current round bookkeeping to flush final JSONL records,
3. stop and dispose the Nautilus node cleanly,
4. exit without partial JSON objects or ambiguous run state.

On restart it should:

- create a new output file,
- allocate a new daemon `run_id`,
- begin at the currently resolvable BTC 5-minute market,
- avoid trying to reconstruct prior in-memory paper positions.

This is the safest phase 1 behavior for long-running paper execution.

## Operational Controls

Keep operational controls intentionally small:

- preset set selection,
- output directory,
- maximum rounds for bounded smoke tests,
- backoff intervals,
- optional execution cutoff before market end.

Do not add a control plane, external scheduler, or database-backed result store in Leg 4.

## Testing Plan

Tests should focus on orchestration correctness, not venue realism.

Required coverage:

- daemon starts a valid BTC round from a resolved session,
- daemon rolls from one round to the next,
- daemon skips a round cleanly when market resolution or open validation fails,
- daemon logs recoverable errors and continues,
- daemon writes both strategy result records and boundary events,
- daemon shuts down cleanly and flushes JSONL,
- bounded run controls such as `--max-rounds 1` behave deterministically.

Test location:

- `tests/unit_tests/examples/test_polymarket_crypto_5m_daemon.py`

## Proposed Implementation Surface

- `examples/live/polymarket/polymarket_crypto_5m_paper_daemon.py`
- optional helper module under `examples/live/polymarket/` if orchestration logic grows too large
- `.docker/docker-compose.yml` service wiring for the daemon
- `.docker/README.md` operator documentation update
- `tests/unit_tests/examples/test_polymarket_crypto_5m_daemon.py`

## Acceptance Fit

This design fits TASK-006 in `TODO.md`:

- long-running BTC-first paper daemon,
- automatic roll to next 5-minute market,
- isolated strategy identities,
- persisted run metadata,
- compose-based startup path,
- lifecycle-focused test coverage.

It deliberately leaves reporting to Leg 5 and multi-asset execution to later legs.
