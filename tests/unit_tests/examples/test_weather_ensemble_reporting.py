from __future__ import annotations

from pathlib import Path

from examples.live.polymarket.polymarket_weather_ensemble_reporting import build_weather_ensemble_summary
from examples.live.polymarket.polymarket_weather_ensemble_reporting import merge_entries_with_settlements
from examples.live.polymarket.polymarket_weather_ensemble_reporting import render_weather_ensemble_markdown
from examples.live.polymarket.polymarket_weather_ensemble_reporting import run_report


def _entry(
    *,
    market_slug: str,
    city: str,
    threshold: float,
    token_side: str = "yes",
    selected_side: str = "yes",
    model_yes_probability: float = 0.7,
    market_yes_price: float = 0.58,
    entry_price: float = 0.58,
    timestamp: str = "2026-04-23T07:00:00+00:00",
) -> dict:
    return {
        "event": "strategy_result",
        "run_id": "run-1",
        "strategy_name": "weather_ensemble_baseline",
        "market_slug": market_slug,
        "city": city,
        "threshold": threshold,
        "band_type": "daily_high_or_higher",
        "forecast_source": "open-meteo-ensemble",
        "model_yes_probability": model_yes_probability,
        "market_yes_price": market_yes_price,
        "edge": round(model_yes_probability - market_yes_price, 6),
        "selected_side": selected_side,
        "token_side": token_side,
        "confidence": max(model_yes_probability, 1.0 - model_yes_probability),
        "entry_price": entry_price,
        "shares": 5.0,
        "stake": round(entry_price * 5.0, 2),
        "resolved": False,
        "timestamp": timestamp,
    }


def test_merge_entries_with_settlements_resolves_matching_entry() -> None:
    entry = _entry(market_slug="tokyo-19c-apr-23", city="Tokyo", threshold=19.0)
    settlement = {
        "event": "settlement_update",
        "entry_id": "tokyo-19c-apr-23|2026-04-23T07:00:00+00:00|yes|weather_ensemble_baseline",
        "market_slug": "tokyo-19c-apr-23",
        "strategy_name": "weather_ensemble_baseline",
        "token_side": "yes",
        "settlement_price": 1.0,
        "settlement_source": "clob_last_trade_price",
        "resolved": True,
        "resolved_outcome": "win",
        "pnl": 2.1,
    }

    merged = merge_entries_with_settlements([entry, settlement])

    assert len(merged) == 1
    assert merged[0]["resolved"] is True
    assert merged[0]["settlement_source"] == "clob_last_trade_price"
    assert merged[0]["pnl"] == 2.1


def test_build_summary_aggregates_mixed_cities_thresholds_and_partial_rows() -> None:
    rows = [
        {"event": "market_snapshot", "market_slug": "tokyo-19c-apr-23"},
        {"event": "market_snapshot", "market_slug": "busan-18c-apr-23"},
        _entry(market_slug="tokyo-19c-apr-23", city="Tokyo", threshold=19.0, model_yes_probability=0.76, market_yes_price=0.61),
        _entry(market_slug="busan-18c-apr-23", city="Busan", threshold=18.0, token_side="no", selected_side="no", model_yes_probability=0.22, market_yes_price=0.37, entry_price=0.63, timestamp="2026-04-23T07:05:00+00:00"),
        {"event": "strategy_result", "market_slug": "partial-row"},
        {
            "event": "settlement_update",
            "entry_id": "tokyo-19c-apr-23|2026-04-23T07:00:00+00:00|yes|weather_ensemble_baseline",
            "market_slug": "tokyo-19c-apr-23",
            "strategy_name": "weather_ensemble_baseline",
            "token_side": "yes",
            "settlement_price": 1.0,
            "settlement_source": "clob_last_trade_price",
            "resolved": True,
            "resolved_outcome": "win",
            "pnl": 2.1,
        },
    ]

    summary = build_weather_ensemble_summary(rows)

    assert summary["totals"]["scanned_markets"] == 3
    assert summary["totals"]["entered_positions"] == 2
    assert summary["totals"]["resolved_trades"] == 1
    assert summary["totals"]["unresolved_positions"] == 1
    assert summary["totals"]["net_pnl"] == 2.1
    by_city = {row["_key"]: row for row in summary["city_breakdown"]}
    assert by_city["Tokyo"]["resolved_wins"] == 1
    assert by_city["Busan"]["unresolved"] == 1
    by_threshold = {row["_key"]: row for row in summary["threshold_breakdown"]}
    assert "19.0|daily_high_or_higher" in by_threshold
    assert summary["forecast_sources"] == ["open-meteo-ensemble"]
    assert summary["settlement_sources"] == ["clob_last_trade_price"]


def test_render_markdown_and_run_report_handle_unresolved_only(tmp_path: Path) -> None:
    rows = [
        _entry(market_slug="guangzhou-29c-apr-23", city="Guangzhou", threshold=29.0),
    ]
    summary = build_weather_ensemble_summary(rows)
    markdown = render_weather_ensemble_markdown(summary)

    assert "Polymarket Weather Ensemble Report" in markdown
    assert "Open Positions" in markdown
    assert "Guangzhou" in markdown

    report_root = tmp_path / "outputs"
    jsonl_dir = report_root / "polymarket" / "weather_ensemble"
    jsonl_dir.mkdir(parents=True)
    (jsonl_dir / "weather_ensemble_20260423T070000Z.jsonl").write_text(
        "\n".join(
            [
                '{"event":"strategy_result","strategy_name":"weather_ensemble_baseline","market_slug":"guangzhou-29c-apr-23","city":"Guangzhou","threshold":29.0,"band_type":"daily_high_or_higher","forecast_source":"open-meteo-ensemble","model_yes_probability":0.67,"market_yes_price":0.58,"edge":0.09,"selected_side":"yes","token_side":"yes","confidence":0.67,"entry_price":0.58,"shares":5.0,"stake":2.9,"resolved":false,"timestamp":"2026-04-23T07:00:00+00:00"}'
            ],
        ),
        encoding="utf-8",
    )

    report_md = tmp_path / "WEATHER_ENSEMBLE_RESULTS.md"
    run_report(report_root=str(report_root), report_md=str(report_md))

    rendered = report_md.read_text(encoding="utf-8")
    assert "Guangzhou" in rendered
    assert "Forecast Sources" in rendered
