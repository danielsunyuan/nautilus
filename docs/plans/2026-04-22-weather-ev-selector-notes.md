# Weather City EV Selector — Design Notes

**Date:** 2026-04-22
**File:** `examples/live/polymarket/weather_city_ev_selector.py`

---

## What This Module Does

`weather_city_ev_selector` is a **pure algorithmic scaffold** for selecting the best Polymarket weather market to enter per city per day, ranked by net expected value.

### It DOES:

- Define a `CandidateSignal` dataclass representing a single market/side being considered (city, date, slug, threshold, CLOB mid, estimated win probability, fees, slippage).
- Compute net EV per unit stake via `ev(candidate)`:
  - `gross_ev = estimated_prob − (mid + slippage)`
  - `fee ≈ fee_rate × mid × (1 − mid)` (Polymarket peak-at-50c approximation)
  - `net_ev = gross_ev − fee`
- Select at most one candidate per `(city, observation_date)` group via `select_best_city_candidates()`.
- Rank within each group by `net_ev`, not by raw threshold height.
- Reject all candidates in a group when the maximum EV ≤ 0.
- Apply a deterministic tie-break: equal EV (within 1e-9) → prefer lower `threshold_f` (more conservative entry).
- Return a `SelectionResult` per input candidate with `selected`, `reason`, and `ev` fields.

### It Does NOT:

- Make any network calls (no HTTP, no CLOB, no Gamma).
- Import or depend on NautilusTrader (no compiled extensions, no TradingNode).
- Read or write any files (no JSONL, no JSONL, no state).
- Compute `estimated_prob` — that value must be provided by the caller.
- Wire into the confirmed-entry daemon or any live trading loop.
- Know anything about the oracle resolution source; that is the caller's concern.

---

## Future Probability Inputs Required for Live Use

The key input this module cannot produce itself is `estimated_prob` — the calibrated probability that a given token side wins. A real calibration pipeline will need to produce this from a combination of signals. The required inputs are:

### 1. Oracle-matched observation gap (hours)

**What it is:** The number of hours remaining between "now" (UTC) and the observation cutoff for this market's date (typically midnight local time, or market close time).

**Why it matters:** A YES bet on "NYC ≥ 70°F" when 23 hours remain has much more uncertainty than the same bet with 1 hour remaining and current temperature already at 72°F. The probability should converge toward certainty as the day progresses and more readings are available.

### 2. Local hour (0–23, city-local time)

**What it is:** The current hour of day at the city's local timezone.

**Why it matters:** Temperature follows a diurnal cycle. At 02:00 local a daily high of 75°F is nearly impossible. At 14:00 local (typical afternoon peak), the probability is nearly determined by current readings. The calibration model must account for where in the cycle the city currently sits.

### 3. Observation count (int)

**What it is:** The number of temperature readings recorded so far today from the oracle station (WU/NOAA/HKO, per the market ruleset).

**Why it matters:** More observations = tighter confidence interval on the daily maximum. Zero observations early in the day means relying entirely on climatological priors and forecasts. Fifteen observations by afternoon means near-certain knowledge of the current trajectory.

### 4. Source type (WU / NOAA / HKO)

**What it is:** Which oracle the market's ruleset names as the resolution source.

**Why it matters:** Different oracles have different accuracy characteristics, update cadences, and historical consistency with Polymarket resolution decisions:
- **WU (Weather Underground):** Used by 46 of 50 cities. Hourly station history. Can lag by 1–2 hours at some stations. Requires TWC internal API key.
- **NOAA:** Used by Istanbul, Moscow, Tel Aviv. Published daily summaries; update time varies. Historically very accurate for Polymarket resolution.
- **HKO (Hong Kong Observatory):** Used by Hong Kong only. Published at a specific local time; calibration must account for the HKO publication window.

The calibrated probability model may need separate sigmoid fits per oracle type to handle update cadence differences.

### 5. Band type (or_higher / or_lower / exact)

**What it is:** The resolution direction of the market — does it resolve YES if the temperature meets or exceeds the threshold, falls below it, or hits an exact band?

**Why it matters:** For `or_higher` markets the probability increases monotonically with the current reading. For `or_lower` it decreases. For exact-band (A2 strategy) the probability is a two-sided estimate based on how close the current reading is to the band boundary. The same EV formula applies in all cases as long as `estimated_prob` correctly reflects the direction, but the calibration inputs differ.

### 6. Optional external priors: historical hit rate by city + month + threshold

**What it is:** A lookup table giving P(daily high ≥ T | city, month) derived from multi-year historical NOAA/WU data.

**Why it matters:** Early in the day, before enough observations are available, the best prior is climatological history for that city, month, and threshold. This is a weak signal that should be down-weighted as observations accumulate, but it prevents the model from assigning equal probability to wildly different thresholds early in the trading window.

---

## EV Formula Notes

For both YES and NO tokens the formula is the same:

```
entry_cost = mid + slippage
gross_ev   = estimated_prob − entry_cost
fee        = fee_rate × mid × (1 − mid)
net_ev     = gross_ev − fee
```

For a NO token, `estimated_prob` must already be the probability that the NO side wins (i.e., `1 − P(YES)`). The formula does not distinguish token side — the caller must flip the probability correctly.

Polymarket's actual fee formula is `fee = qty × rate × p × (1−p)` (taker only). The per-unit approximation here (`fee_rate × mid × (1−mid)`) is accurate for unit stakes and conservative at price extremes where the true fee approaches zero.

---

## Wiring Plan (Future Work)

When this module is wired into `weather_confirmed_entry_daemon.py`:

1. The daemon's polling loop builds `CandidateSignal` objects from the current CLOB mid-prices and the calibration pipeline outputs.
2. `select_best_city_candidates()` is called once per polling cycle.
3. Only `selected=True` results with `ev > 0` are forwarded to the Nautilus order submission path.
4. The existing A1/A2/B2 temperature confirmation logic remains as a *gate* (do not enter unless temperature confirms), not as the ranking criterion. EV ranking replaces the current "highest threshold wins" heuristic.

The module will require no modification for this wiring step — the interface is already complete.
