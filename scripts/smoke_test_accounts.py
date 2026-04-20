from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    name: str
    details: str


def _mask(value: str, *, show: int = 4) -> str:
    v = (value or "").strip()
    if not v:
        return "<empty>"
    if len(v) <= show:
        return "*" * len(v)
    return ("*" * (len(v) - show)) + v[-show:]


def _require_env(keys: Iterable[str]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for key in keys:
        raw = os.environ.get(key, "")
        if not raw.strip():
            results.append(CheckResult(ok=False, name=key, details="missing"))
        else:
            results.append(CheckResult(ok=True, name=key, details=f"present ({_mask(raw)})"))
    return results


def _print_results(title: str, results: list[CheckResult]) -> bool:
    print(f"\n== {title} ==")
    ok = True
    for r in results:
        ok = ok and r.ok
        status = "OK" if r.ok else "FAIL"
        print(f"- {status} {r.name}: {r.details}")
    return ok


def _check_ibkr_socket() -> CheckResult:
    host = os.environ.get("IBKR_HOST", "").strip()
    port_raw = os.environ.get("IBKR_PORT", "").strip()
    if not host or not port_raw:
        return CheckResult(ok=False, name="IBKR_SOCKET", details="IBKR_HOST/IBKR_PORT missing")
    try:
        port = int(port_raw)
    except ValueError:
        return CheckResult(ok=False, name="IBKR_SOCKET", details="IBKR_PORT must be an int")

    try:
        with socket.create_connection((host, port), timeout=2.0):
            return CheckResult(ok=True, name="IBKR_SOCKET", details=f"connected to {host}:{port}")
    except OSError as e:
        return CheckResult(ok=False, name="IBKR_SOCKET", details=f"connect failed: {e.__class__.__name__}")


def _check_polymarket_private_key_local() -> CheckResult:
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not pk:
        return CheckResult(ok=False, name="POLYMARKET_PRIVATE_KEY", details="missing")

    try:
        from eth_account import Account  # type: ignore
    except Exception:
        return CheckResult(
            ok=False,
            name="POLYMARKET_PRIVATE_KEY",
            details="eth-account not installed (pip install eth-account)",
        )

    try:
        acct = Account.from_key(pk)
        addr = str(acct.address)
        return CheckResult(ok=True, name="POLYMARKET_PRIVATE_KEY", details=f"key parses, address={_mask(addr, show=6)}")
    except Exception as e:
        return CheckResult(ok=False, name="POLYMARKET_PRIVATE_KEY", details=f"invalid key: {e.__class__.__name__}")


def _check_polymarket_clob_read() -> CheckResult:
    try:
        from py_clob_client.client import ClobClient  # type: ignore
    except Exception:
        return CheckResult(ok=False, name="POLYMARKET_CLOB", details="py-clob-client not installed (pip install py-clob-client)")

    host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").strip()
    chain_id_raw = os.environ.get("POLYMARKET_CHAIN_ID", "137").strip()
    try:
        chain_id = int(chain_id_raw)
    except ValueError:
        return CheckResult(ok=False, name="POLYMARKET_CLOB", details="POLYMARKET_CHAIN_ID must be an int")

    try:
        client = ClobClient(host, chain_id=chain_id)
        markets = client.get_markets()
        count = 0
        if isinstance(markets, dict):
            try:
                count = int(markets.get("count", 0))
            except Exception:
                count = 0
        return CheckResult(ok=True, name="POLYMARKET_CLOB", details=f"connected, markets.count={count}")
    except Exception as e:
        return CheckResult(ok=False, name="POLYMARKET_CLOB", details=f"read failed: {e.__class__.__name__}")


def _check_polymarket_clob_auth_positions() -> CheckResult:
    try:
        from py_clob_client.client import ClobClient  # type: ignore
    except Exception:
        return CheckResult(ok=False, name="POLYMARKET_CLOB_AUTH", details="py-clob-client not installed (pip install py-clob-client)")

    host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").strip()
    key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not key:
        return CheckResult(ok=False, name="POLYMARKET_CLOB_AUTH", details="POLYMARKET_PRIVATE_KEY missing")

    chain_id_raw = os.environ.get("POLYMARKET_CHAIN_ID", "137").strip()
    sig_type_raw = os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0").strip()
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip() or None

    try:
        chain_id = int(chain_id_raw)
        signature_type = int(sig_type_raw)
    except ValueError:
        return CheckResult(ok=False, name="POLYMARKET_CLOB_AUTH", details="CHAIN_ID/SIGNATURE_TYPE must be ints")

    try:
        client = ClobClient(host, key=key, chain_id=chain_id, signature_type=signature_type, funder=funder)
        api_creds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)

        # "Full capability" authenticated, non-trading checks:
        # - server time (sanity)
        # - api keys list (auth)
        #
        # Note: some account/balance endpoints in py-clob-client require additional params
        # (and may raise AttributeError if called without them). We avoid those here.
        server_time = client.get_server_time()
        api_keys = client.get_api_keys()

        _ = server_time, api_keys
        return CheckResult(ok=True, name="POLYMARKET_CLOB_AUTH", details="auth ok (server_time/api_keys)")
    except Exception as e:
        return CheckResult(ok=False, name="POLYMARKET_CLOB_AUTH", details=f"auth failed: {e.__class__.__name__}")


def _check_polymarket_subgraph_positions() -> CheckResult:
    endpoint = os.environ.get(
        "POLYMARKET_SUBGRAPH_POSITIONS",
        "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/positions-subgraph/0.0.7/gn",
    ).strip()

    query = "query Smoke { __typename }"

    try:
        import httpx  # type: ignore
    except Exception:
        return CheckResult(ok=False, name="POLYMARKET_SUBGRAPH", details="httpx not installed")

    try:
        with httpx.Client(timeout=5.0, headers={"User-Agent": "quant-smoke-test/1.0"}) as client:
            resp = client.post(endpoint, json={"query": query})
        if resp.status_code != 200:
            return CheckResult(ok=False, name="POLYMARKET_SUBGRAPH", details=f"http {resp.status_code}")
        data = resp.json()
        ok = isinstance(data, dict) and "errors" not in data
        return CheckResult(ok=ok, name="POLYMARKET_SUBGRAPH", details="query ok" if ok else "query returned errors")
    except Exception as e:
        return CheckResult(ok=False, name="POLYMARKET_SUBGRAPH", details=f"query failed: {e.__class__.__name__}")


def _check_polymarket_rtds_node() -> CheckResult:
    script = Path("scripts/polymarket_rtds_smoke.mjs").resolve()
    if not script.exists():
        return CheckResult(ok=False, name="POLYMARKET_RTDS", details="missing scripts/polymarket_rtds_smoke.mjs")

    try:
        proc = subprocess.run(
            ["node", str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except FileNotFoundError:
        return CheckResult(ok=False, name="POLYMARKET_RTDS", details="node not installed")
    except subprocess.TimeoutExpired:
        return CheckResult(ok=False, name="POLYMARKET_RTDS", details="timeout")

    if proc.returncode == 0:
        return CheckResult(ok=True, name="POLYMARKET_RTDS", details="received message")
    return CheckResult(ok=False, name="POLYMARKET_RTDS", details="failed (run node script manually for logs)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Account smoke test (masked output).")
    parser.add_argument(
        "--venues",
        default="all",
        help="Comma-separated list of venues to check: polymarket,ibkr,coinbase,all",
    )
    parser.add_argument(
        "--env-file",
        default="configs/.env",
        help="Path to env file (default: configs/.env). For Polymarket-only, use .env.polymarket",
    )
    parser.add_argument(
        "--polymarket-full",
        action="store_true",
        help="Run Polymarket CLOB+RTDS+Subgraph checks (no trading).",
    )
    parser.add_argument(
        "--require-polymarket-rtds",
        action="store_true",
        help="Fail if the Polymarket RTDS check fails (default: warn-only).",
    )
    args = parser.parse_args()

    venues_raw = (args.venues or "all").strip().lower()
    venues = {v.strip() for v in venues_raw.split(",") if v.strip()}
    if "all" in venues:
        venues = {"coinbase", "ibkr", "polymarket"}

    env_path = Path(args.env_file).resolve()
    if not env_path.exists():
        print(f"FAIL: env file not found: {env_path}")
        return 2

    load_dotenv(env_path, override=False)

    coinbase_env_ok = True
    if "coinbase" in venues:
        # Coinbase: keep generic because SDK choice varies; validate presence only here.
        coinbase_env_ok = _print_results(
            "Coinbase env",
            _require_env(
                [
                    "COINBASE_API_KEY",
                    "COINBASE_API_SECRET",
                ]
            ),
        )

    ibkr_env_ok = True
    ibkr_sock_ok = True
    if "ibkr" in venues:
        # IBKR: typically uses a running TWS/IB Gateway. Smoke-test the TCP port only.
        ibkr_env_ok = _print_results(
            "IBKR env",
            _require_env(
                [
                    "IBKR_HOST",
                    "IBKR_PORT",
                    "IBKR_CLIENT_ID",
                ]
            ),
        )
        ibkr_sock = _check_ibkr_socket()
        ibkr_sock_ok = _print_results("IBKR connectivity", [ibkr_sock])

    polymarket_env_ok = True
    polymarket_key_ok = True
    if "polymarket" in venues:
        # Polymarket: key parsing (local) + optional full checks.
        polymarket_env_ok = _print_results(
            "Polymarket env",
            _require_env(
                [
                    "POLYMARKET_PRIVATE_KEY",
                ]
            ),
        )
        polymarket_key_ok = _print_results("Polymarket key check", [_check_polymarket_private_key_local()])

    polymarket_full_ok = True
    if "polymarket" in venues and bool(args.polymarket_full):
        polymarket_full_ok = _print_results("Polymarket CLOB (public read)", [_check_polymarket_clob_read()])
        polymarket_full_ok = polymarket_full_ok and _print_results(
            "Polymarket CLOB (auth + positions)", [_check_polymarket_clob_auth_positions()]
        )
        polymarket_full_ok = polymarket_full_ok and _print_results(
            "Polymarket subgraph (positions endpoint)", [_check_polymarket_subgraph_positions()]
        )
        rtds_ok = _print_results("Polymarket RTDS (official TS client via node)", [_check_polymarket_rtds_node()])
        if bool(args.require_polymarket_rtds):
            polymarket_full_ok = polymarket_full_ok and rtds_ok

    all_ok = (
        coinbase_env_ok
        and ibkr_env_ok
        and ibkr_sock_ok
        and polymarket_env_ok
        and polymarket_key_ok
        and polymarket_full_ok
    )
    print("\n== Summary ==")
    print("PASS" if all_ok else "FAIL")
    return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

