from __future__ import annotations

import json
import socket
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


USER_AGENT = "quant-latency-probe/1.0"

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
CLOB_TIME_URL = "https://clob.polymarket.com/time"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets?limit=1"

TCP_HOSTS = {
    "clob": "clob.polymarket.com",
    "gamma": "gamma-api.polymarket.com",
    "polymarket_web": "polymarket.com",
}

HTTP_ENDPOINTS = {
    "geoblock": GEOBLOCK_URL,
    "clob_time": CLOB_TIME_URL,
    "gamma_markets": GAMMA_MARKETS_URL,
}


def measure_http_latency(
    url: str, attempts: int = 5, timeout: float = 10.0
) -> dict[str, Any]:
    timings: list[float] = []
    errors: list[str] = []

    for _ in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            timings.append(elapsed_ms)
        except Exception as exc:
            if len(errors) < 3:
                errors.append(f"{exc.__class__.__name__}: {exc}")
        time.sleep(0.2)

    result: dict[str, Any] = {
        "url": url,
        "attempts": attempts,
        "successful": len(timings),
        "errors": errors,
        "timings_ms": timings,
    }
    if timings:
        result["min_ms"] = round(min(timings), 2)
        result["max_ms"] = round(max(timings), 2)
        result["mean_ms"] = round(statistics.mean(timings), 2)
        result["median_ms"] = round(statistics.median(timings), 2)
        result["stdev_ms"] = round(statistics.stdev(timings), 2) if len(timings) > 1 else 0.0
    else:
        result["min_ms"] = None
        result["max_ms"] = None
        result["mean_ms"] = None
        result["median_ms"] = None
        result["stdev_ms"] = None

    return result


def measure_tcp_connect(
    host: str, port: int = 443, attempts: int = 5
) -> dict[str, Any]:
    timings: list[float] = []

    ctx = ssl.create_default_context()
    for _ in range(attempts):
        t0 = time.monotonic()
        try:
            raw = socket.create_connection((host, port), timeout=10)
            try:
                ctx.wrap_socket(raw, server_hostname=host)
            finally:
                raw.close()
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            timings.append(elapsed_ms)
        except Exception:
            pass

    result: dict[str, Any] = {
        "host": host,
        "port": port,
        "successful": len(timings),
    }
    if timings:
        result["min_ms"] = round(min(timings), 2)
        result["mean_ms"] = round(statistics.mean(timings), 2)
        result["median_ms"] = round(statistics.median(timings), 2)
    else:
        result["min_ms"] = None
        result["mean_ms"] = None
        result["median_ms"] = None

    return result


def measure_tcp_syn(
    host: str, port: int = 443, attempts: int = 5
) -> dict[str, Any]:
    """TCP SYN-ACK only (no TLS). Exactly 1 network round trip."""
    timings: list[float] = []

    for _ in range(attempts):
        t0 = time.monotonic()
        try:
            sock = socket.create_connection((host, port), timeout=10)
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            timings.append(elapsed_ms)
            sock.close()
        except Exception:
            pass

    result: dict[str, Any] = {
        "host": host,
        "port": port,
        "successful": len(timings),
    }
    if timings:
        result["min_ms"] = round(min(timings), 2)
        result["mean_ms"] = round(statistics.mean(timings), 2)
        result["median_ms"] = round(statistics.median(timings), 2)
    else:
        result["min_ms"] = None
        result["mean_ms"] = None
        result["median_ms"] = None

    return result


def get_geoblock_info(timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(GEOBLOCK_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as exc:
        return {"error": f"{exc.__class__.__name__}: {exc}"}


def main() -> int:
    vpn_exit = get_geoblock_info()

    tcp_syn: dict[str, Any] = {}
    for label, host in TCP_HOSTS.items():
        tcp_syn[label] = measure_tcp_syn(host)

    tcp_handshake: dict[str, Any] = {}
    for label, host in TCP_HOSTS.items():
        tcp_handshake[label] = measure_tcp_connect(host)

    http_latency: dict[str, Any] = {}
    for label, url in HTTP_ENDPOINTS.items():
        http_latency[label] = measure_http_latency(url)

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "vpn_exit": vpn_exit,
        "tcp_syn": tcp_syn,
        "tcp_handshake": tcp_handshake,
        "http_latency": http_latency,
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
