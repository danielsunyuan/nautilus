# Agent instructions (read first)

This repository is worked from a Dockerized development environment.

## Docker-first rule

- Start services from the repo root with `docker compose -f .docker/docker-compose.yml up -d`.
- Use the `nautilus-workspace` container for development commands.
- Prefer `docker compose -f .docker/docker-compose.yml exec workspace <command>` over running Python, `uv`, `cargo`, `make`, or test commands on the host.
- Host-side Docker commands are allowed for container lifecycle only (`docker compose`, `docker ps`, `docker logs`, `docker exec`).
- If the workspace container is missing, build and start it first instead of falling back to host execution.

## Standard entry points

- Open a shell: `docker compose -f .docker/docker-compose.yml exec workspace bash`
- Run Python tooling: `docker compose -f .docker/docker-compose.yml exec workspace uv run ...`
- Run Make targets: `docker compose -f .docker/docker-compose.yml exec workspace make ...`
- Run tests: `docker compose -f .docker/docker-compose.yml exec workspace make pytest`
- Run the Polymarket paper trader: `docker compose -f .docker/docker-compose.yml run --rm papertrade`

## Notes

- The compose stack also provides `postgres`, `redis`, and `pgadmin`.
- `workspace` is the mutable dev shell built from `.docker/DockerfileUbuntu`.
- `papertrade` is a prebuilt runner image built from `.docker/nautilus_trader.dockerfile`.
- If Nautilus package code changes and you need those changes in `papertrade`, rebuild that image before running it.
- Papertrade results are inspected through the bundled `redis` service when cache/message-bus persistence is enabled; use `docker compose -f .docker/docker-compose.yml exec redis redis-cli`, `SCAN 0 MATCH 'trader-*'`, then `XINFO STREAM` or `XRANGE` on the stream key.
- Keep one stable `TraderId` per algo so Redis keys and streams stay partitioned across concurrent papertrade runs, and keep `use_instance_id=True` so each run gets a fresh namespace instead of reloading stale sandbox state.
- Credentials remain in local `.env*` files and are injected into the workspace container through Compose; never print secret values.

## Nautilus runtime protocol

- Follow the Nautilus runtime model rather than building direct exchange scripts when adding execution behavior.
- The expected flow is: resolve venue market -> map to `InstrumentId` -> build `TradingNode` -> attach strategy actors -> route orders through an execution client -> read results from Nautilus cache / positions / orders.
- Prefer small, explicit `Strategy` classes over ad hoc loops that place orders outside the strategy lifecycle.
- Use Polymarket live data clients for venue data and Nautilus sandbox execution for paper trading unless a later approved leg explicitly enables live execution.
- For recurring 5-minute Polymarket markets, derive round timing from the recurring slug/session model; do not trust stale Gamma `endDateIso` as the source of truth for market end.
- One-off smoke or operator entrypoints should still behave like Nautilus runtimes: bounded session supervisor, deterministic shutdown, result extraction from Nautilus state, and no hidden forever loops.
- When implementing a simple paper order, do it as a minimal Nautilus strategy: subscribe to the resolved instrument, wait for a valid quote/book state, submit the order through Nautilus, then observe fill/position/account state through Nautilus cache.
- Continuous runners should keep orchestration outside the strategy and decision logic inside the strategy. The runner resolves the next session, starts the node, stops it at the cutoff, extracts results, writes artifacts, and rolls forward.
- Keep runners generic over asset/session/instrument selection so the same runtime pattern can later support markets beyond BTC 5-minute Up/Down without rewriting the control plane.
