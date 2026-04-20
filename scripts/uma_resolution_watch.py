from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from uma.clients.subgraph import UmaSubgraphClient
from uma.linking import DEFAULT_CLOB_HOST, link_resolution_snapshots, resolve_question_id_candidates
from uma.monitor import build_resolution_alerts
from scripts.polymarket_resolution_candidates import (
    build_candidate_rows,
    _default_price_fetcher_factory,
    gamma_market_snapshot,
)
from scripts.polymarket_balance import (
    _DEFAULT_DATA_API,
    _DEFAULT_GAMMA_API,
    _fetch_data_api_positions,
    _pick_best_user_for_positions,
    _resolve_data_api_user_candidates,
)
from strategies.resolution_flow import rank_resolution_proposals


def _build_cycle(
    *,
    subgraph_url: str,
    clob_host: str,
    gamma_host: str,
    data_api: str,
    uma_voter_address: str | None,
) -> Callable[[], dict[str, Any]]:
    subgraph = UmaSubgraphClient(base_url=subgraph_url)

    def cycle() -> dict[str, Any]:
        snapshots = subgraph.active_requests()
        now = datetime.now(timezone.utc)

        candidates, _ = _resolve_data_api_user_candidates()
        best_user, _ = _pick_best_user_for_positions(data_base=data_api, candidates=candidates)
        rows = _fetch_data_api_positions(data_base=data_api, user=best_user, limit=500) if best_user else []
        gamma_fetcher = lambda slug: gamma_market_snapshot(gamma_host=gamma_host, slug=slug, timeout=15.0)
        price_fetcher = _default_price_fetcher_factory(clob_host)
        built_candidates = build_candidate_rows(rows, gamma_fetcher=gamma_fetcher, price_fetcher=price_fetcher)
        public_candidates = resolve_question_id_candidates(
            snapshots,
            gamma_host=gamma_host,
            clob_host=clob_host,
            scan_fallback=False,
        )
        linked = link_resolution_snapshots(snapshots, built_candidates + public_candidates)
        proposals = rank_resolution_proposals(linked, now=now)
        alerts = build_resolution_alerts(linked, proposals=proposals, now=now)
        account = subgraph.account_summary(uma_voter_address or "") if uma_voter_address else None
        return {
            "account": account,
            "linked": [row.to_dict() for row in linked],
            "alerts": [alert.to_dict() for alert in alerts],
            "proposals": [proposal.to_dict() for proposal in proposals],
        }

    return cycle


def run_watch(
    *,
    cycle_fetcher: Callable[[], dict[str, Any]],
    once: bool,
    poll_seconds: float,
    emit_json: bool,
    print_fn: Callable[[str], None] = print,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    while True:
        payload = cycle_fetcher()
        if emit_json:
            print_fn(json.dumps(payload, indent=2, sort_keys=True))
        else:
            alerts = payload.get("alerts") or []
            for alert in alerts:
                print_fn(f"{alert['kind']} {alert['request_id']} {alert['summary']}")
        if once:
            return 0
        sleep_fn(max(0.0, poll_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch Polymarket-linked UMA resolution activity in read-only mode.")
    parser.add_argument("--env-file", default=".env.polymarket")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--only-held", action="store_true", default=True)
    parser.add_argument("--subgraph-url", default="")
    parser.add_argument("--uma-voter-address", default="")
    parser.add_argument("--clob-host", default=DEFAULT_CLOB_HOST)
    parser.add_argument("--gamma-host", default=_DEFAULT_GAMMA_API)
    parser.add_argument("--data-api", default=_DEFAULT_DATA_API)
    args = parser.parse_args()

    env_path = Path(args.env_file).resolve()
    if env_path.exists():
        load_dotenv(env_path, override=False)

    subgraph_url = (args.subgraph_url or os.environ.get("UMA_SUBGRAPH_URL") or "").strip()
    if not subgraph_url:
        raise SystemExit("FAIL: UMA_SUBGRAPH_URL missing")

    cycle = _build_cycle(
        subgraph_url=subgraph_url,
        clob_host=(args.clob_host or os.environ.get("POLYMARKET_CLOB_HOST") or DEFAULT_CLOB_HOST).strip(),
        gamma_host=args.gamma_host,
        data_api=args.data_api,
        uma_voter_address=(args.uma_voter_address or os.environ.get("UMA_VOTER_ADDRESS") or "").strip() or None,
    )
    return run_watch(
        cycle_fetcher=cycle,
        once=bool(args.once),
        poll_seconds=args.poll_seconds,
        emit_json=bool(args.json),
    )


if __name__ == "__main__":
    raise SystemExit(main())
