import sys
import importlib.util
from pathlib import Path


def _load(name, fname):
    p = Path("examples/live/polymarket") / fname
    spec = importlib.util.spec_from_file_location(name, p.resolve())
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


client = _load("examples.live.polymarket.sports_odds_client", "sports_odds_client.py")


def test_american_to_implied_prob_positive():
    # +150 → 100/(150+100) = 0.400
    assert abs(client.american_to_implied_prob(150) - 0.400) < 0.001


def test_american_to_implied_prob_negative():
    # -200 → 200/(200+100) = 0.667
    assert abs(client.american_to_implied_prob(-200) - 0.667) < 0.001


def test_has_clv_edge_underpriced():
    # Polymarket 0.60, Vegas implied 0.70 → gap=0.10 >= threshold=0.05 → edge
    assert client.has_clv_edge(polymarket_ask=0.60, vegas_implied=0.70, min_edge=0.05)


def test_has_clv_edge_overpriced():
    # Polymarket 0.72, Vegas implied 0.68 → gap=-0.04 < threshold → no edge
    assert not client.has_clv_edge(polymarket_ask=0.72, vegas_implied=0.68, min_edge=0.05)


def test_has_clv_edge_no_vegas_data():
    # No Vegas data available — should not block entry (return True)
    assert client.has_clv_edge(polymarket_ask=0.65, vegas_implied=None, min_edge=0.05)
