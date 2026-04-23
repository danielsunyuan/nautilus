# Weather Service Restart Agent Context

Use this note before restarting, redeploying, or modifying the live weather services.

## Core Rule

A normal weather-service restart does **not** erase durable trading context, as long as `nautilus/outputs` remains intact.

The weather services use the repo mounted into the container and reconstruct important state from JSONL ledgers under `nautilus/outputs/polymarket/runs`.

## What Persists Across Restart

- Trade history in `weather_temp_live_*.jsonl`
- Confirmed-entry history in `weather_confirmed_live_*.jsonl`
- Exit history in `take_profit.jsonl`
- Settlement history in `settlement_live.jsonl`
- Budget/accounting context derived from those ledgers
- Open-position reconstruction for take-profit and settlement processes

Relevant services are bind-mounted to the repo:
- `weather-live-daemon-vpn`
- `weather-confirmed-entry-vpn`
- `weather-take-profit-vpn`
- `weather-settlement-vpn`

## What Does Not Persist

These are process-memory only and reset on restart:
- `entered_this_session`
- confirmed-daemon `confirm_tracker`
- any in-flight polling/confirmation cadence
- unsaved in-memory state between order submission and ledger write

## Operational Consequences

- Restarting the weather stack should **not** lose past trades if outputs are preserved.
- Restarting should **not** normally re-enter already logged open positions, because services rescan the ledgers.
- The confirmed daemon may need to rebuild confirmation state after restart.
- If a trade was submitted but not yet written to JSONL before restart, restart logic may not know about it.
- Manual exchange actions can create drift if the ledger was not updated to match them.

## Safe Restart Checklist

1. Do **not** delete or replace `nautilus/outputs`.
2. Assume JSONL files are the durable source of truth for weather service context.
3. Restart the weather services together when possible:
   - `weather-live-daemon-vpn`
   - `weather-confirmed-entry-vpn`
   - `weather-take-profit-vpn`
   - `weather-settlement-vpn`
4. After restart, verify:
   - live daemon is running and not stuck on a stale `budget_exhausted` state
   - take-profit watcher reloaded open positions
   - settlement process can read the live ledger
   - confirmed daemon is polling normally
5. If there were manual exits or suspicious trades before restart, re-check account state versus ledger state after services come back.

## Current Budget Accounting Note

The live weather trader now budgets against **unresolved open exposure**, not gross same-day turnover.

This means a restart should continue with recycled capital available once positions are closed and written to `take_profit.jsonl` or `settlement_live.jsonl`.

## Recommended Restart Pattern

For code changes in the mounted repo, a container restart is enough. A rebuild is usually unnecessary unless the image itself changed.

Preferred command pattern:

```bash
docker compose -f nautilus/.docker/docker-compose.yml restart \
  weather-live-daemon-vpn \
  weather-confirmed-entry-vpn \
  weather-take-profit-vpn \
  weather-settlement-vpn
```

Then verify logs and runtime state immediately.
