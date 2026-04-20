from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


USDC_DECIMALS = 6


class _Style:
    """ANSI colors; disabled when not a TTY, NO_COLOR is set, or --no-color."""

    def __init__(self, enabled: bool) -> None:
        self._on = enabled
        self.reset = "\033[0m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.red = "\033[91m" if enabled else ""
        self.green = "\033[92m" if enabled else ""
        self.yellow = "\033[93m" if enabled else ""
        self.blue = "\033[94m" if enabled else ""
        self.magenta = "\033[95m" if enabled else ""
        self.cyan = "\033[96m" if enabled else ""
        self.white = "\033[97m" if enabled else ""

    def line(self, s: str = "") -> str:
        return f"{self.dim}{s}{self.reset}" if self._on else s


def _use_color(*, no_color_flag: bool) -> bool:
    if no_color_flag or os.environ.get("NO_COLOR", "").strip():
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pnl_style(S: _Style, cash_pnl: float) -> str:
    """Green if up, red if down, dim if flat (Polymarket-style)."""
    if cash_pnl > 1e-9:
        return S.green
    if cash_pnl < -1e-9:
        return S.red
    return S.dim


# Official Polymarket HTTP APIs (see docs):
# - Data API: https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user
# - Total position value: https://docs.polymarket.com/api-reference/core/get-total-value-of-a-users-positions
# - Gamma public profile (proxy wallet): https://docs.polymarket.com/api-reference/profiles/get-public-profile-by-wallet-address
_DEFAULT_DATA_API = "https://data-api.polymarket.com"
_DEFAULT_GAMMA_API = "https://gamma-api.polymarket.com"
_DEFAULT_HTTP_HEADERS = {"User-Agent": "quant-polymarket-balance/1.0"}


def _mask(value: str, *, show: int = 6) -> str:
    v = (value or "").strip()
    if not v:
        return "<empty>"
    if len(v) <= show:
        return "*" * len(v)
    return ("*" * (len(v) - show)) + v[-show:]


def _get_address_from_private_key(private_key: str) -> str:
    from eth_account import Account  # type: ignore

    acct = Account.from_key(private_key)
    return str(acct.address)


def _normalize_profile_address(addr: str) -> str | None:
    """Data API `user` must be 0x + 40 hex (see OpenAPI Address schema)."""
    a = (addr or "").strip()
    if not a:
        return None
    if not a.startswith("0x"):
        a = "0x" + a
    if len(a) != 42:
        return None
    hexpart = a[2:]
    if len(hexpart) != 40 or any(c not in "0123456789abcdefABCDEF" for c in hexpart):
        return None
    return "0x" + hexpart.lower()


def _fetch_gamma_proxy_wallet(*, gamma_base: str, address: str) -> str | None:
    a = _normalize_profile_address(address)
    if not a:
        return None
    url = f"{gamma_base.rstrip('/')}/public-profile"
    try:
        with httpx.Client(timeout=10.0, headers=_DEFAULT_HTTP_HEADERS) as client:
            r = client.get(url, params={"address": a})
        if r.status_code != 200:
            return None
        data = r.json()
        pw = data.get("proxyWallet")
        if isinstance(pw, str) and pw.strip():
            return _normalize_profile_address(pw.strip())
    except Exception:
        return None
    return None


def _fetch_data_api_total_usdc(*, data_base: str, user: str) -> float:
    a = _normalize_profile_address(user)
    if not a:
        return 0.0
    url = f"{data_base.rstrip('/')}/value"
    with httpx.Client(timeout=20.0, headers=_DEFAULT_HTTP_HEADERS) as client:
        r = client.get(url, params={"user": a})
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        return 0.0
    first = rows[0]
    if isinstance(first, dict) and isinstance(first.get("value"), (int, float)):
        return float(first["value"])
    return 0.0


def _fetch_data_api_positions(*, data_base: str, user: str, limit: int = 500) -> list[dict[str, Any]]:
    """
    GET /positions — documented fields include title, outcome, size, currentValue, curPrice, etc.
    """
    a = _normalize_profile_address(user)
    if not a:
        return []
    url = f"{data_base.rstrip('/')}/positions"
    params = {
        "user": a,
        "limit": min(max(limit, 0), 500),
        "sortBy": "CURRENT",
        "sortDirection": "DESC",
    }
    with httpx.Client(timeout=45.0, headers=_DEFAULT_HTTP_HEADERS) as client:
        r = client.get(url, params=params)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _resolve_data_api_user_candidates() -> tuple[list[str], str]:
    """
    Return (ordered unique addresses, note) for GET /positions and /value.
    Tries explicit env, Gamma proxy derivation, funder, signer.
    """
    data_hint = (os.environ.get("POLYMARKET_DATA_API_USER") or "").strip()
    funder = (os.environ.get("POLYMARKET_FUNDER_ADDRESS") or "").strip()
    pk = (os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip()
    signer = _get_address_from_private_key(pk) if pk else ""

    gamma_base = (os.environ.get("POLYMARKET_GAMMA_API") or _DEFAULT_GAMMA_API).strip()

    seen: set[str] = set()
    ordered: list[str] = []

    def add(addr: str) -> None:
        n = _normalize_profile_address(addr)
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)

    note = "resolution order: POLYMARKET_DATA_API_USER → signer → funder → gamma proxyWallet(s)"
    if data_hint:
        add(data_hint)
    if signer:
        add(signer)
    if funder:
        add(funder)

    for base in [signer, funder]:
        if base:
            proxy = _fetch_gamma_proxy_wallet(gamma_base=gamma_base, address=base)
            if proxy:
                add(proxy)

    return ordered, note


def _pick_best_user_for_positions(*, data_base: str, candidates: list[str]) -> tuple[str | None, float]:
    """Choose the address whose /value total is largest (matches UI portfolio best)."""
    best_u: str | None = None
    best_v = -1.0
    for u in candidates:
        try:
            v = _fetch_data_api_total_usdc(data_base=data_base, user=u)
        except Exception:
            continue
        if v > best_v:
            best_v = v
            best_u = u
    if best_v < 0:
        best_v = 0.0
    return best_u, best_v


def _fetch_clob_balance_allowance() -> tuple[Decimal, int]:
    """
    Uses the official `py-clob-client` to fetch collateral balance + allowance entries.
    """

    from py_clob_client.client import ClobClient  # type: ignore
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams  # type: ignore

    host = (os.environ.get("POLYMARKET_CLOB_HOST") or "https://clob.polymarket.com").strip()
    key = (os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip()
    chain_id = int((os.environ.get("POLYMARKET_CHAIN_ID") or "137").strip())
    signature_type = int((os.environ.get("POLYMARKET_SIGNATURE_TYPE") or "0").strip())
    funder = (os.environ.get("POLYMARKET_FUNDER_ADDRESS") or "").strip() or None

    client = ClobClient(host, key=key, chain_id=chain_id, signature_type=signature_type, funder=funder)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    # Avoid py-clob-client bug when params=None by passing params explicitly.
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
    # Sync cached balance/allowance first (some accounts require it).
    try:
        client.update_balance_allowance(params)
    except Exception:
        pass
    resp = client.get_balance_allowance(params)
    bal_raw = resp.get("balance", "0") if isinstance(resp, dict) else "0"
    allowances = resp.get("allowances", []) if isinstance(resp, dict) else []
    try:
        bal = Decimal(str(bal_raw))
    except Exception:
        bal = Decimal(0)
    n_allow = len(allowances) if isinstance(allowances, list) else 0
    return bal, n_allow


def _fetch_collateral_balance_for_signature_types(signature_types: list[int]) -> list[tuple[int, Decimal, int]]:
    out: list[tuple[int, Decimal, int]] = []
    prev_sig = os.environ.get("POLYMARKET_SIGNATURE_TYPE")
    try:
        for sig in signature_types:
            os.environ["POLYMARKET_SIGNATURE_TYPE"] = str(sig)
            bal, n_allow = _fetch_clob_balance_allowance()
            out.append((sig, bal, n_allow))
    finally:
        if prev_sig is not None:
            os.environ["POLYMARKET_SIGNATURE_TYPE"] = prev_sig
        elif "POLYMARKET_SIGNATURE_TYPE" in os.environ:
            del os.environ["POLYMARKET_SIGNATURE_TYPE"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket balance (official py-clob-client; no secrets printed).")
    parser.add_argument("--env-file", default=".env.polymarket", help="Env file path (default: .env.polymarket)")
    parser.add_argument(
        "--try-signature-types",
        default="0,1,2",
        help="Comma-separated signature types to try (default: 0,1,2).",
    )
    parser.add_argument(
        "--positions",
        action="store_true",
        help="Also list open positions and USDC value via official Data API (/positions, /value).",
    )
    parser.add_argument(
        "--positions-limit",
        type=int,
        default=500,
        help="Max rows for GET /positions (default 500, API max 500).",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors (also respects NO_COLOR env).",
    )
    args = parser.parse_args()

    S = _Style(_use_color(no_color_flag=bool(args.no_color)))

    env_path = Path(args.env_file).resolve()
    if not env_path.exists():
        print(f"{S.red}FAIL:{S.reset} env file not found: {env_path}")
        return 2

    load_dotenv(env_path, override=False)

    private_key = (os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip()
    if not private_key:
        print(f"{S.red}FAIL:{S.reset} POLYMARKET_PRIVATE_KEY missing")
        return 2

    address = _get_address_from_private_key(private_key)
    print(S.line("─" * 72))
    print(f"{S.bold}{S.cyan}Polymarket account{S.reset}  {S.dim}signer{S.reset} {_mask(address)}")
    print(S.line("─" * 72))

    try:
        sigs = []
        for part in (args.try_signature_types or "").split(","):
            part = part.strip()
            if not part:
                continue
            sigs.append(int(part))
        if not sigs:
            sigs = [int((os.environ.get("POLYMARKET_SIGNATURE_TYPE") or "0").strip())]

        results = _fetch_collateral_balance_for_signature_types(sigs)
    except Exception as e:
        print(f"{S.red}FAIL:{S.reset} {e.__class__.__name__}: {e}")
        return 2

    print(f"\n{S.bold}{S.white}CLOB collateral{S.reset} {S.dim}(py-clob-client){S.reset}")
    divisor = Decimal(10) ** USDC_DECIMALS
    for sig, bal, allow_n in results:
        usdc = (bal / divisor) if divisor else bal
        non_zero = bal > 0
        money = f"{S.green}{usdc.normalize()} USDC{S.reset}" if non_zero else f"{S.dim}0 USDC{S.reset}"
        sig_col = f"{S.yellow}{sig}{S.reset}"
        print(
            f"  {S.dim}sig_type{S.reset} {sig_col}  "
            f"{S.dim}base{S.reset} {bal}  "
            f"{S.dim}collateral{S.reset} {money}  "
            f"{S.dim}allowances{S.reset} {allow_n}"
        )

    if bool(args.positions):
        data_base = (os.environ.get("POLYMARKET_DATA_API") or _DEFAULT_DATA_API).strip()
        candidates, res_note = _resolve_data_api_user_candidates()
        print(f"\n{S.bold}{S.magenta}Positions{S.reset} {S.dim}(Data API){S.reset}")
        print(
            f"{S.dim}Docs{S.reset} "
            f"{S.blue}https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user{S.reset}"
        )
        print(f"{S.dim}{res_note}{S.reset}")
        if not candidates:
            print(f"{S.yellow}No candidate addresses to query.{S.reset}")
            return 0
        best_user, api_total = _pick_best_user_for_positions(data_base=data_base, candidates=candidates)
        if not best_user:
            print(
                f"{S.red}Could not resolve a user with /value; tried:{S.reset} "
                f"{S.dim}{', '.join(_mask(x) for x in candidates)}{S.reset}"
            )
            return 2
        print(
            f"{S.dim}Data API user{S.reset} {S.cyan}{_mask(best_user)}{S.reset}  "
            f"{S.dim}/value{S.reset} {S.bold}{S.green}{api_total:.6f}{S.reset} {S.dim}USDC{S.reset}"
        )
        try:
            rows = _fetch_data_api_positions(data_base=data_base, user=best_user, limit=args.positions_limit)
        except Exception as e:
            print(f"{S.red}GET /positions failed:{S.reset} {e.__class__.__name__}: {e}")
            return 2
        sum_current = 0.0
        sum_cash_pnl = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            sum_current += _safe_float(row.get("currentValue"))
            sum_cash_pnl += _safe_float(row.get("cashPnl"))
        pnl_hint = _pnl_style(S, sum_cash_pnl)
        print(
            f"{S.dim}rows{S.reset} {len(rows)}  "
            f"{S.dim}Σ currentValue{S.reset} {S.bold}{S.green}{sum_current:.2f}{S.reset} {S.dim}USDC{S.reset}  "
            f"{S.dim}Σ PnL{S.reset} {S.bold}{pnl_hint}{sum_cash_pnl:+.2f}{S.reset} {S.dim}USDC{S.reset}"
        )
        if rows:
            print(S.line("─" * 72))
            print(
                f"{S.dim}Data API Position fields: avgPrice, curPrice, cashPnl, percentPnl, currentValue "
                f"(see docs OpenAPI schema){S.reset}"
            )
            print(S.line("─" * 72))
        show_n = min(50, len(rows))
        for i, row in enumerate(rows[:show_n]):
            if not isinstance(row, dict):
                continue
            title = str(row.get("title", ""))[:72]
            outcome = str(row.get("outcome", ""))[:22]
            size = _safe_float(row.get("size"))
            avg_p = _safe_float(row.get("avgPrice"))
            cur_p = _safe_float(row.get("curPrice"))
            val_f = _safe_float(row.get("currentValue"))
            cash_pnl = _safe_float(row.get("cashPnl"))
            pct_pnl = _safe_float(row.get("percentPnl"))
            idx = f"{S.blue}{i + 1:>2}.{S.reset}"
            title_s = f"{S.bold}{title}{S.reset}"
            out_s = f"{S.yellow}{outcome}{S.reset}"
            pnl_c = _pnl_style(S, cash_pnl)
            # Share prices are 0–1 (Polymarket probability / $ per share).
            detail = (
                f"{S.dim}shares{S.reset} {size:.4f}  "
                f"{S.dim}avg{S.reset} {avg_p:.4f}  {S.dim}→{S.reset} {S.dim}cur{S.reset} {cur_p:.4f}  "
                f"{S.dim}value{S.reset} {val_f:.2f} {S.dim}USDC{S.reset}  "
                f"{S.dim}P&L{S.reset} {pnl_c}{cash_pnl:+.2f} USDC · {pct_pnl:+.1f}%{S.reset}"
            )
            print(f"{idx} {title_s}")
            print(f"    {out_s}  {detail}")
        if len(rows) > 50:
            print(f"{S.dim}… {len(rows) - 50} more (raise --positions-limit up to 500){S.reset}")
        print(S.line("─" * 72))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

