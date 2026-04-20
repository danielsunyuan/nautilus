# Plan: Consolidate `quant/` and `polynautilus/` under `nautilus/`

**Date:** 2026-04-20  
**Status:** Proposed  
**Motivation:** Active trading infrastructure is split across three repos. `nautilus/` is the trusted framework; the other two contain a mix of useful tooling and unfinished vibecoded scaffolding. Consolidating eliminates the split-brain, simplifies agent context, and makes the project's docker infrastructure self-contained.

---

## Current state

| Repo | What it actually does today |
|---|---|
| `nautilus/` | All strategies, daemons, settlement, exit tooling, tests. The real system. |
| `polynautilus/` | Data recording scripts (CLOB → Nautilus catalog) + docker-compose overlay for live VPN containers. Lightweight recorder image (`python:3.12-slim` + `nautilus_trader` pip). |
| `quant/` | Base `quant-polymarket:latest` image (Python + py-clob-client), geoblock check, balance monitor. Multi-venue scaffolding (IBKR, Coinbase) never built. Early pre-Nautilus research scripts. |

### The duplication problem

- NordVPN `Dockerfile.nordvpn` is **identical** in both `quant/docker/` and `polynautilus/docker/` — except the `polynautilus` entrypoint has a Docker DNS fix that `quant` lacks
- Three docker-compose files for one live trading system (`nautilus/.docker/`, `polynautilus/`, `quant/`)
- Network names differ (`nautilus_nautilus-network` vs compose-default) creating external network dependencies
- Any new agent has to read three repos to understand what is running

---

## What to keep (move into `nautilus/`)

### From `polynautilus/`

| Asset | Destination | Notes |
|---|---|---|
| `docker/Dockerfile.nordvpn` + entrypoint/healthcheck | `nautilus/.docker/` | Use the `polynautilus` version — it has the DNS fix `quant` lacks |
| `docker/Dockerfile.nautilus_recorder` | `nautilus/.docker/Dockerfile.recorder` | Lightweight pip image; keep separate from the Rust/Cython build |
| `docker-compose.yml` services | Merge into `nautilus/.docker/docker-compose.yml` | See Phase 2 for service-by-service mapping |
| `scripts/polymarket_crypto_5m_nautilus_record.py` | `nautilus/scripts/` | Active recorder |
| `scripts/polymarket_crypto_5m_live_signal_watcher.py` | `nautilus/scripts/` | Active signal watcher |
| `scripts/polymarket_crypto_multi_tf_recorder.py` | `nautilus/scripts/` | Active multi-TF recorder |
| `scripts/polymarket_crypto_5m_threshold_calibration.py` | `nautilus/scripts/` | Research tooling |
| `scripts/polymarket_crypto_5m_false_spike_*.py` | `nautilus/scripts/` | Research tooling |
| `scripts/polymarket_crypto_5m_backfill_resolutions.py` | `nautilus/scripts/` | Useful for backfill |

### From `quant/`

| Asset | Destination | Notes |
|---|---|---|
| `scripts/polymarket_geoblock_check.py` | `nautilus/scripts/` | Zero deps (stdlib only) — keep as pre-flight utility |
| `scripts/polymarket_balance.py` | `nautilus/scripts/` | Useful operator tool |
| `scripts/measure_clob_latency.py` | `nautilus/scripts/` | Useful diagnostics |
| `scripts/smoke_test_accounts.py` | `nautilus/scripts/` | Pre-execution smoke test |
| `scripts/uma_resolution_watch.py` | `nautilus/scripts/` | Future UMA integration hook |
| `docker/polymarket_execution_entrypoint.sh` | `nautilus/.docker/` | Geoblock-gated execution wrapper |
| `Dockerfile` (base image) | Superseded — absorb deps into recorder Dockerfile | `quant-polymarket:latest` is just Python + py-clob-client; bake into recorder image |

---

## What to discard (do not migrate)

### From `quant/`
- `adapters/`, `core/`, `strategies/`, `execution/`, `polymarket_sdk/`, `agents/`, `orchestrator/` — multi-venue scaffolding that was never built out; design has been superseded by Nautilus
- Early research scripts: `btc_5m_paper_ws*.py`, `one_cent_*.py`, `eurovision_*.py`, `binance_*.py`, `coinbase_balance.py`, `polymarket_hf_dataset_observe.py`, `polymarket_imbalance_watch.py`, `polymarket_rtds_smoke.mjs`
- `chainlink_streams/`, `uma/` — speculative integrations, not active
- `pyproject.toml`, `package.json`, `run.sh` — quant-specific scaffolding

### From `polynautilus/`
- `scripts/polymarket_crypto_5m_probs_ws.py` — early prototype, superseded by Nautilus daemon
- `scripts/polymarket_btc_hourly_watch.py` — superseded
- `tests/` — test artifacts for polynautilus scripts only
- `outputs/nautilus_catalog`, `outputs/nautilus_export` — runtime data, stays on filesystem

---

## Migration phases

### Phase 1 — Docker infrastructure

1. Copy `polynautilus/docker/Dockerfile.nordvpn` → `nautilus/.docker/Dockerfile.nordvpn`
2. Copy `polynautilus/docker/nordvpn_entrypoint.sh` + `nordvpn_healthcheck.sh` → `nautilus/.docker/`
3. Copy `polynautilus/docker/Dockerfile.nautilus_recorder` → `nautilus/.docker/Dockerfile.recorder`
4. Absorb `quant/docker/polymarket_execution_entrypoint.sh` → `nautilus/.docker/`
5. Rebuild images from the new paths, verify they match current deployed images

### Phase 2 — docker-compose consolidation

Merge `polynautilus/docker-compose.yml` services into `nautilus/.docker/docker-compose.yml`. Key mappings:

| polynautilus service | Action |
|---|---|
| `nordvpn` | Already exists in `nautilus/.docker/docker-compose.yml` — unify to single definition |
| `polymarket-weather-live-vpn` | Already in `nautilus/.docker/` — remove from polynautilus |
| `polymarket-weather-exit-server-vpn` | Already in `nautilus/.docker/` — remove from polynautilus |
| `polymarket-weather-live-settlement-vpn` | Already in `nautilus/.docker/` — remove from polynautilus |
| `polymarket-5m-nautilus-recorder-vpn` | Move → `nautilus/.docker/` |
| `polymarket-5m-live-signal-watcher-vpn` | Move → `nautilus/.docker/` |
| `polymarket-multi-tf-recorder-vpn` | Move → `nautilus/.docker/` |
| `polymarket-readonly` / `polymarket-monitor` / `polymarket-execution` | Move → `nautilus/.docker/`; update image to recorder image |

Resolve network naming: replace `nautilus-shared` external network dependency with the compose-internal network already defined in `nautilus/.docker/docker-compose.yml`.

### Phase 3 — Scripts

1. Copy selected scripts (see table above) into `nautilus/scripts/`
2. Update any `PYTHONPATH` or import assumptions to match the nautilus repo layout
3. Add brief docstrings noting original provenance where useful

### Phase 4 — Smoke test

For each migrated service:
```bash
# Verify image builds
docker compose -f nautilus/.docker/docker-compose.yml build <service>

# Verify service starts and reaches healthy state
docker compose -f nautilus/.docker/docker-compose.yml --profile vpn up -d <service>
docker ps --filter name=<container>
docker logs <container> --tail 20
```

Run the weather settlement poller for one poll cycle and confirm it settles entries as expected.

### Phase 5 — Archive

Once all services are confirmed running from `nautilus/.docker/`:
1. Add `ARCHIVED.md` to `quant/` and `polynautilus/` roots explaining they are superseded
2. Stop running any services from the old compose files
3. No deletion required — git history in those repos remains available

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Volume mount paths change during migration | Audit every `volumes:` in polynautilus compose before moving; keep `../nautilus` relative paths intact |
| Network namespace sharing for exit server port 8080 | The `ports: ["8080:8080"]` must remain on the nordvpn container — verify this is preserved in the merged compose |
| Recorder image Python version (3.12) vs quant base (3.11) | Standardise on 3.12 in the recorder Dockerfile |
| `outputs/` catalog data on polynautilus filesystem | Catalog paths are configured via env vars — update `POLYMARKET_NAUTILUS_CATALOG_HOST_PATH` to point at the same host path |
| Docker network name `nautilus_nautilus-network` | All external network references in polynautilus become internal — simplifies the setup |

---

## Success criteria

- Single `docker compose -f nautilus/.docker/docker-compose.yml` command starts the entire live stack
- All existing containers (weather daemon, settlement, exit server, recorder, signal watcher) run from the consolidated compose
- `quant/` and `polynautilus/` are no longer referenced in any running container
- `nautilus/CLAUDE.md` and `nautilus/AGENTS.md` are the only context files an agent needs to read
