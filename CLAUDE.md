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

**Confirmed-entry daemon** (`weather_confirmed_entry_daemon.py`): A fourth component that runs independently, polling Wunderground every 300–900s. Executes confirmed-outcome trades (A1: or_higher YES, A2: exact-band NO, B2: late-day NO). Output: `weather_confirmed_live_*.jsonl` — picked up automatically by settlement poller and take-profit watcher.

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

### Known resolution sources — Polymarket weather markets

Three oracle types are used across the 50 active cities (confirmed from Gamma market descriptions, April 2026):

- **WU** (46 cities): Weather Underground daily history — `wunderground.com/history/daily/{cc}/{city}/{STATION}`
- **NOAA** (3 cities): NOAA timeseries — `weather.gov/wrh/timeseries?site={STATION}`
- **HKO** (1 city): Hong Kong Observatory — `weather.gov.hk/en/cis/climat.htm`

Live temperature data is fetched via:
- WU cities + HKO + Moscow + Tel Aviv: TWC internal API — `api.weather.com/v1/location/{STATION}:9:{ISO2}/observations/historical.json?apiKey=...&units={e|m}&startDate=YYYYMMDD`
- Istanbul LTFM only: Iowa State ASOS CSV — `mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=LTFM&data=tmpc&...`

See `weather_wunderground_fetcher.py` for the complete implementation (50/50 cities verified).

**Critical station corrections** (naive ICAO assumptions are wrong):

| City | Correct Station | Wrong assumption | Oracle |
|---|---|---|---|
| Denver | **KBKF** (Buckley AFB, Aurora) | KDEN | WU |
| Jakarta | **WIHH** (Halim Perdanakusuma) | WIII | WU |
| Lagos | **DNMM** (Murtala Muhammed) | DNBE | WU |
| Paris | **LFPB** (Le Bourget, ~2026) | LFPG | WU |
| Taipei | **RCSS** (Songshan Airport, ~2026) | RCTP | WU |
| Moscow | **UUWW** (Vnukovo) | UUDD | NOAA |

**Complete 50-city station map:**

| City | Station | ISO | Unit | Oracle |
|---|---|---|---|---|
| NYC | KLGA | US | °F | WU |
| Chicago | KORD | US | °F | WU |
| Miami | KMIA | US | °F | WU |
| Los Angeles | KLAX | US | °F | WU |
| San Francisco | KSFO | US | °F | WU |
| Seattle | KSEA | US | °F | WU |
| Denver | KBKF | US | °F | WU |
| Houston | KHOU | US | °F | WU |
| Dallas | KDAL | US | °F | WU |
| Austin | KAUS | US | °F | WU |
| Atlanta | KATL | US | °F | WU |
| London | EGLC | GB | °C | WU |
| Paris | LFPB | FR | °C | WU |
| Madrid | LEMD | ES | °C | WU |
| Amsterdam | EHAM | NL | °C | WU |
| Munich | EDDM | DE | °C | WU |
| Milan | LIMC | IT | °C | WU |
| Warsaw | EPWA | PL | °C | WU |
| Helsinki | EFHK | FI | °C | WU |
| Ankara | LTAC | TR | °C | WU |
| Tokyo | RJTT | JP | °C | WU |
| Seoul | RKSI | KR | °C | WU |
| Busan | RKPK | KR | °C | WU |
| Taipei | RCSS | TW | °C | WU |
| Singapore | WSSS | SG | °C | WU |
| Kuala Lumpur | WMKK | MY | °C | WU |
| Jakarta | WIHH | ID | °C | WU |
| Manila | RPLL | PH | °C | WU |
| Beijing | ZBAA | CN | °C | WU |
| Shanghai | ZSPD | CN | °C | WU |
| Shenzhen | ZGSZ | CN | °C | WU |
| Guangzhou | ZGGG | CN | °C | WU |
| Chongqing | ZUCK | CN | °C | WU |
| Chengdu | ZUUU | CN | °C | WU |
| Wuhan | ZHHH | CN | °C | WU |
| Lucknow | VILK | IN | °C | WU |
| Karachi | OPKC | PK | °C | WU |
| Jeddah | OEJN | SA | °C | WU |
| Lagos | DNMM | NG | °C | WU |
| Cape Town | FACT | ZA | °C | WU |
| Buenos Aires | SAEZ | AR | °C | WU |
| Sao Paulo | SBGR | BR | °C | WU |
| Mexico City | MMMX | MX | °C | WU |
| Toronto | CYYZ | CA | °C | WU |
| Panama City | MPMG | PA | °C | WU |
| Wellington | NZWN | NZ | °C | WU |
| Istanbul | LTFM | TR | °C | NOAA |
| Moscow | UUWW | RU | °C | NOAA |
| Tel Aviv | LLBG | IL | °C | NOAA |
| Hong Kong | VHHH | HK | °C | HKO |

For any new city, always fetch the market ruleset from Gamma to confirm the station code before acting.

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
- **THE_ODDS_API_KEY** — required for CLV gate (`clv-focused` preset set). Free tier: 500 req/month. If absent, CLV gate passes all markets. Obtain at the-odds-api.com.

## Docker Orchestration

### Services (docker-compose.yml)

Two images are used:
- **`nautilus-trader:latest`** — full Rust/Cython build (~20 min); used for NautilusTrader TradingNode services.
- **`nautilus-recorder:latest`** — lightweight pip-installed `nautilus_trader`; used for recorders, signal watchers, exit server. Rebuilds in seconds.

| Service | Container | Image | Purpose |
|---------|-----------|-------|---------|
| `nordvpn` | `nautilus-nordvpn` | `nautilus-nordvpn` | VPN sidecar; publishes port 8080 for exit server |
| `papertrade-daemon-vpn` | `nautilus-crypto-5m-daemon-vpn` | `nautilus-trader:latest` | 5m BTC paper daemon via NordVPN |
| `weather-daemon-vpn` | `nautilus-weather-daemon-vpn` | `nautilus-trader:latest` | Weather paper daemon via NordVPN |
| `weather-live-daemon-vpn` | `nautilus-weather-live-daemon-vpn` | `nautilus-recorder:latest` | Weather live daemon (90c, $50 budget) |
| `weather-confirmed-entry-vpn` | `nautilus-weather-confirmed-vpn` | `nautilus-recorder:latest` | Confirmed-entry daemon (WU temp signal, $20/day budget) |
| `weather-exit-server-vpn` | `nautilus-weather-exit-server` | `nautilus-recorder:latest` | Manual exit HTTP server (port 8080) |
| `weather-settlement-vpn` | `nautilus-weather-settlement-vpn` | `nautilus-trader:latest` | Weather settlement poller (CLOB, 900s) |
| `sports-daemon-vpn` | `nautilus-sports-daemon-vpn` | `nautilus-trader:latest` | Sports paper daemon via NordVPN |
| `sports-settlement-vpn` | `nautilus-sports-settlement-vpn` | `nautilus-trader:latest` | Sports settlement poller (900s) |
| `sports-results-reporter` | `nautilus-sports-results-reporter` | `nautilus-trader:latest` | Sports JSONL→SPORTS_RESULTS.md (60s) |
| `crypto-results-reporter` | `nautilus-crypto-results-reporter` | `nautilus-trader:latest` | BTC JSONL→Markdown reports |
| `recorder-5m-vpn` | `nautilus-recorder-5m-vpn` | `nautilus-recorder:latest` | CLOB 5m order book recorder → Parquet |
| `recorder-multi-tf-vpn` | `nautilus-recorder-multi-tf-vpn` | `nautilus-recorder:latest` | 15m/1h poll recorder → JSONL |
| `signal-watcher-5m-vpn` | `nautilus-signal-watcher-5m-vpn` | `nautilus-recorder:latest` | Live 5m signal watcher → JSONL |
| `redis` | `nautilus-redis` | `redis:7.2-alpine` | Nautilus cache backend |
| `postgres` | `nautilus-database` | `postgres` | Persistent storage |
| `workspace` | `nautilus-workspace` | `nautilus-workspace` | Dev shell (uv, pytest) |

**One-shot utility profiles** (run with `--rm`):

| Service | Profile | Purpose |
|---------|---------|---------|
| `geoblock-check` | `readonly` | Verify VPN is not geo-blocked |
| `balance-monitor` | `monitor` | Show Polymarket wallet balances |
| `execution-check` | `execution` | Geoblock preflight + smoke test |

### Container Image

The compiled Rust/Cython engine is published to GHCR. Strategy code (Python) is volume-mounted from the git repo at runtime — no rebuild needed for Python changes.

**Registry:** `ghcr.io/danielsunyuan/nautilus-trader`
**Source:** `github.com/danielsunyuan/nautilus_trader` (branch: `develop`)

```bash
# Pull the pre-built image (no 20-min compile)
docker pull ghcr.io/danielsunyuan/nautilus-trader:latest
docker tag ghcr.io/danielsunyuan/nautilus-trader:latest nautilus-trader:latest

# Push a new image version (from build machine only)
docker tag nautilus-trader:latest ghcr.io/danielsunyuan/nautilus-trader:latest
docker tag nautilus-trader:latest ghcr.io/danielsunyuan/nautilus-trader:$(date +%Y-%m-%d)
docker push ghcr.io/danielsunyuan/nautilus-trader:latest
docker push ghcr.io/danielsunyuan/nautilus-trader:$(date +%Y-%m-%d)
```

**When to rebuild the image:** Only if Rust/Cython core changes (rare). All strategy code, daemons, and adapters are Python — just `git pull` and restart containers.

```bash
# Full rebuild (only for Rust/Cython changes, ~20 min)
docker compose -f .docker/docker-compose.yml build papertrade
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
# Query Gamma or CLOB from inside any VPN-connected container
docker exec nautilus-weather-live-daemon-vpn python3 -c "
import httpx
r = httpx.get('https://gamma-api.polymarket.com/markets?condition_id=0xABC...')
print(r.json())
"

# Or use the settlement container
docker exec nautilus-weather-settlement-vpn python3 -c "
import httpx
r = httpx.get('https://clob.polymarket.com/midpoint?token_id=TOKEN_ID')
print(r.json())
"
```

All VPN-connected containers route traffic through `nautilus-nordvpn` and have unrestricted Polymarket API access.

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

# Or via the HTTP server inside the nautilus nordvpn network namespace
docker exec nautilus-nordvpn wget -qO- http://127.0.0.1:8080/positions
docker exec nautilus-nordvpn wget -qO- --post-data='{"market_slug":"..."}' \
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
docker logs -f nautilus-crypto-5m-daemon-vpn

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

## Sports Strategy Presets

### Preset Sets

Four preset sets are available, controlled by `--preset-set` flag passed to the daemon:

| Set | Flag | Count | Description |
|-----|------|-------|-------------|
| `all` | `--preset-set all` | 10 | 5 band-only + 5 basic; undifferentiated baseline |
| `focused` | `--preset-set focused` | 10 | 2 per arena × 5 arenas; sport+type whitelisted |
| `depth-focused` | `--preset-set depth-focused` | 10 | Same as focused + `min_bid_ratio=0.55` |
| `clv-focused` | `--preset-set clv-focused` | 10 | Same as focused + `min_clv_edge=0.05` (needs `THE_ODDS_API_KEY`) |

### What `focused` Contains

Two presets per arena (5 arenas = 10 total), `mode="basic"` (spread + liquidity filter):

- `{arena}_focused_tennis_ufc` — `allowed_sports={"tennis", "ufc"}`, all market types
- `{arena}_focused_nba_totals` — `allowed_sports={"nba"}`, `allowed_market_types={"totals"}`

Excluded sports: MLB, NHL/hockey (negative edge in baseline). Excluded NBA market types: moneyline, spreads (negative edge in baseline).

### What `depth-focused` Adds

Copies every `focused` preset via `dataclasses.replace()`, adds `min_bid_ratio=0.55`. Entry requires bid-side book weight ≥ 55% of visible liquidity. Concept ported from BTC `microprice_support` strategy.

### What `clv-focused` Adds

Copies every `focused` preset, adds `min_clv_edge=0.05`. Entry requires Polymarket ask ≥ 5pp below Vegas implied probability. Requires `THE_ODDS_API_KEY`. If Vegas data is unavailable, gate passes through.

**Known limitation (Apr 2026):** `SportsMarket` has no `home_team`/`away_team` fields. `fetch_implied_prob()` always receives empty strings and always returns `None`. `has_clv_edge(vegas_implied=None)` returns `True`, so `clv-focused` is currently operationally identical to `focused`. Do not run `clv-focused` in production until `SportsMarket` is extended.

### Data Baseline (Apr 2026)

Source: ~387 settled sports paper trades collected April 2026 across all 5 arenas.

| Sport + Type | Win Rate | vs. Breakeven | n | Decision |
|---|---|---|---|---|
| Tennis (all) | 77.6% | +5.1pp | 326 | ✅ Whitelist |
| UFC (all) | 90.2% | +13.2pp | 41 | ✅ Whitelist |
| NBA totals | 80.0% | +28.7pp | 20 | ✅ Whitelist |
| NBA spreads | ~40% | −20pp | est. | ❌ Exclude |
| MLB (all) | negative | negative | est. | ❌ Exclude |
| NHL/hockey | negative | negative | est. | ❌ Exclude |

Gate before drawing conclusions: **≥200 unique settled trades** per preset set before evaluating.

### How to Add a New Preset Set

1. Add a new factory function to `sports_strategy_library.py` following the `focused_presets()` / `depth_focused_presets()` pattern.
2. Use `dataclasses.replace()` to copy existing presets (NOT `vars()` — `slots=True` dataclasses have no `__dict__`).
3. Add a route in `_strategy_presets_for_set()` in `polymarket_sports_paper_daemon.py`.
4. Add a new Docker service in `.docker/docker-compose.yml` with `--preset-set <name>` and `outputs/polymarket/sports` volume mount.
5. Add tests in `tests/unit_tests/examples/test_sports_strategy_library.py` verifying count, field values, and whitelist inheritance.
6. Update this section with what the new set contains and its rationale.

## Win Rate Math

```
resolved_win_rate = resolved_wins / (resolved_wins + resolved_losses)
breakeven_win_rate = average_entry_price / 1.00
edge = resolved_win_rate - breakeven_win_rate
```

Unresolved and no-trade rows are **excluded** from the win rate denominator. Minimum 100 resolved trades per arena before drawing conclusions.
