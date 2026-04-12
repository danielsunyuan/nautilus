# TODO

### TASK-001: Add Dockerized Polymarket paper trading with NordVPN

- Description: Add a Docker Compose workflow and Nautilus example script for paper trading Polymarket with live market data, sandbox execution, and NordVPN sidecar routing.
- Agent Type: Systems integration / event-driven engines
- Status: In Progress
- Dependencies: None
- Output: Docker services for `nordvpn` and `papertrade`, a Polymarket paper trading example, and operator docs for running it.
- Validation:
  - python - <<'PY'
import yaml
from pathlib import Path
compose = yaml.safe_load(Path('.docker/docker-compose.yml').read_text())
assert 'nordvpn' in compose['services']
assert compose['services']['papertrade']['network_mode'] == 'service:nordvpn'
assert compose['services']['papertrade']['build']['dockerfile'] == '.docker/nautilus_trader.dockerfile'
PY
  - python -m pytest --noconftest tests/unit_tests/examples/test_polymarket_papertrade.py -q
