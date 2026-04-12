# Polymarket Papertrade Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a Docker-first Nautilus workflow for Polymarket paper trading that consumes live data through NordVPN and routes simulated orders through the sandbox execution adapter.

**Architecture:** Extend the repo-local Compose stack with a `nordvpn` sidecar and a `papertrade` runner service. Add a new Polymarket paper-trading example script that keeps the live `PolymarketDataClientConfig` but swaps execution to `SandboxExecutionClientConfig`, so the strategy can trade against live books without sending live Polymarket orders.

**Tech Stack:** Docker Compose, NautilusTrader Python examples, Polymarket data adapter, Nautilus sandbox execution adapter, pytest.

---

### Task 1: Track the work and define the target behavior

**Files:**
- Create: `TODO.md`
- Create: `docs/plans/2026-04-12-polymarket-papertrade-design.md`
- Test: `tests/unit_tests/examples/test_polymarket_papertrade.py`

**Step 1: Write the failing test**

Add a test that parses `.docker/docker-compose.yml` and asserts:
- `nordvpn` exists
- `papertrade` exists
- `papertrade.network_mode == "service:nordvpn"`

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit_tests/examples/test_polymarket_papertrade.py -q`
Expected: FAIL because the Compose file does not yet define those services.

**Step 3: Write minimal implementation**

Add the new services to `.docker/docker-compose.yml`.

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit_tests/examples/test_polymarket_papertrade.py -q`
Expected: PASS for the compose assertions.

### Task 2: Add a paper-trading example that uses sandbox execution

**Files:**
- Create: `examples/live/polymarket/polymarket_paper_tester.py`
- Test: `tests/unit_tests/examples/test_polymarket_papertrade.py`

**Step 1: Write the failing test**

Add a test that reads the example script source and asserts it references:
- `PolymarketDataClientConfig`
- `SandboxExecutionClientConfig`
- `SandboxLiveExecClientFactory`

And does not reference:
- `PolymarketExecClientConfig`
- `PolymarketLiveExecClientFactory`

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit_tests/examples/test_polymarket_papertrade.py -q`
Expected: FAIL because the script does not yet exist.

**Step 3: Write minimal implementation**

Create the example script by adapting the Polymarket live data example and the existing sandbox execution examples.

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit_tests/examples/test_polymarket_papertrade.py -q`
Expected: PASS.

### Task 3: Document the operator workflow

**Files:**
- Modify: `.docker/README.md`

**Step 1: Write the failing test**

Extend the test to assert `.docker/README.md` mentions the `papertrade` service and `nordvpn`.

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit_tests/examples/test_polymarket_papertrade.py -q`
Expected: FAIL because the README does not yet mention the new workflow.

**Step 3: Write minimal implementation**

Document how to:
- start `nordvpn`
- run the `papertrade` container
- open the `workspace` shell separately

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit_tests/examples/test_polymarket_papertrade.py -q`
Expected: PASS.
