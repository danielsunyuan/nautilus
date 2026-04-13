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
assert compose['services']['papertrade']['network_mode'] == 'service:nordvpn'
assert compose['services']['papertrade']['build']['dockerfile'] == '.docker/nautilus_trader.dockerfile'
PY
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
- Status: Pending
- Dependencies: TASK-003, TASK-004
- Output: Strategy preset/config modules covering grid, quant filters, spread switch handling, flow and momentum gates, and library-backed stop variants, with `spread_regime` documented as a parity-gap mode instead of a daemon default.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_strategies.py -q

### TASK-006: Add always-on BTC-first paper trading daemon

- Description: Build a Nautilus paper trading daemon that rolls across 5-minute BTC markets until stopped, keeps strategy namespaces isolated, and persists run metadata.
- Agent Type: Execution / routing
- Status: Pending
- Dependencies: TASK-003, TASK-004, TASK-005
- Output: Long-running daemon entrypoint, compose wiring, and daemon lifecycle tests.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_daemon.py -q

### TASK-007: Add Nautilus-native Polymarket 5m reporting

- Description: Generate Nautilus-native run reports equivalent to the current `RESULTS.md` leaderboard and aggregate performance summaries.
- Agent Type: Analytics / reporting
- Status: Pending
- Dependencies: TASK-004, TASK-005, TASK-006
- Output: Reporting module and tests for leaderboard parity fields, stop/settlement accounting, and per-strategy aggregation.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_reporting.py -q

### TASK-008: Add multi-asset data readiness

- Description: Extend the recorder and non-executing session infrastructure to all supported 5m assets while keeping phase-1 paper execution BTC-only.
- Agent Type: Data engineering / orchestration
- Status: Pending
- Dependencies: TASK-004, TASK-006
- Output: Multi-asset recorder/session support with tests for concurrent asset collection.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_multi_asset.py -q

### TASK-009: Add QA parity replay harness

- Description: Compare Nautilus strategy decisions against the current `quant` reference behavior over recorded 5m sessions and define acceptable parity thresholds.
- Agent Type: QA / simulation
- Status: Pending
- Dependencies: TASK-005, TASK-006, TASK-008
- Output: Replay harness and parity assertions for entry/no-entry, side, timing, exit reason, and outcome accounting.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_parity.py -q

### TASK-010: Add latency and live-readiness gate

- Description: Build the latency and operational readiness checks that must pass before any future live execution work begins.
- Agent Type: Risk / execution
- Status: Pending
- Dependencies: TASK-006, TASK-009
- Output: Latency/readiness module or report definitions and tests for timing semantics, disconnect handling, and kill-switch assumptions.
- Validation:
  - uv run python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_crypto_5m_readiness.py -q
