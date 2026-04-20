# CLAUDE.md — Nautilus Workspace Context

## What This Repository Is

A fork/extension of [NautilusTrader](https://nautechsystems.io) — an event-driven algorithmic trading platform — with custom Polymarket paper-trading examples and adapters. The core engine is Rust/Cython; the examples and adapters are Python.

## Must-Read First

- `AGENTS.md` — Docker-first rule, Nautilus runtime protocol, standard entry points. **All agents must follow this.**

## Architecture Boundaries

### Nautilus Core (do not modify without strong justification)

- `nautilus_trader/` — the trading engine (Rust/Cython compiled). Strategies, execution clients, data clients, cache, message bus.
- `nautilus_trader/adapters/polymarket/` — Polymarket adapter (data client, execution client, instrument provider).
- `nautilus_trader/adapters/sandbox/` — Sandbox execution client for paper trading.

### Examples Layer (our working area)

- `examples/live/polymarket/` — runnable paper-trading scripts and supporting modules.
- This is where custom strategies, daemons, resolvers, and reports live.
- Keep example modules self-contained; use `importlib.util` fallback pattern for cross-module imports (the repo has no installed `examples` package).

### Tests

- `tests/unit_tests/examples/` — unit tests for example modules (no network, no compiled extensions required).
- `tests/integration_tests/adapters/polymarket/` — integration tests for the Polymarket adapter (may need compiled Nautilus).

## Nautilus Runtime Protocol (from AGENTS.md)

The flow is always:

1. **Resolve** venue market (slug, token IDs, instrument IDs)
2. **Build** `TradingNode` with data client (live) + execution client (sandbox for paper)
3. **Attach** Strategy actors to the node
4. **Run** the node for the market's duration
5. **Extract** results from Nautilus cache (positions, orders, fills)
6. **Write** artifacts (JSONL, reports)

**Decision logic lives inside the Strategy class.** Orchestration (session discovery, timing, result extraction, JSONL writing) lives in the daemon/runner.

## Two Market Families

### Crypto 5-Minute Markets (existing, proven)

- Recurring 5-minute BTC up/down binary markets.
- Markets resolve within the node's runtime — the daemon runs the node for ~5 minutes, then extracts results from cache.
- Files: `crypto_5m_*`, `polymarket_crypto_5m_*`

### Weather Daily Temperature Markets (new)

- Daily binary markets on city temperature thresholds (e.g., "Will NYC high be >= 70F?").
- Markets resolve **12-36 hours** after entry — the node cannot stay running that long.
- Entry happens via Nautilus strategy (subscribe to quotes, check arena, submit BUY).
- Settlement requires a **separate resolution pass** outside the node runtime.
- Files: `weather_daily_temperature_*`, `polymarket_weather_daily_temperature_*`

**Price arenas:** 50c, 60c, 70c, 80c, 90c — each a 10-cent entry band.
**Strategies:** 5 basic (one per arena) + 3 support (bid-side liquidity filter at 70c/80c/90c).

### Sports Markets (new)

- Polymarket sports binary markets: NBA (moneyline, spreads, totals) + ATP/WTA Tennis + MMA + Boxing.
- Markets resolve within hours of game completion — no long hold like weather.
- Same band-only/basic strategy structure as weather: 10 presets across 5 price arenas.
- Settlement: separate poller queries Gamma by condition_id and matches outcome name.
- Files: `sports_*`, `polymarket_sports_*`
- Output: `outputs/polymarket/sports/sports_{preset_set}_{timestamp}.jsonl`
- Report: `SPORTS_RESULTS.md` (refreshed every 60s by `sports-results-reporter`)
- Plan: `../SPORTS_STRATEGY_PLAN.md`

**Key difference from weather:** 100 markets × 10 presets = 1000 strategy instances per round. Round cadence: 90s node runtime + 300s sleep.

## Settlement Architecture (Weather + Sports Markets)

The weather daemon lifecycle has three phases:

1. **Discovery + Entry** — query Gamma for active temperature markets, enter paper trades via Nautilus sandbox, log entries to JSONL as `strategy_result` with `resolved: false`.
2. **Settlement Polling** — periodically re-query Gamma for resolved markets, match against JSONL entries, write `settlement_update` events with resolution price and P/L.
3. **Reporting** — aggregate JSONL events into Markdown tables (win rate, breakeven, edge, P/L by arena/strategy/city).

Phase 2 is orchestration-only — it does not use Nautilus TradingNode. It reads JSONL + queries Gamma API. This respects the boundary: Nautilus handles order flow, the daemon handles lifecycle.

## CRITICAL: Polymarket Resolution Mechanism

**Every decision involving a Polymarket position — entry, exit, hold, P&L assessment — must account for how that specific market resolves.**

### Resolution is defined by the market's own ruleset

Each market has a ruleset (readable via Gamma API: `GET /markets?condition_id=COND_ID`, field `description` or `resolution_source`). The ruleset names the exact data source Polymarket's oracle will use (e.g., a specific Weather.com feed, a named NOAA station, an ESPN box score, a specific exchange close price).

**That named source is the only authoritative source for resolution.** No other data — wttr.in, Open-Meteo, a different weather station, a different sports feed — is relevant to whether a market resolves YES or NO, even if it is factually accurate by another measure.

### Why this matters in practice

Temperature sensors at different stations kilometres apart can read several degrees differently. A third-party weather API may show 73°F for Austin while the Weather.com station Polymarket's oracle queries shows 68°F — meaning the market resolves YES even though your proxy data said it would lose. Acting on the proxy is the wrong call.

The CLOB mid-price reflects what participants with access to the correct data source believe. A market trading at 0.99 should be treated as near-certain YES from the oracle's perspective unless you can verify the resolution source directly.

### Required workflow before any exit or hold decision

1. Fetch the market ruleset from Gamma inside the VPN container.
2. Identify the named resolution data source.
3. If that source is directly queryable, query it. If not, treat the CLOB mid as the best proxy.
4. **Never substitute a third-party data source** (wttr.in, generic weather APIs, etc.) for the ruleset source.
5. If the resolution source cannot be determined or queried, note the uncertainty explicitly before recommending any action.

### Applies to all market types

- **Weather markets** — resolution source is a specific weather service/station, not any weather API.
- **Sports markets** — resolution source is a specific score/stats provider, not a generic sports feed.
- **Crypto markets** — resolution source is a specific exchange or index, not just any price feed.
- **Any other event market** — always read the ruleset first.

## Fee Model

The sandbox execution client supports pluggable fee models via `SandboxExecutionClientConfig.fee_model_path`.

**Polymarket's actual formula:** `fee = qty × rate × p × (1 - p)` — fees peak at 50c and drop to near-zero at price extremes (0c, 100c). Only takers pay.

**PolymarketFeeModel** (`nautilus_trader/adapters/polymarket/fee_model.py`) implements this correctly. All Polymarket paper-trading daemons must use:
```python
SandboxExecutionClientConfig(
    ...
    fee_model_path="nautilus_trader.adapters.polymarket.fee_model.PolymarketFeeModel",
)
```

**DO NOT** use the default `MakerTakerFeeModel` for Polymarket — it applies a flat % to notional and overcharges by ~34x at high prices (90c+).

## CLOB vs Gamma — Always Prefer CLOB

**CLOB (`clob.polymarket.com`) is the authoritative, real-time source. Gamma (`gamma-api.polymarket.com`) is a delayed, cached read layer on top of CLOB.**

| Use case | Use |
|---|---|
| Current bid/ask, mid-price | CLOB `/midpoint`, `/book` |
| Market resolution (resolved?) | CLOB `/midpoint` — winning token snaps to ≥0.99, losing to ≤0.01 |
| Order submission | CLOB `/order` |
| Market discovery (open markets, slugs, rulesets) | Gamma `/markets` — acceptable since this is metadata, not price data |
| Settlement status | CLOB only — Gamma's `condition_id` lookup returns stale/wrong markets |

**Do not use Gamma for anything price- or resolution-related.** Gamma's `/markets?condition_id=` endpoint is known to return incorrect markets. All settlement and price checks must go through CLOB.

Both CLOB and Gamma are IP-blocked on the WSL2 host — all requests must run inside a VPN-connected container.

## Key Conventions

- **Paper only** — weather daemon uses `SandboxExecutionClientConfig`. Never `PolymarketLiveExecClientFactory`.
- **JSONL source of truth** — all trading events are appended to JSONL files. Reports are derived views.
- **No VPN in code** — NordVPN is Docker/profile config only. Strategy code must not couple to VPN.
- **importlib pattern** — examples import each other via `try/except ModuleNotFoundError` with `importlib.util` fallback.
- **Frozen dataclasses with slots** — all model/preset types follow this pattern.
- **Conservative parsing** — market resolver rejects anything ambiguous; no guessing.

## Docker Orchestration

### Services (docker-compose.yml)

| Service | Container | Image | Purpose |
|---------|-----------|-------|---------|
| `papertrade-daemon-vpn` | `nautilus-papertrade-daemon-vpn` | `nautilus-papertrade:latest` | Live 5m BTC paper daemon via NordVPN |
| `sports-daemon-vpn` | `nautilus-sports-daemon-vpn` | `nautilus-papertrade:latest` | Live sports paper daemon via NordVPN |
| `sports-settlement-vpn` | `nautilus-sports-settlement-vpn` | `nautilus-papertrade:latest` | Sports settlement poller (900s interval) |
| `sports-results-reporter` | `nautilus-sports-results-reporter` | `nautilus-papertrade:latest` | Sports JSONL→SPORTS_RESULTS.md (60s) |
| `nordvpn` | `nautilus-nordvpn` | `nautilus-nordvpn` | VPN sidecar (Tokyo, NORDLYNX) |
| `redis` | `nautilus-redis` | `redis:7.2-alpine` | Nautilus cache backend |
| `postgres` | `nautilus-database` | `postgres` | Persistent storage |
| `workspace` | `nautilus-workspace` | `nautilus-workspace` | Dev shell (uv, pytest) |
| `crypto-results-reporter` | `nautilus-crypto-results-reporter` | `nautilus-papertrade:latest` | BTC JSONL→Markdown reports |

### Rebuild Process

The `nautilus-papertrade:latest` image compiles Nautilus from Rust/Cython source (~20 min full build). For **pure Python changes** (adapters, fee models, configs), patch the image in-place:

```bash
# 1. Stop daemon
docker compose -f .docker/docker-compose.yml --profile vpn stop papertrade-daemon-vpn

# 2. Patch pure Python files into existing image (seconds, not minutes)
SITE="/usr/local/lib/python3.13/site-packages/nautilus_trader"
docker create --name nautilus-patch nautilus-papertrade:latest sleep infinity
docker cp nautilus_trader/adapters/polymarket/fee_model.py nautilus-patch:${SITE}/adapters/polymarket/fee_model.py
# ... copy other changed .py files ...
docker tag nautilus-papertrade:latest nautilus-papertrade:pre-patch
docker commit nautilus-patch nautilus-papertrade:latest
docker rm nautilus-patch

# 3. Restart
docker compose -f .docker/docker-compose.yml --profile vpn up -d papertrade-daemon-vpn
```

For **Rust/Cython changes**, full rebuild is required:
```bash
docker compose -f .docker/docker-compose.yml build papertrade-daemon-vpn
```

### Startup Sequence

1. `postgres` + `redis` start first (no deps)
2. `nordvpn` starts, runs healthcheck until VPN connected
3. `papertrade-daemon-vpn` waits for `nordvpn` healthy + `redis` started
4. Daemon resolves BTC 5m market via Gamma API, builds TradingNode per round
5. Results written to `outputs/polymarket/runs/overnight_{preset}_{timestamp}.jsonl`

**Redis must finish loading before daemon can connect.** If daemon starts while Redis is `LOADING`, restart it after Redis is ready.

## Polymarket API Access — Must Use Docker

`clob.polymarket.com` and `gamma-api.polymarket.com` are **IP-blocked on the host machine** (WSL2). All requests return Azure CDN 404s. Any task that needs live bid/ask prices, order book data, market resolution, or order submission must run inside a VPN-connected container:

```bash
# Query Gamma or CLOB from inside the live daemon container (has VPN)
docker exec polynautilus-polymarket-weather-live-vpn-1 python3 -c "
import httpx
r = httpx.get('https://gamma-api.polymarket.com/markets?condition_id=0xABC...')
print(r.json())
"

# Or use the settlement container (also VPN-connected)
docker exec nautilus-weather-settlement-vpn python3 -c "
import httpx
r = httpx.get('https://clob.polymarket.com/book?token_id=TOKEN_ID')
print(r.json())
"
```

Both containers route traffic through the NordVPN sidecar and have unrestricted Polymarket API access.

## Manual Position Exit

An exit server runs as `nautilus-weather-exit-server` (profile: `vpn execution`). Use it when you observe a position that should be closed before oracle resolution.

```bash
# List all open positions
docker exec nautilus-weather-exit-server \
    python3 /workspace/nautilus/examples/live/polymarket/weather_temperature_exit.py \
    --market-slug "..." --dry-run    # preview first

# Submit the actual sell
docker exec nautilus-weather-exit-server \
    python3 /workspace/nautilus/examples/live/polymarket/weather_temperature_exit.py \
    --market-slug "highest-temperature-in-austin-on-april-20-2026-69forbelow"

# Or via the HTTP server (accessible within VPN network namespace only — NOT from host)
docker exec polynautilus-nordvpn-1 wget -qO- http://127.0.0.1:8080/positions
docker exec polynautilus-nordvpn-1 wget -qO- --post-data='{"market_slug":"..."}' \
    --header='Content-Type: application/json' http://127.0.0.1:8080/exit
```

The HTTP server binds on port 8080 inside the nordvpn network namespace. VPN iptables rules block host→container routing, so `localhost:8080` from the host does not work — use `docker exec` instead.

## Docker Entry Points

```bash
# Dev shell
docker compose -f .docker/docker-compose.yml exec workspace bash

# Run tests
docker compose -f .docker/docker-compose.yml exec workspace uv run --extra polymarket --with pytest python -m pytest tests/unit_tests/examples/ -q

# Paper trader (crypto 5m, via VPN)
docker compose -f .docker/docker-compose.yml --profile vpn up -d papertrade-daemon-vpn

# Follow daemon logs
docker logs -f nautilus-papertrade-daemon-vpn

# Paper trader (one-shot, no VPN)
docker compose -f .docker/docker-compose.yml run --rm papertrade
```

## Test Commands (host fallback when workspace unavailable)

```bash
cd /home/atlas/EL/nautilus

# Weather models + resolver + strategy + report (no compiled extensions needed)
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_polymarket_weather_daily_temperature_models.py \
  tests/unit_tests/examples/test_polymarket_weather_daily_temperature_resolver.py \
  tests/unit_tests/examples/test_polymarket_weather_daily_temperature_strategy.py \
  tests/unit_tests/examples/test_polymarket_weather_daily_temperature_report.py \
  -q

# Daemon tests (need --noconftest to avoid compiled extension imports)
uv run --extra polymarket --with pytest --with pytest-asyncio python -m pytest \
  tests/unit_tests/examples/test_polymarket_weather_daily_temperature_daemon.py \
  --noconftest -q
```

## Win Rate Math

```
resolved_win_rate = resolved_wins / (resolved_wins + resolved_losses)
breakeven_win_rate = average_entry_price / 1.00
edge = resolved_win_rate - breakeven_win_rate
```

Unresolved and no-trade rows are **excluded** from the win rate denominator. Minimum 100 resolved trades per arena before drawing conclusions.
