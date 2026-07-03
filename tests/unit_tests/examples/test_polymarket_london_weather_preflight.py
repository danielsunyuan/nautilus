from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


preflight = _load_module(
    "examples.live.polymarket.polymarket_london_weather_preflight",
    ROOT / "examples" / "live" / "polymarket" / "polymarket_london_weather_preflight.py",
)


def _write_model_path(tmp_path: Path) -> Path:
    model_path = tmp_path / "research" / "weather"
    model_path.mkdir(parents=True)
    (model_path / "__init__.py").write_text("", encoding="utf-8")
    return model_path


def _valid_fixture() -> dict:
    return {
        "model_snapshot": {
            "target_local_date": "2026-06-01",
            "forecast_horizon_days": 1,
            "market_line": 19.0,
            "model_version": "family_b_forecast_error_calibrated_v1",
            "predicted_probability": 0.62,
            "raw_predicted_probability": 0.618,
            "training_row_count": 240,
        },
        "markets": [
            {
                "city": "London",
                "metric": "high",
                "band_type": "or_higher",
                "condition_id": "0xlondon",
                "yes_token_id": "yes-token",
                "no_token_id": "no-token",
                "active": True,
                "accepting_orders": True,
                "resolution_source": "London City Airport / Wunderground EGLC",
                "quote": {
                    "yes_bid": 0.54,
                    "yes_ask": 0.56,
                    "no_bid": 0.43,
                    "no_ask": 0.45,
                    "spread": 0.02,
                    "yes_bid_size": 80,
                    "yes_ask_size": 100,
                    "no_bid_size": 90,
                    "no_ask_size": 120,
                    "timestamp": "2026-05-30T12:00:00Z",
                },
            },
        ],
    }


def _write_fixture(tmp_path: Path, payload: dict | None = None) -> Path:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(payload or _valid_fixture()), encoding="utf-8")
    return fixture_path


def test_blocks_by_default_without_explicit_live_data(tmp_path: Path) -> None:
    model_path = _write_model_path(tmp_path)
    fixture_path = _write_fixture(tmp_path)

    report = preflight.run_preflight(
        env={"WEATHER_RESEARCH_PATH": str(model_path)},
        fixture_path=fixture_path,
        no_network=True,
    )

    assert report["live_data_status"] == "blocked"
    assert report["execution_mode"] == "sandbox_only"
    assert report["ready_for_paper_round"] is False


def test_live_execution_truthy_blocks_even_when_live_data_ready(tmp_path: Path) -> None:
    model_path = _write_model_path(tmp_path)
    fixture_path = _write_fixture(tmp_path)

    report = preflight.run_preflight(
        env={
            "POLYMARKET_LIVE_DATA_READY": "yes",
            "POLYMARKET_FORCE_LIVE_EXECUTION": "1",
            "WEATHER_RESEARCH_PATH": str(model_path),
        },
        fixture_path=fixture_path,
        no_network=True,
    )

    assert report["live_data_status"] == "blocked"
    assert report["execution_mode"] == "sandbox_only"
    assert "live execution" in " ".join(report["blocking_reasons"]).lower()
    assert report["ready_for_paper_round"] is False


def test_private_key_is_not_required_for_fixture_preflight(tmp_path: Path) -> None:
    model_path = _write_model_path(tmp_path)
    fixture_path = _write_fixture(tmp_path)

    report = preflight.run_preflight(
        env={
            "POLYMARKET_LIVE_DATA_READY": "yes",
            "WEATHER_RESEARCH_PATH": str(model_path),
        },
        fixture_path=fixture_path,
        no_network=True,
    )

    assert report["execution_mode"] == "sandbox_only"
    assert report["ready_for_paper_round"] is True
    assert report["accepted_markets"][0]["threshold_f"] == 19.0
    assert report["accepted_markets"][0]["observation_date"] == "2026-06-01"
    assert report["accepted_markets"][0]["yes_ask"] == 0.56
    assert report["accepted_markets"][0]["no_ask"] == 0.45
    assert "private" not in json.dumps(report).lower()


def test_missing_model_path_blocks_with_exact_path(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing" / "weather"
    fixture_path = _write_fixture(tmp_path)

    report = preflight.run_preflight(
        env={
            "POLYMARKET_LIVE_DATA_READY": "yes",
            "WEATHER_RESEARCH_PATH": str(missing_path),
        },
        fixture_path=fixture_path,
        no_network=True,
    )

    assert report["model_status"] == "blocked"
    assert str(missing_path) in report["blocking_reasons"]
    assert report["ready_for_paper_round"] is False


def test_deterministic_family_b_snapshot_succeeds_from_fixture(tmp_path: Path) -> None:
    model_path = _write_model_path(tmp_path)
    fixture_path = _write_fixture(tmp_path)

    report = preflight.run_preflight(
        env={
            "POLYMARKET_LIVE_DATA_READY": "yes",
            "WEATHER_RESEARCH_PATH": str(model_path),
        },
        fixture_path=fixture_path,
        no_network=True,
    )

    assert report["model_snapshot_status"] == "passed"
    assert report["model_snapshot"]["model_version"] == "family_b_forecast_error_calibrated_v1"
    assert report["model_snapshot"]["predicted_probability"] == 0.62


def test_exact_bucket_market_is_rejected_until_supported(tmp_path: Path) -> None:
    model_path = _write_model_path(tmp_path)
    fixture = _valid_fixture()
    fixture["markets"][0]["band_type"] = "exact"
    fixture_path = _write_fixture(tmp_path, fixture)

    report = preflight.run_preflight(
        env={
            "POLYMARKET_LIVE_DATA_READY": "yes",
            "WEATHER_RESEARCH_PATH": str(model_path),
        },
        fixture_path=fixture_path,
        no_network=True,
    )

    assert report["market_discovery_status"] == "blocked"
    assert "unsupported_exact_bucket" in report["rejected_markets"][0]["reason"]
    assert report["ready_for_paper_round"] is False


def test_resolution_blocks_unless_eglc_or_wunderground(tmp_path: Path) -> None:
    model_path = _write_model_path(tmp_path)
    fixture = _valid_fixture()
    fixture["markets"][0]["resolution_source"] = "London Heathrow airport"
    fixture_path = _write_fixture(tmp_path, fixture)

    report = preflight.run_preflight(
        env={
            "POLYMARKET_LIVE_DATA_READY": "yes",
            "WEATHER_RESEARCH_PATH": str(model_path),
        },
        fixture_path=fixture_path,
        no_network=True,
    )

    assert report["resolution_status"] == "blocked"
    assert report["ready_for_paper_round"] is False


def test_market_data_blocks_when_quote_fields_are_missing(tmp_path: Path) -> None:
    model_path = _write_model_path(tmp_path)
    fixture = _valid_fixture()
    fixture["markets"][0]["quote"].pop("timestamp")
    fixture_path = _write_fixture(tmp_path, fixture)

    report = preflight.run_preflight(
        env={
            "POLYMARKET_LIVE_DATA_READY": "yes",
            "WEATHER_RESEARCH_PATH": str(model_path),
        },
        fixture_path=fixture_path,
        no_network=True,
    )

    assert report["market_data_status"] == "blocked"
    assert "market_data_not_ready" in report["rejected_markets"][0]["reason"]
    assert report["ready_for_paper_round"] is False


def test_ready_for_paper_round_requires_every_hard_gate(tmp_path: Path) -> None:
    model_path = _write_model_path(tmp_path)
    fixture_path = _write_fixture(tmp_path)

    report = preflight.run_preflight(
        env={
            "POLYMARKET_LIVE_DATA_READY": "yes",
            "WEATHER_RESEARCH_PATH": str(model_path),
            "UNRELATED_RUNTIME_SETTING": "ignored",
            "OGMA_HOST": "ignored",
        },
        fixture_path=fixture_path,
        no_network=True,
    )

    assert report["live_data_status"] == "passed"
    assert report["model_status"] == "passed"
    assert report["model_snapshot_status"] == "passed"
    assert report["market_discovery_status"] == "passed"
    assert report["resolution_status"] == "passed"
    assert report["market_data_status"] == "passed"
    assert report["ready_for_paper_round"] is True
    assert "ogma" not in json.dumps(report).lower()


def test_live_preflight_can_build_snapshot_from_accepted_live_markets(tmp_path: Path) -> None:
    model_path = _write_model_path(tmp_path)
    market = _valid_fixture()["markets"][0]

    def build_snapshot(*, accepted_markets, model_path):
        assert str(model_path).endswith("weather")
        assert accepted_markets[0]["condition_id"] == "0xlondon"
        return [
            {
                "target_local_date": accepted_markets[0]["observation_date"],
                "forecast_horizon_days": 1,
                "market_line": accepted_markets[0]["threshold_f"],
                "model_version": "family_b_forecast_error_calibrated_v1",
                "predicted_probability": 0.64,
                "raw_predicted_probability": 0.63,
                "training_row_count": 53109,
            },
        ]

    report = preflight.run_preflight(
        env={
            "POLYMARKET_LIVE_DATA_READY": "yes",
            "WEATHER_RESEARCH_PATH": str(model_path),
        },
        live_markets=[market],
        live_model_snapshot_builder=build_snapshot,
        no_network=False,
    )

    assert report["ready_for_paper_round"] is True
    assert report["model_snapshot_status"] == "passed"
    assert report["model_snapshot"][0]["predicted_probability"] == 0.64


def test_preflight_skips_bad_market_when_another_market_is_ready(tmp_path: Path) -> None:
    model_path = _write_model_path(tmp_path)
    fixture = _valid_fixture()
    bad_market = dict(fixture["markets"][0])
    bad_market["condition_id"] = "0xbad"
    bad_market["quote"] = {}
    fixture["markets"].append(bad_market)
    fixture_path = _write_fixture(tmp_path, fixture)

    report = preflight.run_preflight(
        env={
            "POLYMARKET_LIVE_DATA_READY": "yes",
            "WEATHER_RESEARCH_PATH": str(model_path),
        },
        fixture_path=fixture_path,
        no_network=True,
    )

    assert report["market_data_status"] == "passed"
    assert report["ready_for_paper_round"] is True
    assert len(report["accepted_markets"]) == 1
    assert any(item["reason"] == "market_data_not_ready" for item in report["rejected_markets"])
