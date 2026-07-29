# Sports Accounting V2 Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Make sports paper results uniquely attributable and fee-aware, make CLV confirmation fail closed, and freeze the forward-observation data contract.

**Architecture:** Preserve the existing Nautilus sports runtime and JSONL flow. Tighten extraction around actual runtime `StrategyId`, carry position identity and commissions into strategy rows, calculate explicit gross/fee/net settlement fields, and teach reporting to expose missing fees instead of silently claiming net P&L.

**Tech Stack:** Python 3.13, NautilusTrader sandbox/cache models, JSONL, pytest, Docker `nautilus-trader:latest`.

---

### Task 1: Fail-closed CLV confirmation

**Files:**
- Modify: `examples/live/polymarket/sports_odds_client.py`
- Modify: `tests/unit_tests/examples/test_sports_odds_client.py`

**Steps:**

1. Change the existing missing-Vegas-data test to require `False`.
2. Run the focused test and verify it fails because `has_clv_edge` returns
   `True`.
3. Make `has_clv_edge` return `False` when `vegas_implied is None`.
4. Update docstrings that currently promise fail-open behavior.
5. Run the odds and strategy-library tests and verify they pass.

### Task 2: Attribute positions by runtime strategy ID

**Files:**
- Modify: `examples/live/polymarket/polymarket_sports_paper_daemon.py`
- Modify: `tests/unit_tests/examples/test_sports_daemon_helpers.py`

**Steps:**

1. Add a failing test with two presets on one instrument and one position whose
   `strategy_id` matches only one registered runtime strategy.
2. Verify the old extractor credits the position to both compatible presets.
3. Index cache positions by `(instrument_id, strategy_id)`.
4. Query the index using `strategy_ids_by_key`, with no price-band inference
   fallback.
5. Add `strategy_id` and `position_id` to credited strategy-result rows.
6. Verify the targeted extractor tests pass.

### Task 3: Carry actual entry commission

**Files:**
- Modify: `examples/live/polymarket/polymarket_sports_paper_daemon.py`
- Modify: `tests/unit_tests/examples/test_sports_daemon_helpers.py`

**Steps:**

1. Add a failing test for a position exposing one USDC commission.
2. Add a minimal helper that sums position commissions in the settlement
   currency and returns `None` when unavailable or ambiguous.
3. Emit `entry_fee` and `fee_status` on strategy-result rows.
4. Verify known and missing-fee cases.

### Task 4: Make settlement arithmetic gross/fee/net explicit

**Files:**
- Modify: `examples/live/polymarket/sports_settlement.py`
- Create: `tests/unit_tests/examples/test_sports_settlement.py`

**Steps:**

1. Add failing win and loss tests asserting `gross_pnl`, `entry_fee`, and
   `net_pnl`.
2. Extend `UnresolvedEntry` with optional fee data parsed from strategy rows.
3. Calculate `gross_pnl = (settlement_price - entry_price) * shares`.
4. Calculate `net_pnl = gross_pnl - entry_fee` only when the fee is known.
5. Keep legacy `pnl` as the net value when known and `None` when fee data is
   missing.
6. Verify settlement tests pass.

### Task 5: Report fee completeness and net P&L

**Files:**
- Modify: `examples/live/polymarket/sports_report.py`
- Modify: `tests/unit_tests/examples/test_sports_report.py`

**Steps:**

1. Add failing tests proving v2 rows aggregate `net_pnl` and legacy rows count
   as missing-fee records.
2. Merge the new settlement fields into strategy rows.
3. Add fee-known and fee-missing counters to every bucket and the portfolio
   summary.
4. Render an explicit fee-completeness line and label totals as net only when
   every resolved row has a known fee.
5. Verify report tests pass.

### Task 6: Freeze the forward-observation schema

**Files:**
- Create: `examples/live/polymarket/sports_observation_schema.py`
- Create: `tests/unit_tests/examples/test_sports_observation_schema.py`
- Create: `docs/polymarket_sports_forward_observation.md`

**Steps:**

1. Add a failing test for required identifiers, checkpoint, venue quote/depth,
   fee schedule, bookmaker consensus, freshness, and resolution fields.
2. Implement a small frozen dataclass with validation and JSON-serializable
   output; do not add collection or network behavior.
3. Document checkpoint and missing-data semantics plus the research-only
   boundary.
4. Verify schema tests pass.

### Task 7: Full verification

**Files:** No production changes.

**Steps:**

1. Run all sports unit tests in the compiled Nautilus image.
2. Run `git diff --check`.
3. Review every changed line against the design scope.
4. Record commands and actual results in the final handoff.
