# Sports CLOB Settlement Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Resolve sports paper positions from exact CLOB winning-token evidence instead of Gamma outcome prices or outcome-name matching.

**Architecture:** Keep the existing JSONL settlement loop and fee-aware arithmetic. Replace only the resolution adapter and identity comparison: query `GET /markets/{condition_id}`, validate exactly one winner, and match the entry token ID to the winning token ID. Persist enough CLOB evidence to audit every settlement.

**Tech Stack:** Python 3.13, `httpx`, JSONL, pytest, Docker `nautilus-trader:latest`.

---

### Task 1: Define exact token settlement behavior

**Files:**
- Modify: `tests/unit_tests/examples/test_sports_settlement.py`

**Steps:**

1. Add a `token_id` argument to the entry fixture.
2. Add a failing test where the entry outcome label deliberately differs from
   the CLOB label but the token ID matches; require a win.
3. Add a failing test where outcome labels match but token IDs differ; require
   a loss.
4. Add a failing test requiring an entry without a token ID to remain
   unresolved.
5. Run:
   `python -m pytest tests/unit_tests/examples/test_sports_settlement.py -q`
   in the compiled Nautilus image.
6. Verify the failures are caused by missing token identity support.

### Task 2: Parse CLOB market resolution fail closed

**Files:**
- Modify: `tests/unit_tests/examples/test_sports_settlement.py`
- Modify: `examples/live/polymarket/sports_settlement.py`

**Steps:**

1. Add async failing tests using `httpx.MockTransport` for:
   - one closed market with one winner;
   - an open market;
   - zero winners;
   - multiple winners;
   - a mismatched condition ID;
   - a request failure.
2. Require the successful result to carry `winning_token_id`,
   `winning_outcome`, `resolution_source`, and `observed_at`.
3. Run the focused tests and confirm the CLOB tests fail against the Gamma
   adapter.
4. Extend `UnresolvedEntry` with `token_id` and `MarketResolution` with the
   evidence fields.
5. Replace `fetch_market_resolution` with a request to
   `{clob_base_url}/markets/{condition_id}`.
6. Return unresolved for open markets and `None` for malformed, ambiguous, or
   mismatched responses.
7. Run the focused tests and verify green.
8. Commit:
   `fix(sports): resolve settlements by CLOB token`

### Task 3: Persist evidence and keep the loop safe

**Files:**
- Modify: `tests/unit_tests/examples/test_sports_settlement.py`
- Modify: `examples/live/polymarket/sports_settlement.py`

**Steps:**

1. Add a failing settlement-event assertion for:
   - `winning_token_id`;
   - `winning_outcome`;
   - `resolution_source`;
   - `resolution_observed_at`.
2. Add a failing loop test with a fee-missing entry and assert the loop writes
   one settlement without raising while logging gross P/L rather than
   formatting `None` as a float.
3. Emit the evidence fields and make loop logging tolerate unknown net P/L.
4. Run the focused tests and verify green.
5. Commit:
   `feat(sports): persist CLOB settlement evidence`

### Task 4: Migrate CLI and Compose configuration

**Files:**
- Modify: `tests/unit_tests/examples/test_sports_settlement.py`
- Modify: `examples/live/polymarket/sports_settlement.py`
- Modify: `.docker/docker-compose.yml`

**Steps:**

1. Add a failing parser test requiring the default
   `https://clob.polymarket.com` and `--clob-host`.
2. Replace `--gamma-host` with `--clob-host` in the parser and async entrypoint.
3. Change the `sports-settlement-vpn` Compose command to use the CLOB host.
4. Run the focused tests and verify green.
5. Render the Compose configuration and verify the service command contains
   the CLOB host.
6. Commit:
   `chore(sports): route settlement to CLOB`

### Task 5: Document and verify

**Files:**
- Modify: `docs/polymarket_sports_forward_observation.md`
- Modify: `docs/plans/2026-07-29-sports-clob-settlement-and-forward-pipeline-design.md`

**Steps:**

1. Document exact winning-token authority and fail-closed ambiguity.
2. Run all sports unit tests in `nautilus-trader:latest`.
3. Run `git diff --check`.
4. Review every changed line against the paper-only boundary.
5. Commit:
   `docs(sports): record CLOB settlement authority`
