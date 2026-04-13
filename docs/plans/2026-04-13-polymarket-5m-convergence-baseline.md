# Polymarket 5m Convergence Baseline

**Objective:** move Polymarket 5-minute research, paper execution, and data capture into the `nautilus` repo while keeping `quant` and `polynautilus` as reference-only inputs during parity migration.

**Default scope:** BTC-first always-on paper execution, all 7 supported assets for the data plane, and no live execution in phase 1.

## Reference-Only Repositories

- `quant/`
  - Source of truth for current strategy semantics, paper-run result schema, and leaderboard fields.
  - Do not land new primary feature work there during convergence.
- `polynautilus/`
  - Source of truth for current 5m recorder, metadata sidecars, live signal watcher, and multi-asset recording behavior.
  - Do not land new primary feature work there during convergence.

## Parity Matrix

### Strategy families from `quant`

| Family | Mode names | Required inputs / features | Outputs / metrics | Migration notes |
|---|---|---|---|---|
| Core entry / baseline | `basic` | Polymarket best bid/ask, bid/ask size, threshold dwell, timing gates, spread gate, bid support gate, order min size | entry/exit, side, shares, stake, profit, close reason, summary counters | Shared base path for grid, baseline, low-entry, and control presets |
| Quant filters | `microprice`, `support_ratio`, `quote_stability`, `microprice_support` | top-of-book sizes, microprice epsilon, support ratio, quote stability state | Same shared result schema | `quote_stability` requires per-token quote history and reset-on-regime-change behavior |
| Spread switch / regime | `spread_switch`, `spread_regime` | spread threshold and alternate tight entry price | Same shared result schema | `spread_switch` is wired; `spread_regime` is a known parity gap in `quant` and should not be copied blindly |
| Flow-gated | `flow_imbalance`, `microprice_flow` | rolling bid/ask size history, imbalance window, minimum sample count | Same shared result schema, plus flow snapshot at entry | Fail closed when flow data is insufficient |
| Momentum-gated | `binance_momentum`, `microprice_momentum` | Binance mid-price samples, window, sample count, poll interval | Same shared result schema, plus momentum snapshot at entry | Direction mapping is YES=up, NO=down |
| Stop / risk variants | `adaptive_stop`, `trailing_stop`, fixed-stop `basic` presets | entry price, max/min bid seen, drawdown fraction, trail fraction | stop/target/settlement outcomes, drawdown metrics | Adaptive/trailing remain library-backed but are not phase-1 daemon defaults |

### Recorder and watcher behavior from `polynautilus`

| Area | Current behavior | Nautilus migration target |
|---|---|---|
| Session selection | Current 5m slug per asset with Gamma retry and websocket reconnect | Reusable 5m session resolver used by smoke, recorder, watcher, and daemon |
| Quote persistence | `QuoteTick` parquet partitions by `InstrumentId` | Keep native Nautilus `QuoteTick` persistence |
| Metadata sidecar | `polymarket_5m_resolutions.jsonl` with slug, token IDs, resolution, outcomes, prices, and recorder envelope fields | Preserve as Polymarket-specific metadata under the Nautilus catalog root |
| Multi-asset behavior | concurrent tasks for `BTC,ETH,SOL,XRP,DOGE,BNB,HYPE` | Keep data plane multi-asset from day one; keep phase-1 execution BTC-only |
| Signal watcher | rolling `mid_max`, `spread_max`, `updates_per_s` risk alerts | Keep as optional sidecar after recorder parity is in place |

## Known Gaps To Close In Nautilus

- No reusable 5m session/resolver helper exists yet in the clean Nautilus branch.
- No canonical recorder exists yet in the clean Nautilus branch.
- No Nautilus-side strategy preset library exists for the `quant` families.
- No always-on paper daemon exists for rolling between 5m markets.
- No Nautilus-native reporting exists yet for `RESULTS.md` parity.
- Bid/ask sizes need explicit validation because the reference recorder docs say parquet sizes are effectively zero today.

## Acceptance Commands

These commands are the intended implementation gates for later legs and should stay aligned with `TODO.md`.

- Leg 0 governance baseline:
  - `test -f docs/plans/2026-04-13-polymarket-5m-convergence-baseline.md`
  - `rg -n "reference-only|Parity Matrix|Acceptance Commands|Review Protocol" docs/plans/2026-04-13-polymarket-5m-convergence-baseline.md`
  - `rg -n "TASK-00[2-9]|TASK-010" TODO.md`
- Leg 1 runtime foundation:
  - `python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_runtime.py -q -k "slug or fallback or gamma or accepting"`
- Leg 2 recorder:
  - `python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_recorder.py -q -k "quote or metadata or reconnect or size"`
- Leg 3 strategy library:
  - `python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_strategies.py -q`
- Leg 4 daemon:
  - `python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_daemon.py -q`
- Leg 5 reporting:
  - `python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_reporting.py -q`
- Leg 6 multi-asset readiness:
  - `python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_multi_asset.py -q`
- Leg 7 parity harness:
  - `python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_parity.py -q`
- Leg 8 readiness gate:
  - `python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_readiness.py -q`

## Review Protocol

Every implementation leg must follow the same sequence:

1. Implementation subagent completes the leg with tests.
2. Spec-compliance review confirms the leg matches the migration plan and parity matrix.
3. Code-quality review confirms implementation quality and maintainability.
4. Fresh verification commands are run before any success claim or commit.
5. Commit at least one checkpoint for the leg using a conventional commit message.
