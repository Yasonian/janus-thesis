# JANUS orchestrator

A standalone Python package. At each 5-minute decision time `t`, the **input layer** turns
AEMO price forecasts and EV SoC/meter data into one snapshot, the **decision layer** plans
AC charging / DC-BPT export with a greedy receding-horizon planner, and the **live layer**
drives the EVerest SiL (two-context AC + DC-BPT setup with VirtualV2GKey) over MQTT.

## Setup

```bash
cd everest-core/orchestrator
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -e '.[test,plot,milp]'   # versions pinned in pyproject.toml; plot + milp are needed by the experiments
export JANUS_AEMO_DATA_DIR=~/everest-workspace/data/aemo   # this is also the default
```

### AEMO data

The AEMO MMSDM archives are not in the repo. Download the NSW1 PREDISPATCHPRICE and
DISPATCHPRICE archives for January and February 2026 into `$JANUS_AEMO_DATA_DIR` and unzip them:

```bash
cd ~/everest-workspace/data/aemo
B=https://nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/2026
for M in 01 02; do
  curl -fLO "$B/MMSDM_2026_$M/MMSDM_Historical_Data_SQLLoader/PREDISP_ALL_DATA/PUBLIC_ARCHIVE%23PREDISPATCHPRICE%23ALL%23FILE01%2320260${M}010000.zip"
  curl -fLO "$B/MMSDM_2026_$M/MMSDM_Historical_Data_SQLLoader/DATA/PUBLIC_ARCHIVE%23DISPATCHPRICE%23FILE01%2320260${M}010000.zip"
done
for f in *.zip; do unzip -o "$f"; done
```

Files are found by the globs in `config/inputs.yaml` (`data.pd_glob`, …).

### Step 1: build the forecast error table

The planner's price adjustment reads `data/error_distributions.json`, which is generated,
not committed. Build it once after downloading the data (and before running the tests):

```bash
.venv/bin/python -m orchestrator.inputs.error_dist    # -> data/error_distributions.json, data/error_distributions_summary.csv
.venv/bin/python -m pytest
```

Only forecast/actual pairs whose target interval lies in `error_distributions.train_window`
(`inputs.yaml`, January 2026) are used, so February stays out of sample.

## Reproducing the experiments

Offline experiments (no SiL needed) write to `experiments/<experiment>/` (the two E4/E5
variants to `live_logs/e4_sell_only/` and `live_logs/e5_tou_150/`). Run them in this
order, because E8 reads the E4 and E5 results and E7 uses the E8 solver:

```bash
.venv/bin/python -m orchestrator.decision.experiments e3              # E3 switch-duration sensitivity
.venv/bin/python -m orchestrator.decision.experiments e4              # E4 cost-blind ablation
.venv/bin/python -m orchestrator.decision.experiments e4-sell-only    # E4 variant: sell-side adjustment only
.venv/bin/python -m orchestrator.decision.experiments e5              # E5 fixed time-of-use schedule ($300)
.venv/bin/python -m orchestrator.decision.experiments e5-tou150       # E5 variant: $150 threshold
.venv/bin/python -m orchestrator.decision.perfect_foresight --dc-charge   # E8 perfect-foresight MILP
.venv/bin/python -m orchestrator.decision.out_of_sample               # E7 out-of-sample February 2026
```

- **January days** (E2–E5, E8): Jan 3, 7, 10, 11 and 25, set in `decision/experiments.py` (`DAYS`).
- **February days** (E7): picked by `out_of_sample.py`. Every complete February session
  (18:00 → 07:00) is ranked by the standard deviation of its actual 5-min prices, excluding NSW
  public holidays; the picks are the most volatile, ranks 2 and 4, and the flattest.

Live SiL experiments (E1, E2) need the EVerest SiL below:

```bash
# E1: end-to-end switching test (live.yaml defaults: synthetic spike on Jan 11, preset Naive)
.venv/bin/python -m orchestrator.live.run_e2e --everest-config config/config-sil-janus-2ctx-bpt.yaml --duration 200

# E2: one full session per day x {Baseline, A, B, C}, e.g. Jan 10 preset A
.venv/bin/python -m orchestrator.live.run_e2e --until-departure --data real --sim-start 2026-01-10T18:00 --preset A --tag jan10_A
.venv/bin/python -m orchestrator.live.analyze_runs live_logs/*_jan10_* --session 2026-01-10 --out live_logs/jan10_results
```

Each live run is collected in `live_logs/<timestamp>[_tag]/`.

## EVerest SiL setup

- **Environment.** The EVerest devcontainer (`janus-dev-net`, Linux) with `everest-core`
  mounted at `/workspace/everest-core`, plus the mosquitto broker container
  `everest-workspace_devcontainer-mqtt-server-1` (port 1883 published on the host).
- **Build.** The JANUS modules (`modules/Simulation/VirtualV2GKey`,
  `modules/Simulation/V2GKeyBspBridge`, `interfaces/v2g_key_control.yaml`,
  `types/v2g_key.yaml`) must be built into `./build/dist/bin/manager` inside the container
  (standard EVerest CMake/Ninja build).
- **Network.** `config-sil-janus-2ctx-bpt.yaml` binds each HLC stack to its own veth pair.
  These do not survive a container restart; without them `dc_hlc` aborts with
  `Ethernet interface was not found!`. Recreate them after every `docker start janus-dev-net`:

  ```bash
  docker exec -u root janus-dev-net bash -c '
    ip link add veth-ac-secc type veth peer name veth-ac-car
    ip link add veth-dc-secc type veth peer name veth-dc-car
    for i in veth-ac-secc veth-ac-car veth-dc-secc veth-dc-car; do
      sysctl -w net.ipv6.conf.$i.disable_ipv6=0 >/dev/null 2>&1 || true
      ip link set $i up
    done'
  sleep 4   # let IPv6 duplicate-address detection finish (addresses must not be "tentative")
  ```

- **One manager at a time.** Two managers on the same broker interfere. Check with
  `docker exec janus-dev-net pgrep -af bin/manager` before a run; `run_e2e.py` kills any
  leftover manager itself.
- **Manual capture** (optional, outside `run_e2e.py`):
  `docker exec everest-workspace_devcontainer-mqtt-server-1 mosquitto_sub -h localhost -q 1 -v -t "everest/#"`.

## Module structure

| Module | Role |
|---|---|
| `inputs/aemo_csv.py` | Generic C/I/D reader (`read_cid_csv`) and the table specs mapping each table to canonical columns `run_id, published, target, region, rrp`. PD and DISPATCHPRICE are verified on real files; P5MIN_REGIONSOLUTION is a stub spec. |
| `inputs/sources.py` | `PriceSource` and `SocSource` protocols: archive/replay implementations and the message-fed live implementations. |
| `inputs/forecast.py` | Merges P5MIN (first 60 min) with PD (after that) and resamples to 5-minute slots, with lead times, invalid-price handling and gap reporting. |
| `inputs/soc.py` | `estimate_soc`: a SoC range (lower, upper) from a direct reading, the meter, the user-entered SoC, or the minimum SoC. |
| `inputs/error_dist.py`, `inputs/error_lookup.py` | Build the forecast error table from the AEMO archives, and look up error quantiles. |
| `inputs/config.py` | Loads `config/inputs.yaml` into typed settings, with nested overrides. |
| `inputs/snapshot.py` | `InputLayer.get_snapshot(t)` and the status flags. |
| `inputs/replay.py` | The replay driver and its CLI. |
| `decision/planner.py` | `plan(snapshot, ModeState, preset)`: price adjustment, greedy schedule, safety overrides, mode derivation. Only the current slot's action and mode command are executed (receding horizon). |
| `decision/config.py` | Loads `config/decision.yaml`: battery, planner, mode/dwell, presets, session. |
| `decision/scenario_runner.py` | Offline replay of one plug-in session (18:00 → 07:00) with the planner in the loop, scored at actual dispatch prices against a plain-AC baseline. |
| `decision/experiments.py` | E3, E4 and E5 runners, ToU strategy. |
| `decision/perfect_foresight.py` | E8 perfect-foresight MILP (scipy/HiGHS). |
| `decision/out_of_sample.py` | E7 February test. |
| `live/` | MQTT orchestrator node, price feeder, EV responder, battery model (`plant.py`), switching guard, E2E runner and run analysis. |
| `scenarios/build_synthetic_spike.py` | Builds `scenarios/synthetic_spike/{pd,dispatch}.csv` (the E1 input) from Jan 11 data. |
| `config/inputs.yaml`, `config/decision.yaml`, `config/live.yaml` | **All** thresholds and parameters. |

## Input layer rules

- **No look-ahead.** Only runs with `LASTCHANGED <= t` are used, and only the latest such run of each type. A run published exactly at `t` counts as available. `assemble()` raises an error if it is ever handed a future run.
- **Horizon.** It starts at the slot ending `t+5` (the interval in progress) and runs to the slot containing departure. With no departure, or once departure has passed, it runs to the end of the available forecast.
- **Resampling.** A PD value for the 30-minute interval ending `T` is copied to the six slots ending `T-25 … T`. In the first PD interval, slots that have already ended are dropped.
- **Merging.** Slot by slot, P5MIN is used for slots ending `<= t+60 min` and PD otherwise. If P5MIN lacks a slot, that slot falls back to PD.
- **Staleness.** Age is `t − LASTCHANGED` of the latest available run, and every threshold is a strict `>` comparison.

## `get_snapshot(t)` output

```python
from orchestrator.inputs.replay import build_layer
layer = build_layer()                       # loads every PD file in JANUS_AEMO_DATA_DIR
snap = layer.get_snapshot(datetime(2026, 1, 1, 4, 10))   # t must be on the 5-min grid
```

`Snapshot` (call `snap.to_dict()` for a JSON-ready form; there, timedeltas become minutes):

| Field | Content |
|---|---|
| `t` | Decision time (naive datetime, NEM time) |
| `forecast` | List of `ForecastPoint`, one per 5-minute slot, in time order (fields below) |
| `unknown` | List of `UnknownPeriod(start, end, reason)` with reason `"beyond_horizon"` (the forecast ends before departure) or `"missing"` (a hole, e.g. an invalid price) |
| `runs` | `{"PD": RunInfo, "P5MIN": RunInfo}`: the latest available run of each type (`run_id, published, age_minutes, used`), or `None` |
| `latest_dispatch_rrp` | The latest actual dispatch price with `LASTCHANGED <= t`, $/MWh, or `None` |
| `latest_dispatch_interval` | That price's interval end. On the 5-minute grid this is normally `t`: the price for `(t, t+5]` is published ~9 s after `t`, so it isn't available yet. Prices revised for manifestly incorrect inputs (MII) only appear at their revision time |
| `discarded_prices` | `{"PD": n}`: rows in the runs used that fell outside floor/cap or were NaN |
| `soc` | `SocEstimate(lower, upper, method, anchor_time, energy_since_anchor_kwh, invalid_readings, uncertain)`, plus `.width` and `.mid` |
| `user` | `UserSettings(departure_time, target_soc, min_soc, battery_capacity_kwh, preset, user_soc)` |
| `departure` | The departure time resolved to an absolute datetime |
| `flags` | `StatusFlags` (below). `flags.active()` lists the flags that are set |

Each `ForecastPoint` has:

| Field | Content |
|---|---|
| `interval_end` | The target slot (interval-ending) |
| `rrp` | Price in $/MWh (`.rrp_per_kwh` gives $/kWh) |
| `source` | `"PD"` or `"P5MIN"` |
| `run_id` | The ID of the run the value came from |
| `published` | That run's LASTCHANGED |
| `lead_time` | `interval_end − published` |
| `native_interval_end` | For PD, the 30-minute interval the value came from |
| `native_lead_time` | That 30-minute interval's own lead time, so PD error distributions can be looked up at native resolution |

Status flags (the layer **reports** these; it never acts on them):

| Flag | Set when |
|---|---|
| `p5min_unavailable` | No P5MIN run has `LASTCHANGED <= t` |
| `p5min_stale` | The latest P5MIN is older than `p5min_max_age_minutes`, so PD is used for the near term |
| `pd_unavailable` | No PD run has `LASTCHANGED <= t` |
| `pd_stale` | The latest PD is older than `pd_max_age_minutes`. It is still used |
| `safe_mode` | Every price source is older than `safe_mode_max_age_minutes`, or no price data exists at all |
| `horizon_gap` | The forecast ends before departure (see `unknown`) |
| `departure_passed` | `t >= departure`. The forecast is returned to the end of the horizon |
| `missing_intervals` | There are holes inside the horizon |
| `invalid_prices` | Prices outside `[price_floor, price_cap]` were discarded |
| `soc_uncertain` | `soc.upper − soc.lower > uncertain_width_pct` |
| `soc_invalid_reading` | SoC readings outside 0–100 were discarded |

## Running an input replay

```bash
.venv/bin/python -m orchestrator.inputs.replay                       # config default range
.venv/bin/python -m orchestrator.inputs.replay --start 2026-01-10T16:00 --end 2026-01-11T08:00 \
    --departure 07:30 --soc-csv my_session.csv --out snaps.jsonl
```

`--soc-csv` columns are `time,mode,meter_energy_kwh,soc_reading`. `mode` is `AC` or `DC`, the
meter value is cumulative and signed (+ means energy into the EV), and `soc_reading` is left
blank when there is no reading. Without that file, SoC falls back to the user-entered value
or the minimum SoC.

From Python, with your own decision logic:

```python
from orchestrator.inputs.replay import build_layer, replay
layer = build_layer(user=load_config().user.with_overrides(departure_time="07:30", preset="A"))
for snap in replay(layer, start, end):
    decide(snap)
```

Starting before the first PD run in the archive gives `pd_unavailable` and `safe_mode`.

## Forecast error distributions

- **How the table is built.** Each PD row is paired with the actual price for its interval: the mean of the six 5-minute dispatch prices in that half-hour. Intervals with fewer than six are dropped. Every run is kept, so one interval appears once per run.
- **What is stored.** Pairs are grouped by lead-time bin (0–2, 2–6, 6–12, 12–24, 24+ h) and by *forecast*-price bin (<0, 0–100, 100–300, 300–1000, >1000). Bins are `[lo, hi)`. Bins below $300 store `error = actual − forecast` ($/MWh); the $300–1000 and >$1000 bins store `ratio = actual / forecast` (ratios break near zero). Each group has a `kind` field (`absolute` or `ratio`). `get_error_adjustment()` always returns an additive $/MWh adjustment; for ratio groups it is `forecast × (ratio_q − 1)`.
- **Lookup.** `inputs/error_lookup.get_error_adjustment(lead_h, forecast, "q90")` returns the stored quantile. Out-of-range inputs clamp to the edge bins.
- **Thin groups.** A group with fewer than `error_distributions.min_count` pairs (`inputs.yaml`) uses its merged group instead. Merges are across lead times only (`LEAD_MERGES` in `error_dist.py`). If the merged group is also thin, the lookup falls back to the lead bin's `"all"` group.
- **Non-independence.** One target interval is forecast by many runs, so `count` overstates the independent evidence.

## Decision layer

- **Presets.** `config/decision.yaml` `presets`: each maps to a (sell, buy) quantile pair from the error table; `Naive: null` uses the raw forecast on both sides (the cost-blind setting). A side set to `null` in an override uses the raw forecast on that side (sell-only variant).
- **Energy.** Exporting draws `export_power × dt` from the battery. Charging adds `charge_power × dt` to the battery and buys that amount divided by η from the grid; η is applied once.
- **Profit.** `actual_profit` / `profit_vs_baseline` is the saving against plain AC charging from plug-in until the target is reached: `baseline_cost + export_revenue − charge_cost`.
- **The runner owns the true SoC.** The input layer sees what a charger would: metered energy all the time, and a SoC reading only in DC-BPT.
- **Minimum margin.** `planner.minimum_margin_per_mwh` applies to every preset. An export is only scheduled if `sell − buy/η − degradation×1000 > margin`, where `buy` is its buy-back slot (or, for SoC above target, the cheapest known buy left in the horizon).
- **SoC floor and export budget.** The safety pass flips any export that would take the lower SoC bound below `battery.min_soc_pct`. `planner.export_buffer_pct` limits the total export energy one plan may schedule to `(100 − min_soc − buffer)%` of capacity.
- **SoC clamping.** An estimate above 100% clamps both bounds to 100, which gives a zero-width range. See `test_overfull_estimate_clamps_to_100`.
- **`ac_charge_efficiency`** (`inputs.yaml`) converts metered AC kWh to battery kWh for the SoC estimate.

## Live MQTT layer (`orchestrator/live/`)

Four processes, all configured in `config/live.yaml`. **Only the orchestrator node sends commands to EVerest**, and its only command is `set_power_path`.

| Process | Role |
|---|---|
| `live/node.py` | The orchestrator. It runs `plan()` once per decision cycle on live inputs and owns the system mode. It sends `set_power_path` only when the trigger guard passes, and publishes `janus/orchestrator/{state (retained), decision, switch_log}` plus `janus/clock`. |
| `live/price_feeder.py` | Replays PD runs on `janus/prices/forecast` as the decision clock passes each run's LASTCHANGED. |
| `live/ev_responder.py` | SiL stand-in for the car. It plugs in on AC; after each `switch_complete` it unplugs the car of the context that was left and starts the session on the new one, publishing its delay on `janus/ev_responder/event`. |
| `live/run_e2e.py` | End-to-end runner. It starts the EVerest manager and all three processes, captures traffic at the broker, and collects everything in `live_logs/<timestamp>/`. |

- **Live sources.** `LivePriceSource` and `LiveSocSource` in `inputs/sources.py` are message-fed buffers with the same no-look-ahead rule as the replay sources. SoC readings come from `dc_hlc/…/display_parameters.present_soc`, which exists in DC-BPT only and is whole-percent. Energy comes from both contexts' `powermeter` totals, summed.
- **Trigger guard (mandatory).** A switch is sent only when all of these hold:
  - the target differs from the current mode;
  - the active context's `session_info.state` is Charging or a deliberate pause, received within `max_state_age_s`;
  - no `switch_in_progress` is open on either key;
  - no earlier command is still pending.
- **Which key is commanded.** The node commands the V2G-Key of the context being *left*: each instance pulses only its own Control Pilot. The key's own `current_mode` label is not synced between the two instances, so the command label is adjusted when necessary (`switching.command_label`).
- **Decision clock.** One 5-minute decision cycle runs every `wall_seconds_per_cycle` wall seconds, starting when the plug-in session reaches Charging. Only the decision cadence is compressed; switching and charging run in real time.
- **Switch log.** Headline metric: command → new context `session_info.state == Charging`. The `ChargingStarted` session event is also logged for ms resolution, along with CP transitions, `elapsed_ms`, and the responder delay.

### Known upstream limitation: VirtualV2GKey per-context mode state

- **What it is.** Each `VirtualV2GKey` instance (`ac_v2g_key`, `dc_v2g_key`) keeps its own `current_mode`. It starts as `"AC"` and is never synced with the other instance or with the real EvseManager. `set_power_path(mode)` returns false, with no CP pulse, when `mode` equals that label.
- **How the orchestrator handles it.** The orchestrator keeps the system mode itself, commands the key of the context being left, and sends the other label when the honest one would be rejected (`switching.command_label`). The CP pulse is direction-agnostic, so the effect is the same. Every adjusted label is logged (`label_adjusted`).
- **Proper fix.** In VirtualV2GKey's C++: the key should track the real per-context mode.
