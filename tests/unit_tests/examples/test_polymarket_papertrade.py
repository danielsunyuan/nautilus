from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_compose_routes_papertrade_through_nordvpn():
    compose = yaml.safe_load((ROOT / ".docker" / "docker-compose.yml").read_text())
    services = compose["services"]

    assert "nordvpn" in services
    assert "papertrade" in services
    assert services["papertrade"]["network_mode"] == "service:nordvpn"
    assert services["papertrade"]["build"]["dockerfile"] == ".docker/nautilus_trader.dockerfile"


def test_polymarket_paper_script_uses_sandbox_execution():
    source = (ROOT / "examples" / "live" / "polymarket" / "polymarket_paper_tester.py").read_text()

    assert "PolymarketDataClientConfig" in source
    assert "SandboxExecutionClientConfig" in source
    assert "SandboxLiveExecClientFactory" in source
    assert "PolymarketExecClientConfig" not in source
    assert "PolymarketLiveExecClientFactory" not in source


def test_docker_readme_documents_papertrade_and_vpn():
    readme = (ROOT / ".docker" / "README.md").read_text()

    assert "papertrade" in readme
    assert "nordvpn" in readme.lower()
    assert "prebuilt" in readme.lower()
