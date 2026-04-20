from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_URL = "https://polymarket.com/api/geoblock"
DEFAULT_HEADERS = {"User-Agent": "quant-polymarket-geoblock/1.0"}


def _fetch_status(*, url: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    if not isinstance(payload, dict):
        raise ValueError("unexpected geoblock payload")
    return payload


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _render_text(payload: dict[str, Any]) -> str:
    blocked = _as_bool(payload.get("blocked"))
    ip = str(payload.get("ip") or "?")
    country = str(payload.get("country") or "?")
    region = str(payload.get("region") or "?")
    return f"blocked={blocked} country={country} region={region} ip={ip}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Polymarket geoblock status for the current egress IP.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Geoblock endpoint URL.")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds.")
    parser.add_argument("--json", action="store_true", help="Print JSON payload.")
    parser.add_argument(
        "--require-unblocked",
        action="store_true",
        help="Exit non-zero if the current egress is blocked.",
    )
    parser.add_argument(
        "--expected-country",
        default="",
        help="Optional ISO country code expected for this execution environment (for example JP).",
    )
    args = parser.parse_args()

    try:
        payload = _fetch_status(url=args.url, timeout=args.timeout)
    except urllib.error.HTTPError as exc:
        print(f"FAIL: geoblock endpoint returned HTTP {exc.code}", file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print(f"FAIL: geoblock endpoint unreachable: {exc.reason}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI path
        print(f"FAIL: geoblock lookup failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_render_text(payload))

    blocked = _as_bool(payload.get("blocked"))
    country = str(payload.get("country") or "").upper()
    expected_country = args.expected_country.strip().upper()

    if args.require_unblocked and blocked:
        print("FAIL: current egress is blocked for Polymarket trading", file=sys.stderr)
        return 1

    if expected_country and country != expected_country:
        print(
            f"FAIL: current egress country {country or '?'} does not match expected {expected_country}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
