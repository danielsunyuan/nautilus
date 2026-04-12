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
- Credentials remain in local `.env*` files and are injected into the workspace container through Compose; never print secret values.
