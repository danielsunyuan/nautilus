from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_live_daemon_imports_polymarket_live_exec_client_factory() -> None:
    source = (
        ROOT / "examples" / "live" / "polymarket"
        / "polymarket_weather_daily_temperature_live_daemon.py"
    ).read_text(encoding="utf-8")
    assert "PolymarketExecClientConfig" in source
    assert "PolymarketLiveExecClientFactory" in source
    assert "SandboxExecutionClientConfig" not in source
    assert 'trader_id="LIVE-WEATHER-DAEMON"' in source
