#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------
"""
HTTP server exposing manual exit and position-listing endpoints for live weather positions.

Must run inside a VPN-connected container (CLOB API is geo-blocked on the host).

Endpoints:
    GET  /positions          — list all currently open positions from JSONL
    POST /exit               — submit a market SELL for a position
    GET  /health             — liveness check

POST /exit body (JSON):
    {"market_slug": "highest-temperature-in-austin-on-april-20-2026-69forbelow"}
    {"market_slug": "...", "dry_run": true}   # preview without submitting

Usage (started by docker-compose as polymarket-weather-exit-server-vpn):
    Accessible at http://localhost:8080 from the host machine.

Quick reference:
    curl http://localhost:8080/positions | python3 -m json.tool
    curl -s -X POST localhost:8080/exit \\
         -H "Content-Type: application/json" \\
         -d '{"market_slug": "highest-temperature-in-austin-on-april-20-2026-69forbelow"}'
    curl -s -X POST localhost:8080/exit \\
         -H "Content-Type: application/json" \\
         -d '{"market_slug": "...", "dry_run": true}'
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
from datetime import UTC, datetime

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# HTTP/1.1 monkeypatch — must happen before any py_clob_client imports.
import py_clob_client.http_helpers.helpers as _poly_helpers  # noqa: E402
import httpx as _httpx  # noqa: E402
_poly_helpers._http_client = _httpx.Client(http2=False)
del _poly_helpers, _httpx

from examples.live.polymarket.weather_temperature_exit import (  # noqa: E402
    DEFAULT_OUTPUT_DIR,
    find_open_positions,
    submit_exit,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("weather.exit-server")

_OUTPUT_DIR = Path(os.getenv("EXIT_SERVER_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))
_PORT = int(os.getenv("EXIT_SERVER_PORT", "8080"))


class ExitHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for position listing and manual exits."""

    def log_message(self, fmt, *args):  # silence default per-request stderr logging
        log.info("HTTP %s %s %s", self.command, self.path, args[1] if len(args) > 1 else "")

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self):
        if self.path == "/positions" or self.path.startswith("/positions?"):
            self._handle_positions()
        elif self.path == "/health":
            self._respond(200, {"status": "ok", "timestamp": datetime.now(UTC).isoformat()})
        else:
            self._respond(404, {"error": f"Unknown path: {self.path}"})

    def do_POST(self):
        if self.path == "/exit":
            self._handle_exit()
        else:
            self._respond(404, {"error": f"Unknown path: {self.path}"})

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_positions(self):
        try:
            positions = find_open_positions(_OUTPUT_DIR)
            # Summarise fields useful for quick review
            summary = [
                {
                    "market_slug": p.get("market_slug"),
                    "city": p.get("city"),
                    "observation_date": p.get("observation_date"),
                    "threshold_f": p.get("threshold_f"),
                    "metric": p.get("metric"),
                    "token_side": p.get("token_side"),
                    "entry_price": p.get("entry_price"),
                    "shares": p.get("shares"),
                    "stake": p.get("stake"),
                    "instrument_id": p.get("instrument_id"),
                    "entry_time": p.get("entry_time"),
                }
                for p in positions
            ]
            self._respond(200, {"count": len(summary), "positions": summary})
        except Exception as exc:
            log.exception("Error listing positions")
            self._respond(500, {"error": str(exc)})

    def _handle_exit(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body)
        except Exception as exc:
            self._respond(400, {"error": f"Invalid JSON body: {exc}"})
            return

        market_slug = payload.get("market_slug")
        if not market_slug:
            self._respond(400, {"error": "market_slug is required"})
            return

        dry_run = bool(payload.get("dry_run", False))

        try:
            result = submit_exit(
                market_slug=market_slug,
                output_dir=_OUTPUT_DIR,
                dry_run=dry_run,
            )
            self._respond(200, result)
        except ValueError as exc:
            self._respond(404, {"error": str(exc)})
        except Exception as exc:
            log.exception("Error submitting exit for %s", market_slug)
            self._respond(500, {"error": str(exc)})

    # ------------------------------------------------------------------
    # Response helper
    # ------------------------------------------------------------------

    def _respond(self, status: int, body: dict):
        encoded = json.dumps(body, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    log.info("Exit server starting on 0.0.0.0:%d", _PORT)
    log.info("Output dir: %s", _OUTPUT_DIR)
    log.info("Endpoints: GET /positions  POST /exit  GET /health")

    server = HTTPServer(("0.0.0.0", _PORT), ExitHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Exit server stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
