from __future__ import annotations

import argparse
from collections.abc import Generator
from dataclasses import dataclass
import gzip
import json
from pathlib import Path
from typing import Any

try:
    from examples.live.polymarket.polymarket_clob_capture import BookLevel
    from examples.live.polymarket.polymarket_clob_capture import DerivedDepthSnapshot
    from examples.live.polymarket.polymarket_clob_capture import DerivedTradeEvent
    from examples.live.polymarket.polymarket_clob_capture import PolymarketMarketMetadata
except ModuleNotFoundError:
    from polymarket_clob_capture import BookLevel
    from polymarket_clob_capture import DerivedDepthSnapshot
    from polymarket_clob_capture import DerivedTradeEvent
    from polymarket_clob_capture import PolymarketMarketMetadata


@dataclass(frozen=True)
class NautilusExportResult:
    instrument_count: int
    depth_count: int
    trade_count: int
    quote_count: int


def load_capture_records(root: Path) -> Generator[dict[str, Any], None, None]:
    if not root.exists():
        return
    paths = list(root.rglob("*.jsonl")) + list(root.rglob("*.jsonl.gz"))
    for path in sorted(paths):
        if path.suffix == ".gz":
            raw = gzip.open(path, "rt", encoding="utf-8").read().splitlines()
        else:
            raw = path.read_text(encoding="utf-8").splitlines()
        for line in raw:
            if line.strip():
                yield json.loads(line)


def _build_metadata_market_info(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "condition_id": record["condition_id"],
        "question": record["question"],
        "minimum_tick_size": float(record["minimum_tick_size"] or "0.001"),
        "minimum_order_size": float(record["minimum_order_size"] or "1"),
        "end_date_iso": record["end_date_iso"] or "2099-12-31T00:00:00Z",
        "tokens": [{"token_id": record["token_id"], "outcome": record["outcome"]}],
    }


def export_capture_to_catalog(
    *,
    normalized_root: Path,
    catalog_path: Path,
    include_instruments: bool = True,
    include_depth: bool = True,
    include_trades: bool = True,
    include_quotes: bool = False,
) -> NautilusExportResult:
    from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument
    from nautilus_trader.model.data import BookOrder
    from nautilus_trader.model.data import OrderBookDepth10
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.model.data import TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(catalog_path))
    instrument_by_token: dict[str, Any] = {}
    instruments: list[Any] = []
    if include_instruments:
        for record in load_capture_records(normalized_root / "metadata"):
            market_info = _build_metadata_market_info(record)
            instrument = parse_polymarket_instrument(
                market_info=market_info,
                token_id=record["token_id"],
                outcome=record["outcome"],
                ts_init=0,
            )
            instrument_by_token[record["token_id"]] = instrument
            instruments.append(instrument)
        if instruments:
            catalog.write_data(instruments)

    def get_instrument(token_id: str):
        instrument = instrument_by_token.get(token_id)
        if instrument is None:
            raise ValueError(f"Missing instrument metadata for token {token_id}")
        return instrument

    depth_rows: list[Any] = []
    trade_rows: list[Any] = []
    quote_rows: list[Any] = []

    if include_depth:
        for record in load_capture_records(normalized_root / "depth"):
            instrument = get_instrument(record["token_id"])
            bids = [_make_book_order(instrument, side=OrderSide.BUY, level=level) for level in record["bids"]]
            asks = [_make_book_order(instrument, side=OrderSide.SELL, level=level) for level in record["asks"]]
            depth_rows.append(
                OrderBookDepth10(
                    instrument_id=instrument.id,
                    bids=bids,
                    asks=asks,
                    bid_counts=[1] * len(bids),
                    ask_counts=[1] * len(asks),
                    flags=0,
                    sequence=0,
                    ts_event=int(record["event_ts_ms"]) * 1_000_000,
                    ts_init=int(record["receive_ts_ns"]),
                ),
            )
            if include_quotes and record["best_bid_price"] and record["best_ask_price"]:
                quote_rows.append(
                    QuoteTick(
                        instrument_id=instrument.id,
                        bid_price=instrument.make_price(float(record["best_bid_price"])),
                        ask_price=instrument.make_price(float(record["best_ask_price"])),
                        bid_size=instrument.make_qty(float(record["best_bid_size"])),
                        ask_size=instrument.make_qty(float(record["best_ask_size"])),
                        ts_event=int(record["event_ts_ms"]) * 1_000_000,
                        ts_init=int(record["receive_ts_ns"]),
                    ),
                )
        if depth_rows:
            catalog.write_data(depth_rows)
        if quote_rows:
            catalog.write_data(quote_rows)

    if include_trades:
        for record in load_capture_records(normalized_root / "trades"):
            instrument = get_instrument(record["token_id"])
            trade_rows.append(
                TradeTick(
                    instrument_id=instrument.id,
                    price=instrument.make_price(float(record["price"])),
                    size=instrument.make_qty(float(record["size"])),
                    aggressor_side=AggressorSide.BUYER if record.get("side") == "BUY" else AggressorSide.SELLER,
                    trade_id=f"{record['token_id']}-{record['event_ts_ms']}-{record['receive_ts_ns']}",
                    ts_event=int(record["event_ts_ms"]) * 1_000_000,
                    ts_init=int(record["receive_ts_ns"]),
                ),
            )
        if trade_rows:
            catalog.write_data(trade_rows)

    return NautilusExportResult(
        instrument_count=len(instruments),
        depth_count=len(depth_rows),
        trade_count=len(trade_rows),
        quote_count=len(quote_rows),
    )


def _make_book_order(instrument: Any, *, side: Any, level: dict[str, Any] | BookLevel) -> Any:
    from nautilus_trader.model.data import BookOrder

    if isinstance(level, dict):
        price = level["price"]
        size = level["size"]
    else:
        price = level.price
        size = level.size
    return BookOrder(
        side=side,
        price=instrument.make_price(float(price)),
        size=instrument.make_qty(float(size)),
        order_id=0,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalized-root", required=True)
    parser.add_argument("--catalog-path", required=True)
    parser.add_argument("--no-instruments", action="store_true")
    parser.add_argument("--no-depth", action="store_true")
    parser.add_argument("--no-trades", action="store_true")
    parser.add_argument("--include-quotes", action="store_true")
    return parser


def _main() -> None:
    args = _build_parser().parse_args()
    result = export_capture_to_catalog(
        normalized_root=Path(args.normalized_root),
        catalog_path=Path(args.catalog_path),
        include_instruments=not args.no_instruments,
        include_depth=not args.no_depth,
        include_trades=not args.no_trades,
        include_quotes=args.include_quotes,
    )
    print(result)


if __name__ == "__main__":
    _main()
