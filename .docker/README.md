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

## Polymarket papertrade with NordVPN

Use the `papertrade` service when you want live Polymarket market data routed
through the `nordvpn` sidecar while keeping execution simulated inside Nautilus.
This service uses a prebuilt Nautilus runner image (built from
`.docker/nautilus_trader.dockerfile`), not the mutable development workspace.

### Start the VPN sidecar

```bash
docker compose -f .docker/docker-compose.yml up -d nordvpn
```

### Run the paper-trading example

```bash
docker compose -f .docker/docker-compose.yml run --rm papertrade
```

**5-minute crypto Up/Down (BTC, ETH, …)** — resolves the active round slug
(`btc-updown-5m-{unix}` etc.) from Gamma, then runs the same sandbox **ExecTester**
stack against the **Up** or **Down** token:

```bash
docker compose -f .docker/docker-compose.yml run --rm papertrade \
  python /workspace/examples/live/polymarket/polymarket_crypto_5m_paper_smoke.py --asset BTC
```

Use `--side down` for the Down leg. Slug helpers for `PolymarketInstrumentProviderConfig.event_slug_builder`
live in `examples/live/polymarket/slug_builders.py` (`build_btc_updown_5m_slugs`, `build_crypto_updown_5m_slugs`).

This service shares the `nordvpn` network namespace via
`network_mode: "service:nordvpn"` and runs
`examples/live/polymarket/polymarket_paper_tester.py`.

The paper-trading script uses live Polymarket data with sandbox execution, so it
does not submit live venue orders.

Because `papertrade` uses the prebuilt runner image, it does not run
`make install-debug` on every start. Rebuild the image when you need package-
level Nautilus changes reflected in the runner:

```bash
docker compose -f .docker/docker-compose.yml build papertrade
```

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
