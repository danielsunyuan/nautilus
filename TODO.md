# TODO

### TASK-001: Add Dockerized Polymarket paper trading with NordVPN

- Description: Add a Docker Compose workflow and Nautilus example script for paper trading Polymarket with live market data, sandbox execution, and NordVPN sidecar routing.
- Agent Type: Systems integration / event-driven engines
- Status: In Progress
- Dependencies: None
- Output: Docker services for `nordvpn` and `papertrade`, a Polymarket paper trading example, and operator docs for running it.
- Validation:
  - python - <<'PY'
import yaml
from pathlib import Path
compose = yaml.safe_load(Path('.docker/docker-compose.yml').read_text())
assert 'nordvpn' in compose['services']
assert compose['services']['nordvpn']['networks'] == ['nautilus-network']
assert compose['services']['papertrade']['network_mode'] == 'service:nordvpn'
assert compose['services']['papertrade']['build']['dockerfile'] == '.docker/nautilus_trader.dockerfile'
assert compose['services']['redis']['image'] == 'redis:7.2-alpine'
assert compose['services']['redis']['volumes'] == ['nautilus-redis:/data']
PY
  - python -m py_compile examples/live/polymarket/polymarket_paper_tester.py
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_papertrade.py -q

### TASK-002: Establish Polymarket 5m convergence governance baseline

- Description: Freeze `quant` and `polynautilus` as reference-only inputs, capture the migration parity matrix, and define acceptance commands for the Nautilus convergence legs.
- Agent Type: Systems integration / event-driven engines
- Status: Completed
- Dependencies: None
- Output: A convergence baseline note in `docs/plans/` plus task tracking for legs 1-8 in this file.
- Validation:
  - test -f docs/plans/2026-04-13-polymarket-5m-convergence-baseline.md
  - grep -nE "reference-only|Parity Matrix|Acceptance Commands|Review Protocol" docs/plans/2026-04-13-polymarket-5m-convergence-baseline.md
  - grep -nE "TASK-00[2-9]|TASK-010" TODO.md

### TASK-003: Add reusable Polymarket 5m session resolver

- Description: Promote the 5m smoke logic into reusable Nautilus-side Polymarket session helpers for slug derivation, Gamma resolution, previous-window fallback, and open-market validation.
- Agent Type: Exchange adapter agent experienced in Polymarket venue integration
- Status: Completed
- Dependencies: TASK-002
- Output: A reusable 5m session/resolver module and tests covering slug math, Gamma payload parsing, and previous-window fallback.
- Validation:
  - python -m pytest --import-mode=importlib --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_runtime.py -q

### TASK-004: Add Nautilus-native Polymarket 5m recorder and metadata store

- Description: Bring the `polynautilus` recorder behavior into Nautilus as the canonical 5m recorder with `QuoteTick` catalog persistence and Polymarket-specific metadata sidecars.
- Agent Type: Data engineering / normalization
- Status: Completed
- Dependencies: TASK-003
- Output: Recorder/runtime modules plus tests for quote persistence, metadata persistence, reconnect behavior, and size handling.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_recorder.py -q -k "quote or metadata or reconnect or size"

### TASK-005: Add Polymarket 5m strategy preset library

- Description: Port the first-wave `quant` strategy preset semantics into a Nautilus strategy library for BTC-first paper execution.
- Agent Type: Strategy
- Status: Completed
- Dependencies: TASK-003, TASK-004
- Output: Strategy preset/config modules covering grid, quant filters, spread switch handling, flow and momentum gates, and library-backed stop variants, with `spread_regime` documented as a parity-gap mode instead of a daemon default.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_strategies.py -q

### TASK-006: Add always-on BTC-first paper trading daemon

- Description: Build a Nautilus paper trading daemon that rolls across 5-minute BTC markets until stopped, keeps strategy namespaces isolated, and persists run metadata.
- Agent Type: Execution / routing
- Status: Completed
- Dependencies: TASK-003, TASK-004, TASK-005
- Output: Long-running daemon entrypoint, compose wiring, and daemon lifecycle tests.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_daemon.py -q

### TASK-007: Add Nautilus-native Polymarket 5m reporting

- Description: Generate Nautilus-native run reports equivalent to the current `RESULTS.md` leaderboard and aggregate performance summaries.
- Agent Type: Analytics / reporting
- Status: Completed
- Dependencies: TASK-004, TASK-005, TASK-006
- Output: Reporting module and tests for leaderboard parity fields, stop/settlement accounting, and per-strategy aggregation.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_reporting.py -q

### TASK-008: Add multi-asset data readiness

- Description: Extend the recorder and non-executing session infrastructure to all supported 5m assets while keeping phase-1 paper execution BTC-only.
- Agent Type: Data engineering / orchestration
- Status: Completed
- Dependencies: TASK-004, TASK-006
- Output: Multi-asset recorder/session support with tests for concurrent asset collection.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_multi_asset.py -q

### TASK-009: Add QA parity replay harness

- Description: Compare Nautilus strategy decisions against the current `quant` reference behavior over recorded 5m sessions and define acceptable parity thresholds.
- Agent Type: QA / simulation
- Status: Completed
- Dependencies: TASK-005, TASK-006, TASK-008
- Output: Replay harness and parity assertions for entry/no-entry, side, timing, exit reason, and outcome accounting.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_parity.py -q

### TASK-010: Add latency and live-readiness gate

- Description: Build the latency and operational readiness checks that must pass before any future live execution work begins.
- Agent Type: Risk / execution
- Status: Completed
- Dependencies: TASK-006, TASK-009
- Output: Latency/readiness module or report definitions and tests for timing semantics, disconnect handling, and kill-switch assumptions.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_readiness.py -q

### TASK-011: Weather confirmed-entry signal evaluator

- Description: Pure-function signal module (`weather_confirmed_signal.py`) with A1/A2/B2 evaluators, data quality guards (freshness, spike filter), two-poll ConfirmTracker, and `build_signal` market+obs→signal function.
- Plan: docs/plans/2026-04-22-weather-confirmed-entry-daemon.md (Tasks 1–4)
- Agent Type: Python / strategy
- Status: Completed ✅ (27/27 tests passing)
- Dependencies: TASK-010
- Output: `examples/live/polymarket/weather_confirmed_signal.py` + 27 unit tests passing
- Validation:
  - uv run --extra polymarket --with pytest python -m pytest tests/unit_tests/examples/test_weather_confirmed_signal.py -v

### TASK-012: Weather confirmed-entry daemon

- Description: Main daemon (`weather_confirmed_entry_daemon.py`) — adaptive WU polling loop, market matching, CLOB execution, JSONL output compatible with settlement + take-profit watchers.
- Plan: docs/plans/2026-04-22-weather-confirmed-entry-daemon.md (Tasks 5–6)
- Agent Type: Python / async / execution
- Status: Completed ✅ (compiles + 8/8 tests passing)
- Dependencies: TASK-011
- Output: `examples/live/polymarket/weather_confirmed_entry_daemon.py` + daemon unit tests passing
- Validation:
  - uv run --extra polymarket --with pytest --with pytest-asyncio python -m pytest tests/unit_tests/examples/test_weather_confirmed_entry_daemon.py --noconftest -v
  - python3 -m py_compile examples/live/polymarket/weather_confirmed_entry_daemon.py

### TASK-013: Wire settlement + take-profit watcher to confirmed entries

- Description: Update `_live_jsonl_files()` in settlement and `load_open_positions()` in take-profit watcher to include `weather_confirmed_live_*.jsonl` files.
- Plan: docs/plans/2026-04-22-weather-confirmed-entry-daemon.md (Task 7)
- Agent Type: Python
- Status: Completed ✅
- Dependencies: TASK-012
- Output: Settlement and take-profit watcher automatically pick up confirmed entries
- Validation:
  - grep -n "weather_confirmed_live" examples/live/polymarket/weather_daily_temperature_settlement.py
  - grep -n "weather_confirmed_live" examples/live/polymarket/weather_daily_temperature_take_profit.py

### TASK-014: Docker service + CLAUDE.md docs for confirmed-entry daemon

- Description: Add `weather-confirmed-entry-vpn` service to docker-compose.yml with $20 daily budget, and update CLAUDE.md service table + settlement architecture section.
- Plan: docs/plans/2026-04-22-weather-confirmed-entry-daemon.md (Tasks 8–9)
- Agent Type: DevOps / docs
- Status: Completed ✅
- Dependencies: TASK-013
- Output: Running container + updated docs
- Validation:
  - grep -n "weather-confirmed-entry-vpn" .docker/docker-compose.yml
  - grep -n "confirmed" CLAUDE.md
