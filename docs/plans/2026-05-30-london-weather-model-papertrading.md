# London Weather Model Paper Trading Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Wire the `research/weather` London highest-temperature probability models into Nautilus Polymarket paper trading with live Polymarket market data and sandbox execution only.

**Architecture:** Reuse the existing Nautilus Polymarket weather ensemble paper daemon shape: Gamma for market discovery metadata, CLOB/Nautilus Polymarket data client for live prices, and `SandboxExecutionClientConfig` for execution. Add a thin adapter that imports or vendors the `research/weather` probability engine, maps London market lines to `P(EGLC final_daily_high >= line)`, and emits `WeatherEnsembleCandidate`-compatible candidates for the existing Nautilus strategy path.

**Tech Stack:** Python, NautilusTrader Polymarket adapter, Nautilus sandbox execution, Docker Compose workspace/papertrade containers, `research/weather` probability models, pytest.

---

## Non-Negotiable Gates

1. **Live data gate:** Live Polymarket reads require an explicit operator readiness flag. If that flag is absent, run against cached or synthetic fixtures only.
2. **Paper-only gate:** Use live Polymarket data client plus Nautilus sandbox execution. Do not configure `PolymarketLiveExecClientFactory` and do not submit real orders.
3. **Resolution gate:** Only trade London markets whose rules resolve against London City Airport / Wunderground `EGLC` or another explicitly validated source. If rules are missing or conflict with the model target, skip.
4. **Market data gate:** Require condition ID, YES/NO token IDs, CLOB quote/book availability, timestamp, bid/ask, spread, and sizes before a candidate can be accepted.
5. **Risk gate:** Start with tiny sandbox stakes: max `$1` per market, max one open position per market family, max `$5` total deployed for the first run.

## Implementation Shape

Add a new runner rather than mutating the broad existing weather ensemble daemon:

- `examples/live/polymarket/polymarket_london_weather_model_paper_daemon.py`
- `examples/live/polymarket/london_weather_model_bridge.py`
- `tests/unit_tests/examples/test_polymarket_london_weather_model_bridge.py`
- `tests/unit_tests/examples/test_polymarket_london_weather_model_paper_daemon.py`

The bridge should produce the same candidate fields as `weather_ensemble_strategy_library.WeatherEnsembleCandidate`, so the existing `WeatherEnsemblePaperStrategy` can be reused.

## Task 1: Execution Preflight and Readiness Gate

**Files:**
- Create: `examples/live/polymarket/polymarket_london_weather_preflight.py`
- Test: `tests/unit_tests/examples/test_polymarket_london_weather_preflight.py`
- Modify: `.docker/docker-compose.yml` only if needed to mount or expose `research/weather` read-only

**Step 1: Write failing tests**

Test that preflight blocks execution unless every dependency needed for the real paper run is ready:

- `live_data_status="blocked"` when an explicit `POLYMARKET_LIVE_DATA_READY=yes` env var is absent.
- `live_data_status="blocked"` when `POLYMARKET_FORCE_LIVE_EXECUTION` is set to any truthy value.
- `execution_mode="sandbox_only"` always.
- no private key is required for preflight market data discovery or sandbox execution.
- the `research/weather` model package is importable from the Nautilus runtime path, or the preflight returns `model_status="blocked"` with the exact missing path.
- the preflight can build a deterministic Family B live probability snapshot for at least one fixture market line, or returns `model_snapshot_status="blocked"`.
- London market discovery returns only markets with `city == "London"`, `metric == "high"`, condition ID, YES/NO token IDs, and accepting-orders metadata.
- resolution metadata explicitly names London City Airport / Wunderground `EGLC`, or the market is rejected with `resolution_status="blocked"`.
- exact-bucket markets are rejected with `unsupported_exact_bucket` until adjacent-line bucket probability is implemented.
- CLOB quote readiness requires bid, ask, spread, sizes, and quote timestamp; missing fields produce `market_data_status="blocked"`.
- the preflight returns a single JSON object containing all statuses and a final `ready_for_paper_round` boolean.

**Step 2: Implement preflight**

Implement a single preflight script that:

- prints current mode without secrets
- checks `POLYMARKET_LIVE_DATA_READY=yes`
- checks that `POLYMARKET_FORCE_LIVE_EXECUTION` is not set
- checks the `research/weather` import path before any network call
- loads the minimal Family B code needed to produce `P(EGLC final_daily_high >= line)` for target-date/high-temperature markets
- constructs a deterministic probability snapshot from either current live forecast inputs or a named local fixture
- discovers London highest-temperature markets through Gamma metadata only after live-data readiness is explicit
- fetches or validates market rules/description and rejects markets whose resolution source is not EGLC-aligned
- checks CLOB quote/book readiness with harmless GET requests only when live-data readiness is explicit
- verifies candidate side mapping without placing an order:
  - YES candidate entry price is YES ask
  - NO candidate entry price is NO ask when available, otherwise use `1 - YES bid` only for confirmed binary YES/NO markets
  - no candidate is accepted when token IDs or quote semantics are ambiguous
- exits non-zero if any hard gate blocks the paper round

**Step 3: Verify**

Run:

```bash
docker compose -f .docker/docker-compose.yml exec workspace uv run pytest \
  tests/unit_tests/examples/test_polymarket_london_weather_preflight.py -q
```

Expected: pass.

**Step 4: Run preflight before any paper daemon work**

Run:

```bash
docker compose -f .docker/docker-compose.yml exec workspace uv run python \
  examples/live/polymarket/polymarket_london_weather_preflight.py
```

Expected: blocked unless operator live-data readiness and all engineering prerequisites are present. Do not continue to Task 6 unless `ready_for_paper_round` is true.

**Step 5: Fix runtime model path if blocked**

If preflight reports the `research/weather` package is unavailable from the Nautilus container, prefer one of these fixes in order:

1. Mount `../research/weather` read-only into the relevant Docker service at `/workspace_research/weather`.
2. Set an explicit `WEATHER_RESEARCH_PATH=/workspace_research/weather` env var and add that path to `sys.path` inside the preflight/bridge.
3. If Docker mounting is not feasible, vendor only a narrow probability-snapshot adapter into `nautilus/examples/live/polymarket/`.

Do not use `/workspace/../research/weather`; in this compose stack `/workspace` is the mounted `nautilus/` repo and that path is not a reliable mounted dependency.

**Step 6: Fix live probability snapshot if blocked**

If the preflight cannot build a live Family B probability snapshot, implement that before the bridge:

- inputs: target local date, market line, decision timestamp, latest available forecast high, forecast source metadata
- output: rows containing `target_local_date`, `forecast_horizon_days`, `market_line`, `model_version`, `predicted_probability`, `raw_predicted_probability`, and `training_row_count`
- default model: `family_b_forecast_error_calibrated_v1`
- failure mode: block the paper round, do not fall back to climatology-only trading

This preflight task must leave behind enough code for the actual bridge to call the same snapshot builder.

## Task 2: London Market Discovery Filter

**Files:**
- Create: `examples/live/polymarket/london_weather_market_filter.py`
- Test: `tests/unit_tests/examples/test_polymarket_london_weather_market_filter.py`
- Reuse: `examples/live/polymarket/weather_daily_temperature_resolver.py`

**Step 1: Write failing tests**

Use dict fixtures for London and non-London markets. Assert the filter accepts only:

- `city == "London"`
- `metric == "high"`
- `band_type in {"or_higher", "or_lower"}`
- `active == True`
- `accepting_orders == True`
- YES and NO token IDs present
- condition ID present

Reject exact-band markets until exact-bucket probability is implemented.

**Step 2: Implement filter**

Wrap `resolve_daily_temperature_markets()` and `filter_tradeable_daily_temperature_markets()`, then narrow to London high-temperature markets.

**Step 3: Verify**

Run the new test plus existing resolver tests:

```bash
docker compose -f .docker/docker-compose.yml exec workspace uv run pytest \
  tests/unit_tests/examples/test_polymarket_weather_daily_temperature_resolver.py \
  tests/unit_tests/examples/test_polymarket_london_weather_market_filter.py -q
```

## Task 3: Research Model Bridge

**Files:**
- Create: `examples/live/polymarket/london_weather_model_bridge.py`
- Test: `tests/unit_tests/examples/test_polymarket_london_weather_model_bridge.py`
- Reuse: the validated `WEATHER_RESEARCH_PATH` or vendored snapshot adapter from Task 1; fail closed if neither is available.

**Step 1: Write failing tests**

Use a fake model output and fake London markets. Assert:

- `or_higher` maps directly to `P(high >= line)`.
- `or_lower` maps to `1 - P(high >= next_line)` only if the market rules make that complement valid; otherwise reject.
- model probability is clipped to `[0, 1]`.
- YES edge is `model_yes_probability - yes_ask`.
- NO edge is `(1 - model_yes_probability) - no_ask` when NO ask is available.
- fallback NO edge is `yes_bid - model_yes_probability` only for confirmed binary YES/NO markets where `no_ask` is unavailable.
- candidates below `min_edge` are rejected.
- unsupported exact-bucket markets are rejected, not approximated.

**Step 2: Implement bridge**

Add a function:

```python
def build_london_model_candidates(markets, model_snapshot, *, min_edge: float) -> list[WeatherEnsembleCandidate]:
    ...
```

The first version should use the Task 1 Family B calibrated probability snapshot for future target dates. Same-day Family A should remain out of scope unless its schema is fixed.

**Step 3: Verify**

Run:

```bash
docker compose -f .docker/docker-compose.yml exec workspace uv run pytest \
  tests/unit_tests/examples/test_polymarket_london_weather_model_bridge.py -q
```

## Task 4: Paper Daemon Runner

**Files:**
- Create: `examples/live/polymarket/polymarket_london_weather_model_paper_daemon.py`
- Test: `tests/unit_tests/examples/test_polymarket_london_weather_model_paper_daemon.py`
- Reuse: `examples/live/polymarket/weather_ensemble_live_strategy.py`
- Reuse: `examples/live/polymarket/weather_ensemble_strategy_library.py`

**Step 1: Write failing tests**

Mock market discovery, model bridge, and `TradingNode`. Assert:

- the node config uses `PolymarketLiveDataClientFactory`
- the execution client is `SandboxExecutionClientConfig`
- the fee model is `nautilus_trader.adapters.polymarket.fee_model.PolymarketFeeModel`
- no live execution client factory is added
- strategy configs include YES/NO token IDs, selected side, edge, and model probability
- max rounds and output JSONL work

**Step 2: Implement runner**

Copy the minimal structure from `polymarket_weather_ensemble_paper_daemon.py`, but make it London-only and model-bridge-only. Defaults:

- `--max-rounds 1`
- `--poll-interval 300`
- `--min-edge 0.08`
- `--target-usd-per-market 1`
- `--max-total-open-stake 5`
- `--output-dir /workspace/outputs`

**Step 3: Verify**

Run:

```bash
docker compose -f .docker/docker-compose.yml exec workspace uv run pytest \
  tests/unit_tests/examples/test_polymarket_london_weather_model_paper_daemon.py -q
```

## Task 5: Compose Entrypoint

**Files:**
- Modify: `.docker/docker-compose.yml`

**Step 1: Add service**

Add a `london-weather-paper` service under the `polymarket` profile. It should run:

```bash
python /workspace/examples/live/polymarket/polymarket_london_weather_model_paper_daemon.py \
  --max-rounds ${POLYMARKET_LONDON_WEATHER_MAX_ROUNDS:-1} \
  --min-edge ${POLYMARKET_LONDON_WEATHER_MIN_EDGE:-0.08} \
  --target-usd-per-market ${POLYMARKET_LONDON_WEATHER_TARGET_USD:-1}
```

Mount outputs writable and source read-only, matching existing papertrade patterns.

**Step 2: Verify config**

Run:

```bash
docker compose -f .docker/docker-compose.yml config london-weather-paper
```

Expected: service resolves and command points at the new runner.

## Task 6: Dry Smoke Run

**Files:**
- No source changes unless failures expose bugs.

**Step 1: Run preflight**

```bash
docker compose -f .docker/docker-compose.yml exec workspace uv run python \
  examples/live/polymarket/polymarket_london_weather_preflight.py
```

Expected: `ready_for_paper_round=true`. If blocked, stop here and report the blocking status; do not run the paper daemon.

**Step 2: Run one paper round only when preflight passes**

```bash
docker compose -f .docker/docker-compose.yml --profile polymarket run --rm london-weather-paper
```

Expected:

- one bounded round
- JSONL output in `outputs/polymarket/runs/`
- no real execution client
- sandbox orders only if London market candidates pass all gates

**Step 3: Inspect output**

Check the JSONL rows for:

- `market_slug`
- `condition_id`
- `yes_token_id`
- `no_token_id`
- `model_yes_probability`
- `market_yes_price`
- `edge`
- `selected_side`
- `entry_price`
- `stake`
- `accounting_status`

## Rollback

If live data access is blocked or no London markets are active, leave the runner dormant and run bridge tests against fixtures only. Do not broaden to other cities or live execution to make the run appear successful.

## Operator Note — Running Task 6 on Atlas via OGMA Egress (verified 2026-07-04)

Polymarket is IP-blocked from Atlas at the DNS level (both hosts resolve to an
Azure block IP). The working egress is a targeted sshuttle sidecar through
`ssh ogma`, defined in `.docker/docker-compose.ogma-tunnel.yml` (see its header
for full usage). OGMA is a fragile production t3.micro: it carries only this
thin market-data stream; never install anything on it or widen the routed CIDRs.

From `~/EL/nautilus` on Atlas:

```bash
# 0. One-time: the Family B replay artifact lives under the EL data root, so
#    the override bind-mounts ~/EL/data/weather/replays into the research mount.
#    The nested mountpoint must exist:
mkdir -p ~/EL/research/weather/data/replays

# 1. Pinned Cloudflare IPs: defaults are baked into the override file. If they
#    rot, re-resolve FROM OGMA (local DNS is poisoned) and export the
#    POLYMARKET_*_IP / POLYMARKET_TUNNEL_CIDRS overrides:
ssh ogma "getent ahostsv4 gamma-api.polymarket.com | head -1"

# 2. Tunnel up (healthcheck = HTTP 200 from Gamma through the tunnel):
docker compose -f .docker/docker-compose.yml -f .docker/docker-compose.ogma-tunnel.yml \
  --profile polymarket up -d ogma-tunnel

# 3. Preflight (expect blocked without the flag; green with it):
docker compose -f .docker/docker-compose.yml -f .docker/docker-compose.ogma-tunnel.yml \
  --profile polymarket run --rm -e POLYMARKET_LIVE_DATA_READY=yes \
  london-weather-paper python /workspace/examples/live/polymarket/polymarket_london_weather_preflight.py

# 4. One paper round (compose defaults: --max-rounds 1, min-edge 0.08,
#    $1/market, $5 total; sandbox execution only):
docker compose -f .docker/docker-compose.yml -f .docker/docker-compose.ogma-tunnel.yml \
  --profile polymarket run --rm -e POLYMARKET_LIVE_DATA_READY=yes london-weather-paper

# 5. Teardown (tunnel iptables rules live only in the sidecar's netns):
docker compose -f .docker/docker-compose.yml -f .docker/docker-compose.ogma-tunnel.yml \
  --profile polymarket rm -sf ogma-tunnel
```

Notes:

- `POLYMARKET_FORCE_LIVE_EXECUTION` must never be set; the compose service
  pins it empty.
- sshuttle runs with `--no-latency-control`; without it the tunnel throttles
  to ~90 KB/s and Gamma event pages (~4 MB) exceed the discovery HTTP timeout,
  which surfaces as an empty market-discovery result (the fetch helper retries
  and then swallows `HttpTimeoutError`).
- The Gamma/CLOB hostname pins live in the tunnel service's `extra_hosts`;
  the daemon shares the tunnel's network namespace and inherits its
  `/etc/hosts`, so do not add `extra_hosts` to `london-weather-paper` (docker
  rejects it alongside `network_mode`).

## Open Decisions

1. Whether to import `research/weather` directly from Nautilus Docker or vendor a narrow probability service boundary.
2. Whether to support same-day Family A after its schema is fixed.
3. Whether exact bucket markets should be supported by deriving bucket probabilities from adjacent cumulative lines.
