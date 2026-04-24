# Sports Strategy Refinement Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the undifferentiated "enter anything in the price band" sports strategies with targeted presets that exploit confirmed edges (tennis moneyline, NBA/UFC totals) while cutting known bleeders (MLB, NBA spreads, hockey), then stack microstructure filters on top.

**Architecture:** Three phases of additive filters on `SportsStrategyPreset` — (1) sport+market-type whitelist to kill bleeders, (2) time-to-game gate and bid-side depth filter to improve entry quality, (3) Vegas CLV comparison for principled edge confirmation. Each phase produces a new named preset set that can run in parallel with the existing `all` set for A/B comparison. No destructive changes to running infrastructure.

**Tech Stack:** Python 3.13, NautilusTrader (Rust/Cython), Polymarket CLOB/Gamma APIs, httpx, pytest. All code runs inside Docker containers (`nautilus-papertrade:latest`). Polymarket API is **IP-blocked on the host** — any live API call must run inside a VPN container (see CLAUDE.md).

**Test command:**
```bash
cd /home/atlas/EL/nautilus
uv run --extra polymarket --with pytest python -m pytest tests/unit_tests/examples/ -q
```

**Key files:**
```
examples/live/polymarket/
  sports_strategy_library.py      # Preset definitions + entry decision logic
  sports_live_strategy.py         # Nautilus Strategy class (on_quote_tick)
  sports_models.py                # SportsMarket dataclass, classify_price_arena
  sports_resolver.py              # Gamma discovery, SPORTS_TAGS
  sports_settlement.py            # Settlement poller
  sports_report.py                # Markdown report generator
  polymarket_sports_paper_daemon.py  # Long-running daemon + _default_run_round

tests/unit_tests/examples/        # All tests go here (no compiled Nautilus needed)
```

**Data baseline (Apr 20 2026, 588 unique settled trades):**
- Tennis: n=326, WR=77.6%, edge=+5.1pp ✅
- UFC: n=41, WR=90.2%, edge=+13.2pp ✅
- NBA totals: n=20, WR=80%, edge=+28.7pp ✅ (needs more data)
- MLB: n=103, WR=53.4%, edge=-5.5pp ❌
- NBA spreads: n=13, WR=30.8%, edge=-20.4pp ❌
- `basic` beats `band_only` by 5–11pp at every arena ✅

---

## Phase 1 — Sport + Market-Type Whitelist, Retire `band_only`

**Subagent:** Dennis Ritchie (General Coding)

### Task 1: Add `allowed_sports` and `allowed_market_types` to `SportsStrategyPreset`

**Files:**
- Modify: `examples/live/polymarket/sports_strategy_library.py`
- Create: `tests/unit_tests/examples/test_sports_strategy_library.py`

**Context:** `SportsStrategyPreset` is a frozen dataclass with slots in `sports_strategy_library.py:13`. The `should_enter_sports_market` function (line 112) is the pure entry predicate — it currently only checks price band, spread, and ask size. We need to add optional sport/market-type whitelists so a preset can say "only fire on tennis moneyline" without touching the daemon.

`SportsMarket.sport` is a string like `"nba"`, `"tennis"`, `"ufc"`. `SportsMarket.market_type` is `"moneyline"`, `"totals"`, `"spreads"`, `"nrfi"`, etc. These come from `sports_resolver.py`.

**Step 1: Write failing tests**

```python
# tests/unit_tests/examples/test_sports_strategy_library.py
import sys
from pathlib import Path
import importlib.util

def _load(name, fname):
    p = Path("examples/live/polymarket") / fname
    spec = importlib.util.spec_from_file_location(name, p.resolve())
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

lib = _load("examples.live.polymarket.sports_strategy_library", "sports_strategy_library.py")
SportsStrategyPreset = lib.SportsStrategyPreset
should_enter_sports_market = lib.should_enter_sports_market


def _make_preset(**overrides):
    defaults = dict(
        name="test", arena="sports_60c", min_ask=0.60, max_ask=0.70, mode="basic"
    )
    return SportsStrategyPreset(**{**defaults, **overrides})


def test_allowed_sports_blocks_wrong_sport():
    preset = _make_preset(allowed_sports=frozenset({"tennis"}))
    assert not should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100, sport="nba", market_type="moneyline"
    )


def test_allowed_sports_passes_correct_sport():
    preset = _make_preset(allowed_sports=frozenset({"tennis"}))
    assert should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100, sport="tennis", market_type="moneyline"
    )


def test_allowed_market_types_blocks_wrong_type():
    preset = _make_preset(allowed_market_types=frozenset({"totals"}))
    assert not should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100, sport="nba", market_type="spreads"
    )


def test_allowed_market_types_passes_correct_type():
    preset = _make_preset(allowed_market_types=frozenset({"totals"}))
    assert should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100, sport="nba", market_type="totals"
    )


def test_no_whitelist_passes_everything():
    preset = _make_preset()
    assert should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100, sport="mlb", market_type="spreads"
    )


def test_both_whitelists_combined():
    preset = _make_preset(
        allowed_sports=frozenset({"nba"}),
        allowed_market_types=frozenset({"totals"}),
    )
    # nba+totals passes
    assert should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100, sport="nba", market_type="totals"
    )
    # nba+spreads blocked
    assert not should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100, sport="nba", market_type="spreads"
    )
    # tennis+totals blocked
    assert not should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100, sport="tennis", market_type="totals"
    )
```

**Step 2: Run tests to confirm they fail**
```bash
cd /home/atlas/EL/nautilus
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sports_strategy_library.py -v
```
Expected: `TypeError` — `SportsStrategyPreset` doesn't accept `allowed_sports`/`allowed_market_types`.

**Step 3: Add fields and update `should_enter_sports_market`**

In `sports_strategy_library.py`, add two optional fields to `SportsStrategyPreset`:
```python
@dataclass(frozen=True, slots=True)
class SportsStrategyPreset:
    name: str
    arena: str
    min_ask: float
    max_ask: float
    max_spread: float = 0.02
    min_ask_size: float = 50.0
    order_qty: float = 10.0
    mode: str = "band_only"
    allowed_sports: frozenset[str] | None = None        # None = all sports
    allowed_market_types: frozenset[str] | None = None  # None = all market types
```

Update `should_enter_sports_market` signature and add whitelist checks at the top:
```python
def should_enter_sports_market(
    *,
    preset: SportsStrategyPreset,
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
    sport: str = "",
    market_type: str = "",
) -> bool:
    # Sport whitelist
    if preset.allowed_sports is not None and sport not in preset.allowed_sports:
        return False
    # Market type whitelist
    if preset.allowed_market_types is not None and market_type not in preset.allowed_market_types:
        return False
    # Price band
    if ask < preset.min_ask:
        return False
    if ask >= preset.max_ask:
        return False
    if preset.mode == "band_only":
        return True
    if (ask - bid) > preset.max_spread:
        return False
    if ask_size < preset.min_ask_size:
        return False
    return True
```

**Step 4: Run tests — confirm all pass**
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sports_strategy_library.py -v
```
Expected: 7 tests PASS.

**Step 5: Commit**
```bash
git add examples/live/polymarket/sports_strategy_library.py \
        tests/unit_tests/examples/test_sports_strategy_library.py
git commit -m "feat(sports): add allowed_sports + allowed_market_types whitelist to SportsStrategyPreset"
```

---

### Task 2: Wire sport/market_type into `SportsPaperStrategy.on_quote_tick`

**Files:**
- Modify: `examples/live/polymarket/sports_live_strategy.py`
- Modify: `examples/live/polymarket/polymarket_sports_paper_daemon.py`

**Context:** `SportsPaperStrategy.on_quote_tick` currently calls `should_enter_sports_market` without passing `sport` or `market_type` (lines 166–172 of `sports_live_strategy.py`). `SportsPaperStrategyConfig` holds the `preset` but not the market's sport/type. The daemon builds strategy configs in `_default_run_round` (lines 537–555) where it has the full `SportsMarket` object.

**Step 1: Add `sport` and `market_type` to `SportsPaperStrategyConfig`**

In `sports_live_strategy.py`:
```python
class SportsPaperStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    preset: SportsStrategyPreset
    order_qty: Decimal
    sport: str = ""                    # add this
    market_type: str = ""              # add this
    family_instrument_ids: tuple[InstrumentId, ...] = ()
    target_usd_per_market: Decimal | None = None
    min_order_size_shares: Decimal = Decimal("0")
    max_stake_per_market: Decimal | None = None
    max_open_positions: int | None = None
    max_total_open_stake: Decimal | None = None
```

Update `on_quote_tick` to pass them through:
```python
if not should_enter_sports_market(
    preset=self.config.preset,
    bid=bid,
    ask=ask,
    bid_size=bid_size,
    ask_size=ask_size,
    sport=self.config.sport,
    market_type=self.config.market_type,
):
    return
```

**Step 2: Pass sport/market_type from daemon when building strategies**

In `polymarket_sports_paper_daemon.py`, in `_default_run_round`, update the strategy construction:
```python
strategy = SportsPaperStrategy(
    config=SportsPaperStrategyConfig(
        strategy_id=f"SPORTS-{preset.name.upper()}",
        instrument_id=InstrumentId.from_str(inst_id_str),
        preset=preset,
        order_qty=Decimal(str(preset.order_qty)),
        sport=market.sport,            # add this
        market_type=market.market_type, # add this
    ),
)
```

**Step 3: Run full test suite**
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/ -q
```
Expected: all pass, no regressions.

**Step 4: Commit**
```bash
git add examples/live/polymarket/sports_live_strategy.py \
        examples/live/polymarket/polymarket_sports_paper_daemon.py
git commit -m "feat(sports): pass sport/market_type through to entry decision function"
```

---

### Task 3: Add `focused` preset set — tennis + UFC all types, NBA totals only, `basic` mode only

**Files:**
- Modify: `examples/live/polymarket/sports_strategy_library.py`
- Modify: `examples/live/polymarket/polymarket_sports_paper_daemon.py`
- Extend: `tests/unit_tests/examples/test_sports_strategy_library.py`

**Context:** `_strategy_presets_for_set` in the daemon (line 170) routes preset set names. Currently supports `"all"`, `"band_only"`, `"basic"`. We're adding `"focused"`. The `focused` set replaces `band_only` with properly filtered `basic`-mode presets that only fire on known-positive sport+type combos.

**Step 1: Write failing test**

Add to `test_sports_strategy_library.py`:
```python
def test_focused_presets_exist():
    presets = lib.focused_presets()
    assert len(presets) > 0
    # All focused presets are basic mode
    assert all(p.mode == "basic" for p in presets)
    # All focused presets have sport or market_type whitelists
    assert all(
        p.allowed_sports is not None or p.allowed_market_types is not None
        for p in presets
    )


def test_focused_presets_block_mlb():
    presets = lib.focused_presets()
    for preset in presets:
        result = should_enter_sports_market(
            preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100,
            sport="mlb", market_type="moneyline"
        )
        assert not result, f"{preset.name} should not enter mlb moneyline"


def test_focused_presets_block_nba_spreads():
    presets = lib.focused_presets()
    for preset in presets:
        result = should_enter_sports_market(
            preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100,
            sport="nba", market_type="spreads"
        )
        assert not result, f"{preset.name} should not enter nba spreads"


def test_focused_presets_allow_tennis():
    presets = lib.focused_presets()
    # At least one preset allows tennis moneyline in the 60c band
    any_tennis = any(
        should_enter_sports_market(
            preset=p, bid=0.61, ask=0.63, bid_size=100, ask_size=100,
            sport="tennis", market_type="moneyline"
        )
        for p in presets
    )
    assert any_tennis


def test_focused_presets_allow_nba_totals():
    presets = lib.focused_presets()
    any_nba_totals = any(
        should_enter_sports_market(
            preset=p, bid=0.61, ask=0.63, bid_size=100, ask_size=100,
            sport="nba", market_type="totals"
        )
        for p in presets
    )
    assert any_nba_totals
```

**Step 2: Run to confirm they fail**
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sports_strategy_library.py::test_focused_presets_exist -v
```
Expected: `AttributeError: module has no attribute 'focused_presets'`.

**Step 3: Add `focused_presets()` to the library**

In `sports_strategy_library.py`, add after `all_sports_presets()`:
```python
# Sports + market types with confirmed positive edge from Apr 2026 data baseline.
# Tennis: all market types positive (WR 77.6%, +5.1pp edge, n=326)
# UFC: all market types positive (WR 90.2%, +13.2pp edge, n=41)
# NBA: totals only (WR 80%, +28.7pp edge, n=20) — spreads bleed at -20pp
# MLB, hockey: excluded entirely (both negative across all arenas)
_FOCUSED_SPORTS = frozenset({"tennis", "ufc", "nba"})
_FOCUSED_NBA_TYPES = frozenset({"totals"})
_ALL_TYPES = None  # no restriction


def focused_presets() -> tuple[SportsStrategyPreset, ...]:
    """
    Targeted presets based on confirmed edge from baseline data collection.

    Only basic mode (spread + liquidity filter). Only positive sport+type combos.
    Re-run baseline analysis before modifying these whitelists.
    """
    arenas = [
        ("sports_50c", 0.50, 0.60),
        ("sports_60c", 0.60, 0.70),
        ("sports_70c", 0.70, 0.80),
        ("sports_80c", 0.80, 0.90),
        ("sports_90c", 0.90, 0.981),
    ]
    presets = []
    for arena, min_ask, max_ask in arenas:
        # Tennis + UFC: all market types
        presets.append(SportsStrategyPreset(
            name=f"{arena}_focused_tennis_ufc",
            arena=arena,
            min_ask=min_ask,
            max_ask=max_ask,
            mode="basic",
            allowed_sports=frozenset({"tennis", "ufc"}),
            allowed_market_types=None,
        ))
        # NBA: totals only
        presets.append(SportsStrategyPreset(
            name=f"{arena}_focused_nba_totals",
            arena=arena,
            min_ask=min_ask,
            max_ask=max_ask,
            mode="basic",
            allowed_sports=frozenset({"nba"}),
            allowed_market_types=frozenset({"totals"}),
        ))
    return tuple(presets)
```

Add routing in `polymarket_sports_paper_daemon.py` inside `_strategy_presets_for_set`:
```python
# Import focused_presets alongside all_sports_presets at top of file
from examples.live.polymarket.sports_strategy_library import (
    all_sports_presets,
    focused_presets,
    should_enter_sports_market,
)

# In _strategy_presets_for_set:
if normalized == "focused":
    return focused_presets()
```

Also update the fallback importlib block to include `focused_presets`.

**Step 4: Run all tests**
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sports_strategy_library.py -v
```
Expected: all pass.

**Step 5: Commit**
```bash
git add examples/live/polymarket/sports_strategy_library.py \
        examples/live/polymarket/polymarket_sports_paper_daemon.py \
        tests/unit_tests/examples/test_sports_strategy_library.py
git commit -m "feat(sports): add focused preset set — tennis+ufc all types, NBA totals only, basic mode"
```

---

### Task 4: Add `focused` docker-compose service and patch running container

**Files:**
- Modify: `.docker/docker-compose.yml`

**Context:** Current docker-compose has `sports-daemon-vpn` running `--preset-set all`. We add a second service `sports-focused-vpn` running `--preset-set focused`. Both run simultaneously — `all` continues baseline data collection, `focused` runs the filtered strategy for A/B comparison. Service pattern mirrors `papertrade-daemon-vpn` and `ninety-microprice-research-vpn`.

The container does NOT need a rebuild — `sports_strategy_library.py` and `sports_live_strategy.py` are pure Python. Patch them in-place using the standard patch procedure from CLAUDE.md:

```bash
# 1. Copy updated files into the running sports daemon image
docker cp examples/live/polymarket/sports_strategy_library.py \
    nautilus-sports-daemon-vpn:/workspace/examples/live/polymarket/sports_strategy_library.py
docker cp examples/live/polymarket/sports_live_strategy.py \
    nautilus-sports-daemon-vpn:/workspace/examples/live/polymarket/sports_live_strategy.py
docker cp examples/live/polymarket/polymarket_sports_paper_daemon.py \
    nautilus-sports-daemon-vpn:/workspace/examples/live/polymarket/polymarket_sports_paper_daemon.py
```

**Step 1: Add `sports-focused-vpn` service to docker-compose**

Add after the `sports-daemon-vpn` service block:
```yaml
  sports-focused-vpn:
    container_name: nautilus-sports-focused-vpn
    profiles: ["vpn"]
    image: nautilus-papertrade:latest
    user: "${UID:-1000}:${GID:-1000}"
    working_dir: /opt/pysetup
    command:
      - python
      - /workspace/examples/live/polymarket/polymarket_sports_paper_daemon.py
      - --preset-set
      - focused
      - --max-rounds
      - "${SPORTS_FOCUSED_MAX_ROUNDS:-0}"
    init: true
    tty: true
    stdin_open: true
    env_file:
      - ../.env
      - ../.env.polymarket
    environment:
      NAUTILUS_CACHE_HOST: ${NAUTILUS_VPN_CACHE_HOST:-172.22.0.3}
      NAUTILUS_CACHE_PORT: ${NAUTILUS_CACHE_PORT:-6379}
      POLYMARKET_API_SECRET: ${POLYMARKET_API_SECRET:-unused}
      POLYMARKET_PASSPHRASE: ${POLYMARKET_PASSPHRASE:-unused}
      POLYMARKET_CLOB_API_SECRET: ${POLYMARKET_CLOB_API_SECRET:-unused}
      POLYMARKET_CLOB_PASSPHRASE: ${POLYMARKET_CLOB_PASSPHRASE:-unused}
    volumes:
      - ..:/workspace
      - ../outputs:/workspace/outputs
    network_mode: "service:nordvpn"
    depends_on:
      nordvpn:
        condition: service_healthy
      postgres:
        condition: service_started
      redis:
        condition: service_started
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
```

**Step 2: Start the focused daemon**
```bash
cd /home/atlas/EL/nautilus
docker compose -f .docker/docker-compose.yml --profile vpn up -d sports-focused-vpn
docker logs nautilus-sports-focused-vpn --tail=20
```
Expected: daemon starts, logs show `markets_found` in round_start events.

**Step 3: Verify output file is created**
```bash
ls -lt outputs/polymarket/sports/sports_focused_*.jsonl | head -3
```

**Step 4: Commit**
```bash
git add .docker/docker-compose.yml
git commit -m "feat(sports): add sports-focused-vpn daemon service for filtered strategy A/B test"
```

---

## Phase 2 — Time-to-Game Gate + Bid-Side Depth Filter

**Subagent:** Dennis Ritchie (General Coding)

### Task 5: Time-to-game gate on `SportsStrategyPreset`

**Files:**
- Modify: `examples/live/polymarket/sports_strategy_library.py`
- Modify: `examples/live/polymarket/sports_live_strategy.py`
- Modify: `examples/live/polymarket/polymarket_sports_paper_daemon.py`
- Extend: `tests/unit_tests/examples/test_sports_strategy_library.py`

**Context:** `SportsMarket.game_time` is an ISO8601 UTC string. The daemon currently filters out `game_time <= now` (already started) but doesn't filter by how far in the future a game is. Hypothesis: prices sharpen in the last few hours before tip-off. We add `max_hours_before_game: float | None` — skip if `game_time - now > max_hours_before_game`. Pass `game_time` into `should_enter_sports_market` so the pure function can check it.

**Step 1: Write failing tests**

Add to `test_sports_strategy_library.py`:
```python
from datetime import UTC, datetime, timedelta

def test_time_gate_blocks_far_future_game():
    preset = _make_preset(max_hours_before_game=2.0)
    game_time = (datetime.now(tz=UTC) + timedelta(hours=4)).isoformat()
    assert not should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", game_time=game_time,
    )


def test_time_gate_allows_imminent_game():
    preset = _make_preset(max_hours_before_game=2.0)
    game_time = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    assert should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", game_time=game_time,
    )


def test_no_time_gate_passes_any_game_time():
    preset = _make_preset()  # max_hours_before_game=None
    game_time = (datetime.now(tz=UTC) + timedelta(hours=24)).isoformat()
    assert should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", game_time=game_time,
    )


def test_empty_game_time_passes_with_gate():
    """Markets with no game_time (game_time='') should not be blocked by the gate."""
    preset = _make_preset(max_hours_before_game=1.0)
    assert should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=100, ask_size=100,
        sport="tennis", market_type="moneyline", game_time="",
    )
```

**Step 2: Run to confirm failure**
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sports_strategy_library.py::test_time_gate_blocks_far_future_game -v
```

**Step 3: Add `max_hours_before_game` to `SportsStrategyPreset` and update function**

```python
# In SportsStrategyPreset:
max_hours_before_game: float | None = None  # None = no gate

# Updated should_enter_sports_market:
def should_enter_sports_market(
    *,
    preset: SportsStrategyPreset,
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
    sport: str = "",
    market_type: str = "",
    game_time: str = "",
) -> bool:
    # Sport whitelist
    if preset.allowed_sports is not None and sport not in preset.allowed_sports:
        return False
    # Market type whitelist
    if preset.allowed_market_types is not None and market_type not in preset.allowed_market_types:
        return False
    # Time-to-game gate
    if preset.max_hours_before_game is not None and game_time:
        try:
            from datetime import UTC, datetime
            gt = datetime.fromisoformat(game_time.replace("Z", "+00:00"))
            hours_until = (gt - datetime.now(tz=UTC)).total_seconds() / 3600
            if hours_until > preset.max_hours_before_game:
                return False
        except (ValueError, TypeError):
            pass  # unparseable game_time — don't block
    # Price band
    if ask < preset.min_ask:
        return False
    if ask >= preset.max_ask:
        return False
    if preset.mode == "band_only":
        return True
    if (ask - bid) > preset.max_spread:
        return False
    if ask_size < preset.min_ask_size:
        return False
    return True
```

Update `on_quote_tick` in `sports_live_strategy.py` to pass `game_time`. Add `game_time: str = ""` to `SportsPaperStrategyConfig`. In the daemon, set `game_time=market.game_time` when constructing the config.

**Step 4: Run all tests**
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sports_strategy_library.py -v
```

**Step 5: Commit**
```bash
git add examples/live/polymarket/sports_strategy_library.py \
        examples/live/polymarket/sports_live_strategy.py \
        examples/live/polymarket/polymarket_sports_paper_daemon.py \
        tests/unit_tests/examples/test_sports_strategy_library.py
git commit -m "feat(sports): add time-to-game gate to SportsStrategyPreset"
```

---

### Task 6: Bid-side depth filter (sports microprice support)

**Files:**
- Modify: `examples/live/polymarket/sports_strategy_library.py`
- Extend: `tests/unit_tests/examples/test_sports_strategy_library.py`

**Context:** The BTC `microprice_support` strategy is the only near-breakeven strategy in 4,400+ BTC trades (+0.16% avg ROI, 56.6% WR). Its core signal: heavy bid-side liquidity (`bid_size / (bid_size + ask_size) >= support_ratio_threshold`). We port this concept to sports: add optional `min_bid_ratio` field. If set, only enter when bid-side book weight exceeds the threshold. This filters out thin markets where someone is offering at our price but no one is supporting it.

**Step 1: Write failing tests**

```python
def test_bid_ratio_blocks_ask_heavy_book():
    preset = _make_preset(min_bid_ratio=0.55)
    # bid_size=30, ask_size=70 → ratio=0.30 < 0.55 → block
    assert not should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=30, ask_size=70,
        sport="tennis", market_type="moneyline",
    )


def test_bid_ratio_allows_bid_heavy_book():
    preset = _make_preset(min_bid_ratio=0.55)
    # bid_size=70, ask_size=30 → ratio=0.70 >= 0.55 → allow
    assert should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=70, ask_size=30,
        sport="tennis", market_type="moneyline",
    )


def test_bid_ratio_zero_total_size_does_not_crash():
    preset = _make_preset(min_bid_ratio=0.55)
    # both sizes zero — should not divide by zero, should block
    assert not should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=0, ask_size=0,
        sport="tennis", market_type="moneyline",
    )


def test_no_bid_ratio_passes_ask_heavy_book():
    preset = _make_preset()  # min_bid_ratio=None
    assert should_enter_sports_market(
        preset=preset, bid=0.61, ask=0.63, bid_size=10, ask_size=90,
        sport="tennis", market_type="moneyline",
    )
```

**Step 2: Run to confirm failure**

**Step 3: Implement**

Add to `SportsStrategyPreset`:
```python
min_bid_ratio: float | None = None  # bid_size/(bid_size+ask_size) threshold; None=no gate
```

Add to `should_enter_sports_market` (after the ask_size check):
```python
# Bid-side depth gate
if preset.min_bid_ratio is not None:
    total_size = bid_size + ask_size
    if total_size <= 0:
        return False
    if bid_size / total_size < preset.min_bid_ratio:
        return False
```

Add a `depth_focused_presets()` function — same as `focused_presets()` but with `min_bid_ratio=0.55` on each preset. Add `"depth-focused"` routing in `_strategy_presets_for_set`.

**Step 4: Run all tests**
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sports_strategy_library.py -v
```

**Step 5: Commit**
```bash
git add examples/live/polymarket/sports_strategy_library.py \
        examples/live/polymarket/polymarket_sports_paper_daemon.py \
        tests/unit_tests/examples/test_sports_strategy_library.py
git commit -m "feat(sports): add bid-side depth filter (min_bid_ratio) to SportsStrategyPreset"
```

---

## Phase 3 — Vegas Closing Line Value (CLV) Comparison

**Subagent:** Ada Lovelace (Backend Architect)

### Task 7: Vegas odds client

**Files:**
- Create: `examples/live/polymarket/sports_odds_client.py`
- Create: `tests/unit_tests/examples/test_sports_odds_client.py`

**Context:** The Odds API (https://the-odds-api.com) provides real-time sportsbook odds in JSON. Free tier: 500 requests/month. Endpoint: `GET https://api.the-odds-api.com/v4/sports/{sport_key}/odds/?apiKey=KEY&regions=us&markets=h2h&oddsFormat=american`. We convert American odds to implied probability and compare to Polymarket ask. If `polymarket_ask < implied_prob - threshold` → underpriced → enter. If overpriced → skip. Requires `THE_ODDS_API_KEY` env var.

Key mapping: `sport_key` — `basketball_nba`, `tennis_atp`, `tennis_wta`, `mma_mixed_martial_arts`. `h2h` = moneyline. `totals` market returns over/under lines.

**Step 1: Write tests with mock responses**

```python
# tests/unit_tests/examples/test_sports_odds_client.py
import sys, importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

def _load(name, fname):
    p = Path("examples/live/polymarket") / fname
    spec = importlib.util.spec_from_file_location(name, p.resolve())
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

client = _load("examples.live.polymarket.sports_odds_client", "sports_odds_client.py")


def test_american_to_implied_prob_positive():
    # +150 → 1/(1+1.5) = 0.400
    assert abs(client.american_to_implied_prob(150) - 0.400) < 0.001


def test_american_to_implied_prob_negative():
    # -200 → 200/(200+100) = 0.667
    assert abs(client.american_to_implied_prob(-200) - 0.667) < 0.001


def test_has_clv_edge_underpriced():
    # Polymarket 0.60, Vegas implied 0.70 → gap=0.10 > threshold=0.05 → edge
    assert client.has_clv_edge(polymarket_ask=0.60, vegas_implied=0.70, min_edge=0.05)


def test_has_clv_edge_overpriced():
    # Polymarket 0.72, Vegas implied 0.68 → gap=-0.04 < threshold → no edge
    assert not client.has_clv_edge(polymarket_ask=0.72, vegas_implied=0.68, min_edge=0.05)


def test_has_clv_edge_no_vegas_data():
    # No Vegas data available — should not block entry (return True by default)
    assert client.has_clv_edge(polymarket_ask=0.65, vegas_implied=None, min_edge=0.05)
```

**Step 2: Implement `sports_odds_client.py`**

```python
"""
Vegas odds fetcher for CLV comparison.

Fetches h2h (moneyline) and totals odds from The Odds API and converts to
implied probabilities for comparison against Polymarket ask prices.

Requires env var THE_ODDS_API_KEY. Returns None gracefully if key is missing
or API call fails — callers should treat None as "no data, don't block entry".
"""
from __future__ import annotations
import os
from typing import Any

ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"

POLYMARKET_SPORT_TO_ODDS_API: dict[str, str] = {
    "nba": "basketball_nba",
    "tennis": "tennis_atp",   # ATP — WTA is "tennis_wta"
    "ufc": "mma_mixed_martial_arts",
    "mlb": "baseball_mlb",
}


def american_to_implied_prob(american_odds: float) -> float:
    """Convert American odds to implied probability (no vig removal)."""
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return abs(american_odds) / (abs(american_odds) + 100)


def has_clv_edge(
    *,
    polymarket_ask: float,
    vegas_implied: float | None,
    min_edge: float = 0.05,
) -> bool:
    """
    Return True if Polymarket is underpriced vs Vegas by at least min_edge,
    OR if no Vegas data is available (don't block on missing data).
    """
    if vegas_implied is None:
        return True
    return (vegas_implied - polymarket_ask) >= min_edge


async def fetch_implied_prob(
    *,
    sport: str,
    home_team: str,
    away_team: str,
    outcome_name: str,
    http_client: Any,
    market: str = "h2h",
) -> float | None:
    """
    Fetch implied probability for a specific outcome from The Odds API.
    Returns None if API key missing, call fails, or team not found.
    """
    api_key = os.getenv("THE_ODDS_API_KEY")
    if not api_key:
        return None
    sport_key = POLYMARKET_SPORT_TO_ODDS_API.get(sport)
    if not sport_key:
        return None
    try:
        resp = await http_client.get(
            f"{ODDS_API_BASE}/{sport_key}/odds/",
            params={"apiKey": api_key, "regions": "us", "markets": market, "oddsFormat": "american"},
        )
        resp.raise_for_status()
        events = resp.json()
    except Exception:
        return None
    for event in events:
        teams = {event.get("home_team", "").lower(), event.get("away_team", "").lower()}
        if home_team.lower() not in teams and away_team.lower() not in teams:
            continue
        for bookmaker in event.get("bookmakers", [])[:3]:  # use first 3 books, average
            for mkt in bookmaker.get("markets", []):
                if mkt.get("key") != market:
                    continue
                for outcome in mkt.get("outcomes", []):
                    if outcome.get("name", "").lower() in outcome_name.lower() or \
                       outcome_name.lower() in outcome.get("name", "").lower():
                        return american_to_implied_prob(float(outcome["price"]))
    return None
```

**Step 3: Run tests**
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/test_sports_odds_client.py -v
```

**Step 4: Commit**
```bash
git add examples/live/polymarket/sports_odds_client.py \
        tests/unit_tests/examples/test_sports_odds_client.py
git commit -m "feat(sports): add Vegas CLV odds client with American→implied prob conversion"
```

---

### Task 8: Add `min_clv_edge` to preset + wire into strategy

**Files:**
- Modify: `examples/live/polymarket/sports_strategy_library.py`
- Modify: `examples/live/polymarket/sports_live_strategy.py`
- Modify: `examples/live/polymarket/polymarket_sports_paper_daemon.py`

**Context:** This is a bigger integration. `should_enter_sports_market` is currently synchronous — CLV lookup is async. The cleanest approach: pre-fetch Vegas implied probs once per round in the daemon and pass them as a `vegas_implied: float | None` argument to `should_enter_sports_market`. The strategy config gets the pre-fetched value; the pure entry function just compares.

**Step 1: Add `min_clv_edge` to `SportsStrategyPreset`**
```python
min_clv_edge: float | None = None  # None = no CLV gate; 0.05 = require 5pp Polymarket discount vs Vegas
```

**Step 2: Add `vegas_implied` parameter to `should_enter_sports_market`**
```python
def should_enter_sports_market(
    *,
    preset,
    bid, ask, bid_size, ask_size,
    sport="", market_type="", game_time="",
    vegas_implied: float | None = None,
) -> bool:
    ...
    # CLV gate (after time gate, before price band)
    if preset.min_clv_edge is not None:
        if not has_clv_edge(
            polymarket_ask=ask,
            vegas_implied=vegas_implied,
            min_edge=preset.min_clv_edge,
        ):
            return False
    ...
```

**Step 3: Pre-fetch Vegas data in the daemon**

In `_default_run_round`, before building strategies:
```python
# Pre-fetch Vegas implied probs for all markets (one async batch)
vegas_cache: dict[str, float | None] = {}
if any(p.min_clv_edge is not None for p in presets):
    for market in markets:
        key = f"{market.slug}:{market.outcome_name}"
        vegas_cache[key] = await fetch_implied_prob(
            sport=market.sport,
            outcome_name=market.outcome_name,
            ...
        )
```

Pass `vegas_implied=vegas_cache.get(key)` into `SportsPaperStrategyConfig`.

**Step 4: Add `clv_focused` preset set**

A version of `focused_presets()` with `min_clv_edge=0.05` — only enter when Polymarket is ≥5pp below Vegas.

**Step 5: Add `THE_ODDS_API_KEY` to env documentation in CLAUDE.md**

**Step 6: Run all tests, commit**
```bash
uv run --extra polymarket --with pytest python -m pytest \
  tests/unit_tests/examples/ -q
git add -A
git commit -m "feat(sports): wire Vegas CLV gate into SportsStrategyPreset and daemon"
```

---

## Phase 4 — Kelly Sizing + Game-Family Position Limits

**Subagent:** Dennis Ritchie (General Coding)

### Task 9: Kelly criterion position sizing

**Files:**
- Modify: `examples/live/polymarket/sports_strategy_library.py`
- Modify: `examples/live/polymarket/sports_live_strategy.py`

**Context:** Currently all trades are flat `order_qty=10` shares regardless of edge. `SportsPaperStrategyConfig.target_usd_per_market` and `_compute_order_quantity` already support dollar-based sizing — we just need to set it from estimated edge. Add `kelly_edge_estimate: float | None` to `SportsStrategyPreset`. If set, compute Kelly stake: `f = edge / (1 - entry_price)`, cap at `max_kelly_fraction` (default 0.25 = quarter-Kelly). Convert to dollars via `target_usd_per_market`.

```python
def kelly_stake_usd(
    *,
    edge: float,           # e.g. 0.12 for 12pp edge
    entry_price: float,    # e.g. 0.65
    bankroll_usd: float,   # e.g. 1000.0
    max_fraction: float = 0.25,  # quarter-Kelly cap
) -> float:
    """Full Kelly fraction capped at max_fraction of bankroll."""
    if entry_price >= 1.0 or edge <= 0:
        return 0.0
    full_kelly = edge / (1.0 - entry_price)
    fraction = min(full_kelly, max_fraction)
    return bankroll_usd * fraction
```

Add tests for `kelly_stake_usd`, add `kelly_edge_estimate` and `kelly_max_fraction` to preset, use in `_compute_order_quantity`.

### Task 10: Game-family position cap

**Files:**
- Modify: `examples/live/polymarket/sports_live_strategy.py`
- Modify: `examples/live/polymarket/polymarket_sports_paper_daemon.py`

**Context:** `SportsPaperStrategyConfig.family_instrument_ids` already exists but is never populated. In the daemon, markets from the same game share a slug prefix (e.g. `nba-bos-mia-2026-04-20-*`). Group markets by game, pass all instrument IDs for that game as `family_instrument_ids`. `_family_has_existing_risk()` in the strategy then prevents entering both Over AND Under on the same game, or entering the same game from multiple presets.

---

## Running the Analysis After Each Phase

After deploying each phase, re-run the focused analysis:

```bash
python3 /home/atlas/EL/nautilus/outputs/polymarket/analysis/nba_50c_edge_analysis.py
```

And compare `sports_focused_*.jsonl` vs `sports_all_*.jsonl` settlement results. Gate before drawing conclusions: **≥200 unique settled trades** in the focused file before Phase 2 is evaluated.

---

## CLAUDE.md Update

After all tasks, add a `## Sports Strategy Presets` section to `nautilus/CLAUDE.md` documenting:
- What `focused` / `depth-focused` / `clv-focused` preset sets contain
- How to add a new preset set
- The data baseline (Apr 2026) that informed the whitelist
