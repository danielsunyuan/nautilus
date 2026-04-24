# Weather Manager Review And Fleet Execution Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
>
> **For Claude:** This is a manager handoff. Execute with parallel subagents only on disjoint write sets. Workers are not alone in the codebase; do not revert unrelated edits.

**Goal:** Keep the useful parts of the confirmed-entry ladder refactor, remove the correctness and measurement defects it introduced or exposed, and move the weather stack toward an EV-based selector instead of more threshold heuristics.

**Architecture:** Treat `weather_confirmed_entry_daemon.py` as the execution layer, not the predictive edge. Preserve city-level pre-fetch and single-cycle ladder suppression, but first fix stale-observation confirmation, post-fetch polling cadence, and ledger/reporting integrity. After the ledger can measure edge cleanly, add a pure city-level EV selector that chooses one candidate from calibrated probabilities rather than blindly taking the highest confirmed rung.

**Tech Stack:** Python 3.12, `httpx`, `py_clob_client`, Polymarket CLOB, `weather_wunderground_fetcher.py`, JSONL live ledger, `pytest`.

---

## Manager Decision

- Keep Phase 1 pre-fetch. It is worth keeping primarily because it gives one consistent observation snapshot per city per cycle and one stable `prev_max`, not because it massively reduces API calls. `fetch_daily_high()` already caches by city in `weather_wunderground_fetcher.py:412-415`.
- Keep the idea of "only one A1 rung per city per cycle", but do not keep the current implementation unchanged.
- Keep the daemon-side B2 gate only if you want a defensive invariant. Under the current `build_signal()` contract it adds no real decision value.
- Do not spend another iteration on ladder heuristics until the runtime correctness and ledger/reporting defects below are fixed.
- The better medium-term solution is not "more ladder rules". It is "one best EV weather candidate per city" driven by a calibrated probability input.
- Current weather results do not justify more heuristic rollout yet. The latest report shows negative edge and weak samples in `WEATHER_DAILY_TEMPERATURE_RESULTS.md:29-36` and `WEATHER_DAILY_TEMPERATURE_RESULTS.md:81-110`.

## Findings To Act On

1. **High: stale data can falsely create a second confirming poll.**  
   In `weather_confirmed_entry_daemon.py:183-196`, Phase 1 leaves `latest_obs` untouched on fetch miss. Phase 2 then reuses cached observations at `weather_confirmed_entry_daemon.py:219-245`, which can advance `ConfirmTracker` counts even though no fresh poll arrived. With `MIN_CONFIRM_POLLS = 2` in `weather_confirmed_signal.py:43`, one real breach plus one failed fetch can incorrectly satisfy A1/A2 confirmation.

2. **High: weather entry rows still do not emit canonical `strategy_name`.**  
   Confirmed entries write `preset_name` and `strategy_type` only in `weather_confirmed_entry_daemon.py:112-143`. Live weather entries write `preset_name` only in `polymarket_weather_daily_temperature_live_daemon.py:462-474` and `polymarket_weather_daily_temperature_live_daemon.py:791-819`. Settlement, TP, and report readers all expect `strategy_name` in `weather_daily_temperature_settlement.py:242-247`, `weather_daily_temperature_take_profit.py:194-205`, and `weather_daily_temperature_report.py:167-182`. This is why the live strategy leaderboard collapses to `unknown`.

3. **High: TP/manual exits are misclassified by the report.**  
   The TP watcher writes `settlement_update` rows using the sell price as `settlement_price` and computes `resolved_outcome` from realized PnL in `weather_daily_temperature_take_profit.py:286-306`. The report classifier still treats a win as `settlement_price == 1.0 and pnl > 0` in `weather_daily_temperature_report.py:32-49`. A profitable exit at `0.95` or `0.99` can therefore be scored as a loss.

4. **High: report merge keys are not unique enough.**  
   `merge_entries_with_settlements()` keys by `(market_slug, strategy_name)` in `weather_daily_temperature_report.py:99-124`. That can overwrite repeated entries or repeated settlements for the same market/strategy pair. The ledger needs a stable `entry_id` or `position_id` propagated from entry through exit.

5. **Medium: poll cadence is computed before fresh Phase 1 observations are loaded.**  
   `_run_main_loop()` computes `poll_secs` from stale `latest_obs` at `weather_confirmed_entry_daemon.py:423-427`, then fetches fresh observations inside `_run_poll_cycle()` at `weather_confirmed_entry_daemon.py:180-196`, then sleeps the stale interval at `weather_confirmed_entry_daemon.py:458-465`. On startup that can sleep `900s` even after discovering a near-threshold city.

6. **Medium: the A1 city latch currently fires too early.**  
   `city_a1_entered.add(city)` happens at `weather_confirmed_entry_daemon.py:275-276` before quote, max-price, budget, and order-success checks at `weather_confirmed_entry_daemon.py:282-345`. A higher rung can suppress lower rungs for the whole cycle even when the higher rung never actually survives gating.

7. **Medium: sorting by `(city, -threshold_f)` changes cross-city budget priority.**  
   `sorted(markets, key=lambda m: (m.city, -m.threshold_f))` at `weather_confirmed_entry_daemon.py:201` does more than sort thresholds within a city. It also makes city name affect execution order under budget pressure.

8. **Medium: strategy A/B measurement is blocked by slug-only de-duplication.**  
   `_already_entered_today()` returns slugs only in `polymarket_weather_daily_temperature_live_daemon.py:612-644`, and the confirmed daemon filters by slug at `weather_confirmed_entry_daemon.py:413-420`. Once one strategy opens a market, other strategies in the same ledger lose the opportunity, which makes same-opportunity comparison weak.

9. **Low: the arena table hides active live arenas.**  
   The report hard-codes `temp_50c` through `temp_90c` in `weather_daily_temperature_report.py:23` and renders only those rows in `weather_daily_temperature_report.py:333-356`. Live weather also trades `temp_90c_no` and `temp_confirmed`.

10. **Low: default output roots are inconsistent across weather components.**  
    `weather_daily_temperature_take_profit.py:51` defaults to `/workspace/nautilus/outputs`, while settlement and shared weather daemons default to `/workspace/outputs` in `weather_daily_temperature_settlement.py:66` and `polymarket_weather_daily_temperature_paper_daemon.py:112`.

## Definition Of Done

- [ ] Confirmed and live weather `strategy_result` rows always carry non-empty `strategy_name`.  
  Eval: inspect generated JSONL rows and run report tests.
- [ ] A failed weather fetch cannot advance A1/A2 confirmation counts.  
  Eval: daemon unit test covering stale-cache reuse passes.
- [ ] Next poll interval is based on fresh observations from the current cycle.  
  Eval: daemon unit test proves `300s` on first near-threshold cycle.
- [ ] Weather reporting classifies take-profit/manual exits correctly and no longer collapses the strategy leaderboard to `unknown`.  
  Eval: report unit tests plus regenerated summary on local live ledger.
- [ ] Entry/exit joins use a stable per-entry identifier instead of `(market_slug, strategy_name)` only.  
  Eval: settlement/report tests with duplicate market+strategy rows pass.
- [ ] An EV-selector scaffold exists as a pure module with tests, but is not wired live yet.  
  Eval: dedicated selector test file passes.
- [ ] With a valid `TWC_API_KEY`, one dry-run daemon cycle completes without WU `401` for WU cities.  
  Eval: container log review.

## Fleet Dispatch

### Worker A: Runtime Correctness And Event Schema

**Ownership:**  
- Modify: `examples/live/polymarket/weather_confirmed_entry_daemon.py`  
- Modify: `examples/live/polymarket/polymarket_weather_daily_temperature_live_daemon.py`  
- Modify: `tests/unit_tests/examples/test_weather_confirmed_entry_daemon.py`

**Objective:** Fix the daemon runtime bugs and make emitted weather entry rows structurally usable downstream.

**Success Criteria**
- [ ] `strategy_name` is emitted on all weather entry rows.  
  Eval: direct JSON dict assertions in unit tests.
- [ ] Confirm counts do not advance on fetch miss / `None` observation.  
  Eval: `_run_poll_cycle` stale-fetch test.
- [ ] Poll interval uses current-cycle observations, not prefetch state.  
  Eval: first-cycle cadence test.
- [ ] City A1 suppression is applied only after the chosen policy point, not at raw signal creation.  
  Eval: higher-rung rejection test.
- [ ] Cross-city ordering behavior is explicit and covered by test.  
  Eval: ordering test.

**Implementation Steps**
1. Add `strategy_name` to both weather entry writers.  
   For confirmed entries, set `strategy_name = signal.preset_name` and keep `strategy_type = signal.strategy` as extra metadata.  
   For live arena entries, set `strategy_name = preset.name`.
2. Prevent stale cached observations from recording synthetic confirmations.  
   Recommended rule: if the Phase 1 fetch for a city fails or returns `None`, skip confirmation updates and signal evaluation for that city for the current cycle.
3. Recompute or defer `poll_secs` until after Phase 1 fetch completes.  
   Cleanest option: make `_run_poll_cycle()` return `(budget_remaining, next_poll_secs)` from fresh `latest_obs`.
4. Limit the sort policy to intra-city threshold ranking only.  
   Preserve resolver order across cities; sort descending threshold within each city's slice.
5. Move the `city_a1_entered` latch to the point where you actually intend to suppress lower rungs.  
   Recommendation: latch only after the candidate passes quote/max-price/budget checks, immediately before order submission, or after successful dry-run acceptance if dry-run should still lock the ladder.
6. Add real `_run_poll_cycle()` tests for:  
   - one fetch per unique city  
   - stale fetch miss does not advance confirmation  
   - highest rung rejected, lower rung still eligible  
   - fresh obs drive first-cycle `300s` polling  
   - explicit cross-city ordering behavior

**Verification Commands**
```bash
cd /home/atlas/EL/nautilus
uv run --extra polymarket --with pytest --with pytest-asyncio python -m pytest \
  tests/unit_tests/examples/test_weather_confirmed_entry_daemon.py \
  --noconftest -q
```

**Commit**
```bash
git add examples/live/polymarket/weather_confirmed_entry_daemon.py \
  examples/live/polymarket/polymarket_weather_daily_temperature_live_daemon.py \
  tests/unit_tests/examples/test_weather_confirmed_entry_daemon.py
git commit -m "fix: harden weather confirmed-entry runtime and schema"
```

### Worker B: Ledger, Settlement, TP, And Reporting Integrity

**Ownership:**  
- Modify: `examples/live/polymarket/weather_daily_temperature_report.py`  
- Modify: `examples/live/polymarket/weather_daily_temperature_settlement.py`  
- Modify: `examples/live/polymarket/weather_daily_temperature_take_profit.py`  
- Modify: `tests/unit_tests/examples/test_polymarket_weather_daily_temperature_report.py`  
- Modify: `tests/unit_tests/examples/test_polymarket_weather_daily_temperature_settlement.py`

**Objective:** Make the weather ledger measurable and internally consistent so strategy edge can be trusted.

**Success Criteria**
- [ ] Strategy leaderboard groups confirmed/live rows under real strategy names.  
  Eval: report test and local summary render.
- [ ] Duplicate entries for one market do not overwrite each other during merge.  
  Eval: report/settlement duplicate-key test.
- [ ] TP/manual exits are classified by realized outcome, not oracle-only `settlement_price == 1.0`.  
  Eval: report classification test.
- [ ] All live arenas appear in the arena table.  
  Eval: render test.
- [ ] TP, settlement, and report point at the same default output root.  
  Eval: code inspection plus unit test where applicable.

**Implementation Steps**
1. Introduce one normalization helper for weather ledger rows.  
   Use `strategy_name` if present, otherwise fall back to `preset_name`, then `strategy_type`, then `"unknown"`.  
   This is a compatibility bridge, not an excuse to keep missing `strategy_name`.
2. Introduce a stable `entry_id` or `position_id` on the reporting/settlement side.  
   If Worker A has already emitted it, consume it. If not, Claude must add the smallest compatible fallback in a follow-up integration pass.
3. Change report merge logic to join exits to entries by that stable identifier, not by `(market_slug, strategy_name)` alone.
4. Fix classification for non-oracle exits.  
   Recommended rule: if `exit_method` or TP/manual exit semantics are present, trust `resolved_outcome` and `pnl`; reserve `settlement_price == 1.0/0.0` logic for oracle resolution rows.
5. Expand or data-drive `ALL_ARENAS` so `temp_90c_no` and `temp_confirmed` render explicitly.
6. Unify default weather output directory handling across settlement, TP watcher, and reporting paths.
7. Add tests covering:  
   - confirmed rows with only `preset_name`/`strategy_type`  
   - duplicate same-market same-strategy entries  
   - profitable TP exits  
   - arena rendering for `temp_90c_no` and `temp_confirmed`

**Verification Commands**
```bash
cd /home/atlas/EL/nautilus
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_polymarket_weather_daily_temperature_report.py \
  tests/unit_tests/examples/test_polymarket_weather_daily_temperature_settlement.py \
  -q
```

**Commit**
```bash
git add examples/live/polymarket/weather_daily_temperature_report.py \
  examples/live/polymarket/weather_daily_temperature_settlement.py \
  examples/live/polymarket/weather_daily_temperature_take_profit.py \
  tests/unit_tests/examples/test_polymarket_weather_daily_temperature_report.py \
  tests/unit_tests/examples/test_polymarket_weather_daily_temperature_settlement.py
git commit -m "fix: repair weather ledger attribution and reporting"
```

### Worker C: EV Selector Scaffold

**Ownership:**  
- Create: `examples/live/polymarket/weather_city_ev_selector.py`  
- Create: `tests/unit_tests/examples/test_weather_city_ev_selector.py`  
- Create: `docs/plans/2026-04-22-weather-ev-selector-notes.md`

**Objective:** Build the pure selection layer for the better algorithm without wiring it live yet.

**Success Criteria**
- [ ] Selector chooses at most one weather candidate per city/day.  
  Eval: selector tests.
- [ ] Ranking is by net EV, not by raw threshold.  
  Eval: selector tests with competing rungs.
- [ ] Tie-break is explicit and deterministic.  
  Eval: selector tests.
- [ ] The design note clearly states what calibrated probability inputs will be required later.  
  Eval: human review.

**Implementation Steps**
1. Create a pure dataclass-driven module, for example:
   ```python
   @dataclass(frozen=True, slots=True)
   class CandidateSignal:
       city: str
       observation_date: str
       market_slug: str
       token_side: str
       threshold_f: float
       mid: float
       estimated_prob: float
       fee_rate: float = 0.0
       slippage: float = 0.0
   ```
2. Implement EV helpers for both YES and NO tokens.  
   Include fees/slippage in the calculation so "highest confirmed threshold" can lose to a lower rung with better price.
3. Implement `select_best_city_candidate(candidates)` returning one candidate per city/day and a reason code for rejects.
4. Add tests for:  
   - higher threshold but worse EV loses  
   - same EV tie-break behavior  
   - no selection when all EVs are negative  
   - per-city isolation
5. Write a short design note listing the future probability inputs required for live use: oracle-matched observation gap to threshold, local hour, obs count, source type, band type, and optional external priors.

**Verification Commands**
```bash
cd /home/atlas/EL/nautilus
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_weather_city_ev_selector.py \
  -q
```

**Commit**
```bash
git add examples/live/polymarket/weather_city_ev_selector.py \
  tests/unit_tests/examples/test_weather_city_ev_selector.py \
  docs/plans/2026-04-22-weather-ev-selector-notes.md
git commit -m "feat: add pure city-level EV selector for weather"
```

## Claude Integration Pass

After Workers A-C return:

1. Review each patch for file ownership violations and schema conflicts.
2. Integrate Worker A first, then Worker B, then Worker C.
3. Re-run the combined weather suite:
   ```bash
   cd /home/atlas/EL/nautilus
   uv run --extra polymarket --with pytest --with pytest-asyncio python -m pytest \
     tests/unit_tests/examples/test_weather_confirmed_entry_daemon.py \
     tests/unit_tests/examples/test_polymarket_weather_daily_temperature_report.py \
     tests/unit_tests/examples/test_polymarket_weather_daily_temperature_settlement.py \
     tests/unit_tests/examples/test_weather_city_ev_selector.py \
     --noconftest -q
   ```
4. Regenerate the weather report and verify the strategy leaderboard is no longer `unknown`.
5. Only after code/tests pass, provision a valid `TWC_API_KEY` in `.env.polymarket` and run a dry-run daemon cycle inside the correct container.

## Manual Ops Task

This is not a coding subagent task because it involves secrets.

- Add a valid `TWC_API_KEY` to `.env.polymarket`.
- Restart only the relevant weather daemon container.
- Confirm logs show successful WU fetches for WU cities instead of `401`.
- Confirm the first post-start cycle uses fresh observations for polling cadence.
- Confirm generated JSONL rows now carry `strategy_name`.

## Final Recommendation

Keep Claude's pre-fetch refactor and the concept of one A1 rung per city per cycle. Do **not** keep the current implementation as-is. The must-fix items are stale-confirmation risk, pre-fetch polling cadence, canonical strategy naming, and entry/exit identity in the ledger. Once those are repaired, stop optimizing the ladder itself and move the next weather iteration to a pure EV selector fed by calibrated probabilities.
