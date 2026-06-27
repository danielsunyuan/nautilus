# Polymarket viewer tools

Standalone terminal viewers for Polymarket CLOB and Gamma data. These are
operator/debug tools, separate from production scripts under `scripts/`.

Run from the `nautilus/` repo root. The prebuilt Docker image avoids a local
`uv` build:

```bash
cd nautilus
docker run --rm -it \
  -v "$(pwd):/workspace:ro" \
  -w /opt/pysetup \
  nautilus-trader:latest \
  python /workspace/tools/polymarket/<script>.py
```

## Scripts

| Script | Purpose |
| --- | --- |
| `btc_5m_stream.py` | Fast live BTC 5m UP/DOWN top-of-book |
| `btc_5m_orderbook.py` | Full depth books with optional `--watch` |
| `orderbook.py` | Generic book by `--slug` or `--token-id` |
| `world_cup_odds.py` | One-shot Gamma search odds table |
| `world_cup_winner_stream.py` | Live World Cup winner top-of-book |
| `world_cup_winner_gaussian_stream.py` | Live Gaussian over winner mids |

## Examples

```bash
# BTC 5m stream (default 0.5s refresh)
docker run --rm -it -v "$(pwd):/workspace:ro" -w /opt/pysetup nautilus-trader:latest \
  python /workspace/tools/polymarket/btc_5m_stream.py

# World Cup winner stream
docker run --rm -it -v "$(pwd):/workspace:ro" -w /opt/pysetup nautilus-trader:latest \
  python /workspace/tools/polymarket/world_cup_winner_stream.py

# Gaussian visualization
docker run --rm -it -v "$(pwd):/workspace:ro" -w /opt/pysetup nautilus-trader:latest \
  python /workspace/tools/polymarket/world_cup_winner_gaussian_stream.py
```

Local Python (from `nautilus/` with deps installed):

```bash
python tools/polymarket/btc_5m_stream.py --interval 0.25
```
