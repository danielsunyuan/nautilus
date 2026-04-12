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
This service is a prebuilt Nautilus runner image, not the mutable development
workspace.

### Start the VPN sidecar

```bash
docker compose -f .docker/docker-compose.yml up -d nordvpn
```

### Run the paper-trading example

```bash
docker compose -f .docker/docker-compose.yml run --rm papertrade
```

This service shares the `nordvpn` network namespace via
`network_mode: "service:nordvpn"` and runs
`examples/live/polymarket/polymarket_paper_tester.py`.

The paper-trading script uses live Polymarket data with sandbox execution, so it
does not submit live venue orders.

Because `papertrade` uses the prebuilt `.docker/nautilus_trader.dockerfile`
image, it does not run `make install-debug` on every start. Rebuild the image
when you need package-level Nautilus code changes reflected in the runner:

```bash
docker compose -f .docker/docker-compose.yml build papertrade
```

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
