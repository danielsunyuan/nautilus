# Weather Ensemble Nautilus Paper Strategy Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
>
> **For Claude:** This is a manager handoff. Execute with parallel subagents only on disjoint write sets. Workers are not alone in the codebase; do not revert unrelated edits. Keep this strategy isolated from the existing weather arena and confirmed-entry stacks until paper evidence justifies integration.

**Goal:** Port the solid parts of the external weather ensemble strategy into Nautilus as an isolated paper-trading weather daemon with robust forecast parsing, explicit probability-to-edge logic, dedicated settlement/reporting, and enough paper-performance telemetry to judge whether the strategy deserves a longer life inside Nautilus.

**Architecture:** Build a parallel weather strategy family that discovers Polymarket daily temperature markets, computes forecast-based probabilities from ensemble data, selects paper-trade candidates with explicit edge and liquidity gates, enters through Nautilus sandbox execution, and settles/report through separate JSONL-driven passes. Forecasting is only an entry prior; settlement and exit assessment must still respect Nautilus' existing rule that Polymarket resolution is determined by the market's own oracle source and CLOB behavior, not by the forecast provider.

**Tech Stack:** Python 3.12, NautilusTrader `TradingNode`, Polymarket live data client, Sandbox execution client with `PolymarketFeeModel`, Open-Meteo ensemble API, Polymarket Gamma for market metadata only, Polymarket CLOB for price/resolution checks, JSONL artifacts, `pytest`.

---

## Manager Intent

- Do not mix this with the current weather ladder, confirmed-entry, or price-band families yet.
- This is a **forecast-driven paper experiment**, not a replacement for the current weather stack.
- The useful imported idea is the **forecast distribution -> probability -> edge** loop, not the external repo's looser assumptions about resolution or production readiness.
- Settlement, TP, and post-trade accounting must continue to honor Nautilus' current source-of-truth rules for Polymarket weather markets.
- The first goal is reliable measurement: paper entries, subsequent settlement updates, and a report we can trust the next morning.

## Scope Boundaries

Included:

- isolated ensemble forecast fetcher and probability model,
- candidate ranking logic for daily high/low threshold markets,
- one dedicated weather ensemble paper strategy actor,
- one dedicated paper daemon,
- one dedicated settlement pass and report,
- compose services for daemon, settlement, and reporter,
- unit tests for forecast parsing, EV/edge math, daemon lifecycle, settlement merge, and reporting.

Excluded:

- live execution,
- modifications to the existing confirmed-entry daemon,
- replacing the current weather price-arena baseline,
- using forecast data as a proxy for resolution or manual exit decisions,
- city/station oracle remapping work already handled by the current weather stack.

## Required Robustness Rules

- Gamma may be used for discovery and ruleset metadata only; CLOB must be used for price and resolution state.
- Ensemble forecast data is an **input to entry decisions only**. It is never a settlement source.
- The strategy must fail soft when the forecast provider is unavailable; emit a skipped diagnostic row instead of forcing a trade.
- Probability math must be fully testable offline with canned forecast payloads.
- Output namespace, report name, and service names must keep this family separated from existing weather strategies.

## Definition Of Done

- [ ] A dedicated weather ensemble paper daemon exists and runs independently of the current weather stack.
- [ ] Forecast parsing supports deterministic unit tests and produces stable per-market YES/NO probabilities.
- [ ] Candidate selection records model probability, market price, edge, entry side, and filter reasons per market.
- [ ] A settlement poller and markdown report exist for this family only.
- [ ] A bounded smoke pass completes and writes JSONL without touching the current weather daemons.
- [ ] Compose services exist for overnight paper execution, settlement polling, and report refresh.
- [ ] The report shows paper-performance metrics by city, threshold family, and strategy name.

## Proposed File Surface

Create:

- `examples/live/polymarket/weather_ensemble_models.py`
- `examples/live/polymarket/weather_ensemble_forecast.py`
- `examples/live/polymarket/weather_ensemble_signal_engine.py`
- `examples/live/polymarket/weather_ensemble_strategy_library.py`
- `examples/live/polymarket/weather_ensemble_live_strategy.py`
- `examples/live/polymarket/polymarket_weather_ensemble_paper_daemon.py`
- `examples/live/polymarket/weather_ensemble_settlement.py`
- `examples/live/polymarket/polymarket_weather_ensemble_reporting.py`
- `tests/unit_tests/examples/test_weather_ensemble_forecast.py`
- `tests/unit_tests/examples/test_weather_ensemble_signal_engine.py`
- `tests/unit_tests/examples/test_polymarket_weather_ensemble_daemon.py`
- `tests/unit_tests/examples/test_weather_ensemble_settlement.py`
- `tests/unit_tests/examples/test_weather_ensemble_reporting.py`

Modify:

- `examples/live/polymarket/weather_daily_temperature_resolver.py`
- `.docker/docker-compose.yml`
- `.docker/README.md`

Generated artifact targets:

- `outputs/polymarket/weather_ensemble/weather_ensemble_<timestamp>.jsonl`
- `outputs/polymarket/weather_ensemble/weather_ensemble_settlement.jsonl`
- `WEATHER_ENSEMBLE_RESULTS.md`

## Fleet Dispatch

### Worker A: Forecast Fetching And Probability Engine

**Ownership:**
- Create: `examples/live/polymarket/weather_ensemble_models.py`
- Create: `examples/live/polymarket/weather_ensemble_forecast.py`
- Create: `examples/live/polymarket/weather_ensemble_signal_engine.py`
- Test: `tests/unit_tests/examples/test_weather_ensemble_forecast.py`
- Test: `tests/unit_tests/examples/test_weather_ensemble_signal_engine.py`

**Objective:** Build a deterministic forecast/probability layer that ports the useful ensemble-threshold idea without leaking provider assumptions into execution or settlement.

**Success Criteria**
- [ ] Ensemble payload parsing is deterministic and unit-testable from canned JSON.
- [ ] Probability helpers cover daily high and daily low threshold cases explicitly.
- [ ] The signal engine returns structured candidate decisions with probabilities, edge, side, and filter reasons.
- [ ] The module is pure enough that most logic can be tested with no network and no Nautilus runtime.

**Implementation Steps**
1. Define dataclasses for:
   - ensemble member distributions,
   - market probability snapshots,
   - candidate decisions,
   - filter outcomes.
2. Implement a forecast client for the selected provider, initially Open-Meteo ensemble, with:
   - response validation,
   - timeout handling,
   - small cache/TTL semantics where appropriate,
   - source metadata recording.
3. Implement threshold probability helpers:
   - `probability_high_above`,
   - `probability_high_below`,
   - `probability_low_above`,
   - `probability_low_below`.
4. Add explicit clipping/bounding rules and document them.
5. Implement a signal engine that compares model probability to market YES price and returns:
   - selected side,
   - edge,
   - entry price,
   - confidence,
   - rejection reasons.
6. Add tests for:
   - payload parsing,
   - member counting,
   - exact threshold edge cases,
   - probability clipping,
   - YES vs NO selection,
   - missing forecast data.

**Verification Commands**
```bash
cd /home/atlas/EL/nautilus
docker compose -f .docker/docker-compose.yml exec workspace uv run --with pytest python -m pytest \
  tests/unit_tests/examples/test_weather_ensemble_forecast.py \
  tests/unit_tests/examples/test_weather_ensemble_signal_engine.py \
  --noconftest -q
```

**Commit**
```bash
git add examples/live/polymarket/weather_ensemble_models.py \
  examples/live/polymarket/weather_ensemble_forecast.py \
  examples/live/polymarket/weather_ensemble_signal_engine.py \
  tests/unit_tests/examples/test_weather_ensemble_forecast.py \
  tests/unit_tests/examples/test_weather_ensemble_signal_engine.py
git commit -m "feat: add weather ensemble forecast and signal engine"
```

### Worker B: Nautilus Strategy, Daemon, And Candidate Isolation

**Ownership:**
- Create: `examples/live/polymarket/weather_ensemble_strategy_library.py`
- Create: `examples/live/polymarket/weather_ensemble_live_strategy.py`
- Create: `examples/live/polymarket/polymarket_weather_ensemble_paper_daemon.py`
- Modify: `examples/live/polymarket/weather_daily_temperature_resolver.py`
- Modify: `.docker/docker-compose.yml`
- Test: `tests/unit_tests/examples/test_polymarket_weather_ensemble_daemon.py`

**Objective:** Turn the forecast decision layer into a real Nautilus paper-trading family with isolated service names, outputs, and preset wiring.

**Success Criteria**
- [ ] The strategy actor owns entry decisions inside Nautilus.
- [ ] The daemon discovers weather markets, computes forecast candidates, and only instantiates strategy actors for accepted candidates.
- [ ] The daemon stays isolated from current weather output roots and strategy names.
- [ ] Bounded smoke runs are deterministic and recoverable failures do not crash the family.

**Implementation Steps**
1. Extend or reuse `weather_daily_temperature_resolver.py` only for metadata discovery; do not mix ensemble logic into the resolver itself beyond the smallest shared helper.
2. Create a dedicated preset family, for example `weather_ensemble_baseline`.
3. Create a dedicated strategy actor that receives the precomputed model snapshot and handles quote-based entry gates:
   - price band / max entry cap,
   - spread,
   - ask size,
   - optional one-position-per-market-family protection.
4. Create a dedicated daemon that:
   - discovers open weather markets,
   - fetches forecast distributions,
   - ranks/filters candidates,
   - starts a bounded Nautilus paper node,
   - writes append-only JSONL rows under `outputs/polymarket/weather_ensemble/`.
5. JSONL rows must include:
   - `strategy_name`,
   - `market_slug`,
   - `city`,
   - `threshold`,
   - `band_type`,
   - `forecast_source`,
   - `model_yes_probability`,
   - `market_yes_price`,
   - `edge`,
   - `selected_side`,
   - `confidence`,
   - filter status and reasons.
6. Add compose services:
   - `weather-ensemble-daemon-vpn`
   - `weather-ensemble-settlement-vpn`
   - `weather-ensemble-results-reporter`
7. Add daemon tests for:
   - output path building,
   - market filtering,
   - no forecast -> skipped rows,
   - one-round smoke mode,
   - stable JSONL event shapes,
   - no contamination of existing weather output files.

**Verification Commands**
```bash
cd /home/atlas/EL/nautilus
docker compose -f .docker/docker-compose.yml exec workspace uv run --with pytest python -m pytest \
  tests/unit_tests/examples/test_polymarket_weather_ensemble_daemon.py \
  --noconftest -q
```

**Commit**
```bash
git add examples/live/polymarket/weather_ensemble_strategy_library.py \
  examples/live/polymarket/weather_ensemble_live_strategy.py \
  examples/live/polymarket/polymarket_weather_ensemble_paper_daemon.py \
  examples/live/polymarket/weather_daily_temperature_resolver.py \
  .docker/docker-compose.yml \
  tests/unit_tests/examples/test_polymarket_weather_ensemble_daemon.py
git commit -m "feat: add weather ensemble paper daemon"
```

### Worker C: Settlement, Reporting, And Paper-Performance Review Surface

**Ownership:**
- Create: `examples/live/polymarket/weather_ensemble_settlement.py`
- Create: `examples/live/polymarket/polymarket_weather_ensemble_reporting.py`
- Modify: `.docker/README.md`
- Test: `tests/unit_tests/examples/test_weather_ensemble_settlement.py`
- Test: `tests/unit_tests/examples/test_weather_ensemble_reporting.py`

**Objective:** Make this strategy measurable across days without mixing it into the current weather report or using forecast data as a false settlement source.

**Success Criteria**
- [ ] Settlement logic uses Polymarket market state/CLOB conventions, not the forecast provider.
- [ ] The report shows unresolved candidates separately from settled paper trades.
- [ ] The report gives enough evidence to judge whether forecast probability is better than market probability.

**Implementation Steps**
1. Create a dedicated settlement module for `outputs/polymarket/weather_ensemble/*.jsonl`.
2. Reuse the repo's existing Polymarket resolution discipline:
   - Gamma for market/ruleset metadata if needed,
   - CLOB for resolution pricing or resolved token state,
   - no use of forecast provider as settlement truth.
3. Create a dedicated markdown report containing:
   - total scanned markets,
   - accepted candidates,
   - entered positions,
   - unresolved positions,
   - resolved trades,
   - win rate,
   - net P&L,
   - average edge,
   - average entry price,
   - per-city stats,
   - per-threshold stats,
   - rough calibration buckets.
4. Update Docker docs with exact commands for smoke testing, overnight running, and next-day inspection.
5. Add tests for:
   - merge of strategy rows and settlement rows,
   - unresolved-only files,
   - mixed cities,
   - report stability with partial data,
   - no confusion between forecast source and settlement source.

**Verification Commands**
```bash
cd /home/atlas/EL/nautilus
docker compose -f .docker/docker-compose.yml exec workspace uv run --with pytest python -m pytest \
  tests/unit_tests/examples/test_weather_ensemble_settlement.py \
  tests/unit_tests/examples/test_weather_ensemble_reporting.py \
  --noconftest -q
```

**Commit**
```bash
git add examples/live/polymarket/weather_ensemble_settlement.py \
  examples/live/polymarket/polymarket_weather_ensemble_reporting.py \
  .docker/README.md \
  tests/unit_tests/examples/test_weather_ensemble_settlement.py \
  tests/unit_tests/examples/test_weather_ensemble_reporting.py
git commit -m "feat: add weather ensemble settlement and reporting"
```

## Claude Integration Pass

After Workers A-C return:

1. Review for any attempt to shortcut settlement with forecast data and reject that change.
2. Integrate Worker A first, then Worker B, then Worker C.
3. Run the full targeted suite:
   ```bash
   cd /home/atlas/EL/nautilus
   docker compose -f .docker/docker-compose.yml exec workspace uv run --with pytest python -m pytest \
     tests/unit_tests/examples/test_weather_ensemble_forecast.py \
     tests/unit_tests/examples/test_weather_ensemble_signal_engine.py \
     tests/unit_tests/examples/test_polymarket_weather_ensemble_daemon.py \
     tests/unit_tests/examples/test_weather_ensemble_settlement.py \
     tests/unit_tests/examples/test_weather_ensemble_reporting.py \
     --noconftest -q
   ```
4. Run a smoke pass:
   ```bash
   cd /home/atlas/EL/nautilus
   docker compose -f .docker/docker-compose.yml run --rm papertrade \
     python examples/live/polymarket/polymarket_weather_ensemble_paper_daemon.py \
       --preset-set weather_ensemble_baseline \
       --max-rounds 1 \
       --output-dir /workspace/outputs
   ```
5. Inspect the generated JSONL for forecast diagnostics and report rendering.
6. If smoke is clean, bring up the dedicated services:
   ```bash
   cd /home/atlas/EL/nautilus
   docker compose -f .docker/docker-compose.yml up -d \
     weather-ensemble-daemon-vpn \
     weather-ensemble-settlement-vpn \
     weather-ensemble-results-reporter
   ```

## Paper-Performance Observation Window

- Let this family run for multiple settlement cycles before any verdict.
- Minimum review threshold:
  - 50+ scanned markets,
  - 15+ paper entries,
  - at least one full next-day settlement cycle,
  - one refreshed `WEATHER_ENSEMBLE_RESULTS.md`.
- Review questions:
  - Does model probability show better discrimination than the raw market price?
  - Are edge estimates concentrated in illiquid or thin-spread traps?
  - Do some cities systematically underperform because the forecast prior is mismatched to the oracle station?
  - Is the strategy actually capturing edge after fees and selection filters?

Do not merge this family into the main weather stack until the paper-performance evidence is clear and the attribution remains clean.
