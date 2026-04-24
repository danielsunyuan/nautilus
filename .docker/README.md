# Docker services

## Workspace container

Use the `workspace` service as the default development environment. This avoids
host-side dependency drift and keeps Rust, Python, and `uv` tooling inside a
reproducible container.

### Start the full development stack

From the repo root:

```bash
docker compose -f .docker/docker-compose.yml up -d
```

### Open a shell in the workspace container

```bash
docker compose -f .docker/docker-compose.yml exec workspace bash
```

### Run commands inside the workspace container

```bash
docker compose -f .docker/docker-compose.yml exec workspace make install-debug
docker compose -f .docker/docker-compose.yml exec workspace make pytest
docker compose -f .docker/docker-compose.yml exec workspace uv run python -c "import nautilus_trader"
```

## Polymarket papertrade

Use the `papertrade` service when you want live Polymarket market data with
execution simulated inside Nautilus. By default, `papertrade` and
`papertrade-daemon` use the normal Docker network and reach Polymarket directly.
Use this default path for deployments where no VPN is required.

These services use a prebuilt Nautilus runner image (built from
`.docker/nautilus_trader.dockerfile`), not the mutable development workspace.

### Run the paper-trading example

```bash
docker compose -f .docker/docker-compose.yml run --rm papertrade
```

### Run through NordVPN

VPN routing is opt-in. Use the `vpn` profile and the `papertrade-vpn` or
`papertrade-daemon-vpn` services when this machine needs Polymarket traffic to
leave through the `nordvpn` sidecar:

```bash
docker compose -f .docker/docker-compose.yml --profile vpn up -d nordvpn
docker compose -f .docker/docker-compose.yml --profile vpn run --rm papertrade-vpn
```

The VPN services share the `nordvpn` network namespace via
`network_mode: "service:nordvpn"`. The Nautilus runner code is the same; only
the container network path changes.

**5-minute crypto Up/Down (BTC, ETH, …)** — resolves the active round slug
(`btc-updown-5m-{unix}` etc.) from Gamma, then runs the same sandbox **ExecTester**
stack against the **Up** or **Down** token:

```bash
docker compose -f .docker/docker-compose.yml run --rm papertrade \
  python /workspace/examples/live/polymarket/polymarket_crypto_5m_paper_smoke.py --asset BTC
```

When VPN routing is required, use the matching opt-in service:

```bash
docker compose -f .docker/docker-compose.yml --profile vpn run --rm papertrade-vpn \
  python /workspace/examples/live/polymarket/polymarket_crypto_5m_paper_smoke.py --asset BTC
```

Use `--side down` for the Down leg. Slug helpers for `PolymarketInstrumentProviderConfig.event_slug_builder`
live in `examples/live/polymarket/slug_builders.py` (`build_btc_updown_5m_slugs`, `build_crypto_updown_5m_slugs`).

The default `papertrade` service runs
`examples/live/polymarket/polymarket_paper_tester.py`.

The paper-trading script uses live Polymarket data with sandbox execution, so it
does not submit live venue orders.

Because `papertrade` uses the prebuilt runner image, it does not run
`make install-debug` on every start. Rebuild the image when you need package-
level Nautilus changes reflected in the runner:

```bash
docker compose -f .docker/docker-compose.yml build papertrade
```

The compose file mounts repo-root `outputs/` into `/workspace/outputs` as a
writable path for papertrade and daemon JSONL/report artifacts while keeping the
rest of the `papertrade` repo mount read-only.
The `papertrade` and `papertrade-daemon` services also default to running as
`${UID:-1000}:${GID:-1000}` so generated artifacts stay writable on the host.

## Redis-backed persistence

The compose stack also starts a local `redis` service for Nautilus cache and
message-bus persistence. That gives each papertrade run a durable inspection
surface while the Redis container remains up, which is useful for reviewing
fills, portfolio state, and published stream data after a run.
Papertrade results can therefore be inspected after the runner exits, as long
as the Redis service and its volume remain available.

The default Polymarket papertrade runner uses a stable trader identity
(`PAPER-001`) so each strategy instance can keep its Redis namespace separate.
Each run also gets its own Nautilus instance ID in Redis, so stale sandbox state
is not reloaded into later papertrade sessions. If you run multiple algos, keep
the trader IDs distinct.

### Inspect persisted state

Open a Redis shell:

```bash
docker compose -f .docker/docker-compose.yml exec redis redis-cli
```

List trader-scoped keys and streams:

```bash
SCAN 0 MATCH 'trader-*'
```

Inspect a stream once you have its key:

```bash
XINFO STREAM <stream-key>
XRANGE <stream-key> - +
```

If you want to narrow the scan to one algo, match on the trader ID:

```bash
SCAN 0 MATCH '*PAPER-001*'
```

For a GUI, use Redis Insight to browse the keyspace and stream payloads.

## Polymarket 5m paper-trading daemon

Use the `papertrade-daemon` service when you want the BTC 5-minute paper
workflow to roll from one round to the next until stopped, while persisting
JSONL run output under `outputs/polymarket/runs/`. The default daemon uses the
normal Docker network. Use `papertrade-daemon-vpn` with `--profile vpn` only
when this host needs NordVPN routing.

### Start the daemon

```bash
docker compose -f .docker/docker-compose.yml up -d papertrade-daemon
```

Start the daemon through NordVPN:

```bash
docker compose -f .docker/docker-compose.yml --profile vpn up -d papertrade-daemon-vpn
```

### Follow daemon logs

```bash
docker compose -f .docker/docker-compose.yml logs -f papertrade-daemon
```

For the VPN daemon, follow `papertrade-daemon-vpn`.

### Stop the daemon

```bash
docker compose -f .docker/docker-compose.yml stop papertrade-daemon
```

For the VPN daemon, stop `papertrade-daemon-vpn`.

The daemon defaults to:

- asset `BTC`
- preset set `research`
- output path prefix `outputs/polymarket/runs/`

Override the command when you want bounded smoke runs or a different preset set:

```bash
docker compose -f .docker/docker-compose.yml run --rm papertrade-daemon \
  python /workspace/examples/live/polymarket/polymarket_crypto_5m_paper_daemon.py \
  --asset BTC \
  --preset-set quant \
  --max-rounds 1
```

### Keep `RESULTS.md` updated

The paper daemon writes JSONL rows continuously. Run the report refresher next
to it when you want `outputs/polymarket/reports/RESULTS.md` and
`summary_latest.json` rebuilt automatically from those JSONL files:

```bash
docker compose -f .docker/docker-compose.yml up -d crypto-results-reporter
```

The refresher defaults to every 60 seconds. Override it when starting Compose:

```bash
REPORT_REFRESH_INTERVAL_SECONDS=30 docker compose -f .docker/docker-compose.yml up -d crypto-results-reporter
```

## BTC Microstructure Paper Trading

Isolated microstructure strategy family for BTC 5-minute markets. Uses external
BTC candle data (RSI, momentum, VWAP, SMA crossover) plus Polymarket order book
skew to generate trading signals.

### One-Round Smoke Test

```bash
docker compose -f .docker/docker-compose.yml run --rm papertrade \
  python examples/live/polymarket/polymarket_crypto_5m_microstructure_paper_daemon.py \
    --preset-set microstructure_baseline \
    --max-rounds 1 \
    --output-dir /workspace/outputs
```

### Overnight Daemon

```bash
docker compose -f .docker/docker-compose.yml --profile vpn up -d \
  btc-microstructure-daemon-vpn \
  btc-microstructure-results-reporter
```

### Check Results

```bash
cat outputs/polymarket/reports/BTC_MICROSTRUCTURE_RESULTS.md
```

### View Logs

```bash
docker logs -f nautilus-btc-microstructure-daemon-vpn
```

## Weather Ensemble Paper Strategy

Isolated forecast-driven paper strategy family for Polymarket daily temperature
markets. Writes its own JSONL ledger and markdown report under the weather
ensemble namespace.

### One-Round Smoke Test

```bash
docker compose -f .docker/docker-compose.yml run --rm papertrade \
  python /workspace/examples/live/polymarket/polymarket_weather_ensemble_paper_daemon.py \
    --preset-set weather_ensemble_baseline \
    --max-rounds 1 \
    --output-dir /workspace/outputs
```

### Overnight Daemon

```bash
docker compose -f .docker/docker-compose.yml up -d weather-ensemble-daemon-vpn
```

### Settlement Poller

```bash
docker compose -f .docker/docker-compose.yml up -d weather-ensemble-settlement-vpn
```

### Refresh And Inspect Report

```bash
docker compose -f .docker/docker-compose.yml run --rm papertrade \
  python /workspace/examples/live/polymarket/polymarket_weather_ensemble_reporting.py \
    --report-root /workspace/outputs \
    --report-md /workspace/WEATHER_ENSEMBLE_RESULTS.md

docker compose -f .docker/docker-compose.yml exec workspace bash -lc 'ls -1t outputs/polymarket/weather_ensemble | head'
docker compose -f .docker/docker-compose.yml exec workspace bash -lc 'sed -n "1,220p" WEATHER_ENSEMBLE_RESULTS.md'
```

Generated artifacts:

- `outputs/polymarket/weather_ensemble/weather_ensemble_*.jsonl`
- `outputs/polymarket/weather_ensemble/weather_ensemble_settlement.jsonl`
- `WEATHER_ENSEMBLE_RESULTS.md`

## Postgres (local testing)

Postgres integration tests run on Linux when a Postgres instance is available.

### Start Postgres and init schema

From the repo root:

```bash
make init-services
```

This starts the Postgres container (from this `docker-compose.yml`), waits for it, and applies the schema (`schema/sql/types.sql`, `tables.sql`, `functions.sql`, `partitions.sql`).

Credentials (default): user `nautilus`, password `pass`, database `nautilus`, port `5432`.

### Run Postgres tests

**Python:**

```bash
make test-postgres
```

Requires `make init-services` (or at least `make start-services` then `make init-db`) to have been run first.

**Rust:**

```bash
POSTGRES_HOST=localhost POSTGRES_PORT=5432 POSTGRES_USERNAME=nautilus POSTGRES_PASSWORD=pass POSTGRES_DATABASE=nautilus \
  cargo test -p nautilus-infrastructure --features postgres -- --test-threads=1
```

### Start Postgres only (no schema)

```bash
docker compose -f .docker/docker-compose.yml up -d postgres
```

Then from repo root: `make init-db` to apply the schema.

### Stop / purge

- `make stop-services` — stop containers (data preserved).
- `make purge-services` — stop and remove volumes.
