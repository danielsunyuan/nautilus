# Sports CLOB Settlement and Forward Pipeline Design

**Date:** 2026-07-29
**Status:** Approved for execution under the sports research mega goal
**Scope:** Research collection and Nautilus sandbox paper infrastructure only

## Objective

Replace name-and-price-based Gamma sports settlement with exact CLOB token
evidence, then collect a pre-registered ATP/WTA moneyline forward window
without adding another long-running process to the memory-constrained OGMA
node.

The acceptance window is the later of:

- 30 calendar days after the first complete observation; and
- 300 resolved outcome-token observations with complete venue and bookmaker
  data.

No live-capital strategy can be produced by this pipeline.

## Settlement authority

Polymarket's current CLOB V2 public client exposes
`GET /markets/{condition_id}`. A resolved response includes the condition ID,
`closed=true`, and one token with `winner=true`. This is the authoritative
settlement source.

The resolver will query by the exact condition ID already persisted with the
paper position. It will accept a resolution only when:

1. the returned condition ID exactly matches the request;
2. the market is closed;
3. exactly one token has `winner=true`; and
4. the paper entry has a non-empty token ID.

Settlement will compare token IDs, not normalized outcome names. The
settlement row will persist the winning token ID and outcome, CLOB endpoint,
and observation timestamp. Network failures, malformed payloads, no winner,
multiple winners, condition mismatches, and legacy entries without token IDs
remain unresolved.

Three approaches were considered:

- **Direct CLOB REST polling — selected.** Exact, public, restart-safe, and
  already implemented by the official V2 Python client.
- **CLOB WebSocket `market_resolved`.** Lower latency, but events can be missed
  during disconnects and still require REST recovery.
- **Direct on-chain event indexing.** Strong provenance but excessive RPC,
  negative-risk, and reorganization complexity for a paper-research resolver.

Named ATP/WTA result sources are reconciliation evidence for the research
dataset; they do not override CLOB settlement.

## Forward observation collection

OGMA already runs `collector-market-snapshots`, which polls public CLOB books
every 60 seconds and uses about 68 MiB. OGMA has 911 MiB total memory and the
operating guide prohibits another service. The observer will therefore extend
that existing process.

Discovery will use current Gamma sports metadata:

- `/sports` supplies ATP and WTA series IDs and named resolution sources;
- `/events/keyset` supplies active events and embedded markets for each series;
- only singles moneylines with a valid future game start are eligible.

The observer will retain the existing general snapshots and add one append-only
row per `(condition_id, token_id, checkpoint)` for:

- `t_minus_60m`;
- `t_minus_15m`.

The checkpoint window is the first successful 60-second poll at or after the
intended decision time and before the next checkpoint or game start. A durable
JSONL key scan on startup prevents duplicate checkpoint rows after restarts.

Each checkpoint row records the schema-v1 identifiers; best bid/ask and their
sizes; the walked ask for a fixed five-share research quantity; tick size;
minimum order size; the CLOB V2 fee curve; raw bookmaker quotes; de-vigged
consensus; freshness; and explicit missing-data reasons.

## Bookmaker and missing-data policy

The Odds API remains the first provider because the current sports code already
uses its event vocabulary. Raw h2h quotes will be retained for every matched
bookmaker and both players. De-vigging is performed per complete two-outcome
book, then the consensus is the median of bookmaker probabilities.

`THE_ODDS_API_KEY` is not currently present on OGMA. The collector will still
deploy safely and write venue observations with
`bookmaker_api_key_missing`; these rows are incomplete and do not start the
300-resolution complete-data counter. The forward acceptance clock starts only
with the first row containing complete venue and bookmaker data.

Outcome matching is fail closed. Exact normalized names may match
automatically; aliases or ambiguous matches require a versioned mapping and
cannot be inferred from prices.

## Storage and processing

OGMA writes daily JSONL under
`/home/ubuntu/collectors/outputs/sports_forward/`. The existing Atlas pull
mirrors it into the 5 TB archive after the transfer script and health checks
are extended for the new product.

Atlas owns settlement enrichment, completeness reporting, and final
evaluation. Raw OGMA rows are append-only. Resolution enrichment produces a
derived dataset rather than rewriting observations.

The evaluation will report:

- complete and excluded rows by reason;
- consensus probability and CLOB mid calibration;
- hypothetical five-share execution at the stored walked ask;
- actual CLOB V2 fee curve;
- gross, fee, and net P/L;
- results by ATP/WTA, checkpoint, price band, tournament, and calendar week;
- sensitivity to maximum quote age and bookmaker count.

No strategy is frozen unless the untouched window has positive aggregate net
P/L, positive net P/L at both checkpoints, at least 100 resolved observations
in each of ATP and WTA or an explicitly narrowed follow-up protocol, and no
single tournament or week contributes more than half of total profit.

## Failure handling and operations

- Every missing input is recorded; nothing fills forward from a later sample.
- CLOB resolution ambiguity stays unresolved.
- Bookmaker absence never degrades into a Polymarket-only edge claim.
- The existing snapshot product must remain fresh during deployment.
- OGMA deployment uses a clean SHA-addressed runtime checkout; its dirty
  operational checkout is not reset or overwritten.
- Atlas mirror freshness, parseability, growth, and storage are verified before
  collection is called healthy.
- The paper strategy, if the gate eventually passes, uses Nautilus live data
  plus sandbox execution, one `TradingNode` per process, and no live execution
  factory.

## External references

- Polymarket CLOB market channel:
  `https://docs.polymarket.com/api-reference/wss/market`
- Polymarket sports metadata:
  `https://docs.polymarket.com/api-reference/sports/get-sports-metadata-information`
- Polymarket CLOB V2 migration:
  `https://docs.polymarket.com/v2-migration`
- Official Python V2 client:
  `https://github.com/Polymarket/py-clob-client-v2`
