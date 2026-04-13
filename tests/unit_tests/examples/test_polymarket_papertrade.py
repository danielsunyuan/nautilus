from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_compose_routes_papertrade_through_nordvpn():
    compose = yaml.safe_load((ROOT / ".docker" / "docker-compose.yml").read_text())
    services = compose["services"]

    assert "nordvpn" in services
    assert "papertrade" in services
    assert "redis" in services
    assert services["papertrade"]["network_mode"] == "service:nordvpn"
    assert services["papertrade"]["build"]["dockerfile"] == ".docker/nautilus_trader.dockerfile"
    assert services["papertrade"]["depends_on"]["redis"]["condition"] == "service_started"
    assert services["papertrade"]["depends_on"]["postgres"]["condition"] == "service_started"
    assert "nautilus-redis:/data" in services["redis"]["volumes"]
    assert services["redis"]["command"][:2] == ["redis-server", "--appendonly"]


def test_polymarket_paper_script_uses_sandbox_execution_and_stable_trader_id():
    source = (ROOT / "examples" / "live" / "polymarket" / "polymarket_paper_tester.py").read_text()

    assert "PolymarketDataClientConfig" in source
    assert "SandboxExecutionClientConfig" in source
    assert "SandboxLiveExecClientFactory" in source
    assert 'TraderId("PAPER-001")' in source
    assert "load_cache=False" in source
    assert "snapshot_orders=True" in source
    assert "snapshot_positions=True" in source
    assert "snapshot_positions_interval_secs=5.0" in source
    assert "use_instance_id=True" in source
    assert "stream_per_topic=False" in source
    assert "PolymarketExecClientConfig" not in source
    assert "PolymarketLiveExecClientFactory" not in source


def test_docker_readme_documents_papertrade_vpn_and_redis_persistence():
    readme = (ROOT / ".docker" / "README.md").read_text()
    lower = readme.lower()

    assert "papertrade" in lower
    assert "nordvpn" in lower
    assert "prebuilt" in lower
    assert "redis-backed persistence" in lower
    assert "redis-cli" in lower
    assert "trader-*" in lower
    assert "xrange" in lower
    assert "redis insight" in lower
    assert "papertrade results" in lower


def test_agents_documents_redis_backed_papertrade_context():
    agents = (ROOT / "AGENTS.md").read_text()
    lower = agents.lower()

    assert "papertrade" in lower
    assert "redis" in lower
    assert "redis-cli" in lower
    assert "trader-*" in lower
    assert "traderid" in lower
