# Polymarket CLOB Capture on `atlas-wsl`

## Purpose

This slice adds a standalone Polymarket CLOB collector for future Nautilus backtesting.
It is intentionally split into two layers:

1. `polymarket_clob_capture.py`
   Captures raw Polymarket market WebSocket events and writes normalized depth/trade records.
2. `polymarket_clob_export.py`
   Converts normalized capture output into a Nautilus Parquet catalog.

The collector is the first thing to validate on `atlas-wsl`.
The Nautilus export step is second and depends on a working Nautilus Python runtime.

## Files

- `examples/live/polymarket/polymarket_clob_capture.py`
- `examples/live/polymarket/polymarket_clob_export.py`
- `tests/unit_tests/examples/live/polymarket/test_polymarket_clob_capture.py`

## What The Collector Stores

Under a chosen output root it writes:

- `raw/YYYY-MM-DD/<session>.jsonl`
  Lossless inbound WebSocket payloads with receive timestamps and session metadata.
- `normalized/metadata/YYYY-MM-DD/metadata.jsonl`
  Token and market mapping records.
- `normalized/depth/YYYY-MM-DD/depth.jsonl`
  Derived top-10 depth snapshots from `book` and `price_change`.
- `normalized/trades/YYYY-MM-DD/trades.jsonl`
  Derived trade rows from `last_trade_price`.
- `normalized/sessions/YYYY-MM-DD/sessions.jsonl`
  Session start and gap markers.

## Minimum Runtime For Collector

`atlas-wsl` currently has Python 3.12, which is enough for the collector.

Install collector dependencies if missing:

```bash
cd ~/EL/nautilus
python3 -m venv .venv-clob
source .venv-clob/bin/activate
python -m pip install --upgrade pip
python -m pip install aiohttp httpx
```

## Running The Collector

Example with a bounded output root:

```bash
cd ~/EL/nautilus
source .venv-clob/bin/activate
python examples/live/polymarket/polymarket_clob_capture.py \
  --output-root ~/EL/data/polymarket-clob \
  --event-slug <event-slug>
```

Other selection modes are also supported:

```bash
python examples/live/polymarket/polymarket_clob_capture.py \
  --output-root ~/EL/data/polymarket-clob \
  --market-slug <market-slug>
```

```bash
python examples/live/polymarket/polymarket_clob_capture.py \
  --output-root ~/EL/data/polymarket-clob \
  --token-id <token-id>
```

Useful options:

- `--subscription-batch-size 50`
- `--heartbeat-interval-secs 30`
- `--compress-raw`
- `--compress-normalized`

Stop the collector with `Ctrl+C`.

## First Validation Task

The first validation target is the collector itself, not the Nautilus export.

Run a bounded collection against one active Polymarket event and verify all of the following:

1. metadata resolution works
   The process starts without immediate resolver failure.
2. raw payloads are written
   `raw/YYYY-MM-DD/*.jsonl` appears and grows.
3. normalized metadata is written
   `normalized/metadata/.../metadata.jsonl` exists.
4. normalized depth is written
   `normalized/depth/.../depth.jsonl` contains top-10 bid/ask ladders.
5. normalized trades are written when trades occur
   `normalized/trades/.../trades.jsonl` appears for active markets.
6. session markers are written
   `normalized/sessions/.../sessions.jsonl` contains `session_started` and any gap records.

Quick inspection commands:

```bash
find ~/EL/data/polymarket-clob -type f | sort
```

```bash
tail -n 5 ~/EL/data/polymarket-clob/normalized/depth/*/depth.jsonl
```

```bash
tail -n 5 ~/EL/data/polymarket-clob/normalized/trades/*/trades.jsonl
```

```bash
tail -n 5 ~/EL/data/polymarket-clob/normalized/sessions/*/sessions.jsonl
```

## Targeted Test

The focused unit test for this slice is:

```bash
cd ~/EL/nautilus
source .venv-clob/bin/activate
python -m pytest --noconftest -q \
  tests/unit_tests/examples/live/polymarket/test_polymarket_clob_capture.py
```

## Export Step

The exporter writes Nautilus catalog data from normalized capture records:

```bash
python examples/live/polymarket/polymarket_clob_export.py \
  --normalized-root ~/EL/data/polymarket-clob/normalized \
  --catalog-path ~/EL/data/polymarket-catalog \
  --include-quotes
```

## Important Current Caveat

On the source machine, the exporter currently assumes an older Polymarket parsing import path than the repo's Nautilus v2 runtime exposes.
So the collector should be validated first.
If export is attempted and fails on a Polymarket parser import, that is a known compatibility gap to patch next rather than a collector failure.
