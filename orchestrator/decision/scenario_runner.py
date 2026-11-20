"""Scenario runner: replay a plug-in session with the planner in the loop.

    python -m orchestrator.decision.scenario_runner                 # all scenarios x presets
    python -m orchestrator.decision.scenario_runner --scenario flat --preset A

Each 5-min step: get_snapshot(t) -> plan(...) -> execute the current action over
(t, t+5] at the ACTUAL dispatch price for that slot. The runner owns the true
battery SoC and feeds the input layer what a charger would see: metered energy
always, a direct SoC reading only while in DC-BPT.

Money (all $, actual dispatch prices):
  export_revenue  = price/1000 * kWh_exported - degradation * kWh_exported
  charge_cost     = price/1000 * kWh_into_battery / eta
  net_cashflow    = export_revenue - charge_cost
  baseline_cost   = cost of plain AC charging from plug-in until target (no planner)
  actual_profit   = baseline_cost + net_cashflow   (saving vs the uncontrolled baseline)
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

from orchestrator.inputs.aemo_csv import DISPATCH_SPEC, PD_SPEC, load_tables
from orchestrator.inputs.config import InputConfig, load_config
from orchestrator.inputs.error_lookup import ErrorLookup, default_lookup
from orchestrator.inputs.replay import find_files
from orchestrator.inputs.snapshot import InputLayer
from orchestrator.inputs.sources import ArchivePriceSource, SocSample

from .config import DecisionConfig, load_decision_config
from .planner import AC, DC_BPT, Action, ModeCommand, ModeState, plan

ORCH_DIR = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ORCH_DIR / "scenarios"
RESULTS_DIR = SCENARIO_DIR / "results"
SYNTHETIC_DIR = SCENARIO_DIR / "synthetic_spike"
PRESETS = ["Naive", "A", "B", "C"]


@dataclass(frozen=True)
class Scenario:
    name: str
    session_date: date            # plug-in evening
    data: str                     # "real" or "synthetic_spike"


SCENARIOS: Dict[str, Scenario] = {
    "flat": Scenario("flat", date(2026, 1, 11), "real"),
    "sustained_peak": Scenario("sustained_peak", date(2026, 1, 10), "real"),
    "synthetic_spike": Scenario("synthetic_spike", date(2026, 1, 11), "synthetic_spike"),
}


class GrowingSocSource:
    """SocSource the runner appends to as the session unfolds."""

    def __init__(self):
        self.samples: List[SocSample] = []

    def add(self, s: SocSample) -> None:
        self.samples.append(s)

    def samples_until(self, t: datetime) -> List[SocSample]:
        return [s for s in self.samples if s.time <= t]


@dataclass
class Data:
    prices: ArchivePriceSource
    dispatch: pd.Series            # slot end -> actual RRP $/MWh


def load_data(kind: str, input_cfg: Optional[InputConfig] = None) -> Data:
    """"real": every PD + DISPATCHPRICE file in the data dir; "synthetic_spike": built CSVs."""
    if kind == "real":
        cfg = input_cfg or load_config()
        pd_frame = load_tables(find_files(cfg, "pd"), PD_SPEC, cfg.region)
        disp = load_tables(find_files(cfg, "dispatch"), DISPATCH_SPEC, cfg.region)
    elif kind == "synthetic_spike":
        parse = ["published", "target"]
        pd_frame = pd.read_csv(SYNTHETIC_DIR / "pd.csv", parse_dates=parse, dtype={"run_id": str})
        disp = pd.read_csv(SYNTHETIC_DIR / "dispatch.csv", parse_dates=parse)
    else:
        raise ValueError(kind)
    return Data(ArchivePriceSource(pd_frame, dispatch_frame=disp), disp.set_index("target")["rrp"])


def _at(d: date, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(hh, mm))


def run_session(session_date: date, preset: str, data: Data, cfg: Optional[DecisionConfig] = None,
                input_cfg: Optional[InputConfig] = None, lookup: Optional[ErrorLookup] = None,
                initial_soc: Optional[float] = None, switch_downtime_s=None,
                planner: Callable = plan) -> pd.DataFrame:
    """Replay one plug-in session.

    switch_downtime_s: None (default) keeps switch downtime informational only.
        A number D, or a dict {"SWITCH_TO_DC_BPT": D1, "SWITCH_TO_AC": D2}, makes each
        switch block all energy flow for D seconds from the start of the slot it is
        commanded in (carried into later slots if D exceeds one slot). The planner is
        not told: this measures what switch time costs the switch-blind planner.
    planner: plan() by default; any callable with the same signature returning a
        PlanResult (e.g. a fixed time-of-use strategy).
    """
    cfg = cfg or load_decision_config()
    input_cfg = input_cfg or load_config()
    lookup = lookup or default_lookup()
    start = _at(session_date, cfg.session_start)
    dep = _at(session_date, cfg.departure)
    if dep <= start:
        dep += timedelta(days=1)
    soc = cfg.initial_soc_pct if initial_soc is None else initial_soc

    # battery/session values come from decision.yaml so both layers agree
    user = input_cfg.user.with_overrides(
        departure_time=dep.isoformat(), target_soc=cfg.target_soc_pct, min_soc=cfg.min_soc_pct,
        battery_capacity_kwh=cfg.capacity_kwh, user_soc=soc)
    soc_src = GrowingSocSource()
    layer = InputLayer(input_cfg, data.prices, soc_src, user, session_start=start)

    dt = cfg.slot_h
    cap = cfg.capacity_kwh
    state = ModeState(AC, None)
    meter = 0.0
    soc_src.add(SocSample(start, AC, meter, None))
    cum_cash = 0.0
    baseline_cost = 0.0
    baseline_soc = soc
    rows = []
    slot_s = cfg.slot_minutes * 60.0
    downtime_left = 0.0
    t = start
    while t < dep:
        slot_end = t + timedelta(minutes=cfg.slot_minutes)
        snap = layer.get_snapshot(t)
        res = planner(snap, state, preset, cfg, lookup)

        mode_before = state.mode
        if res.mode_command is ModeCommand.SWITCH_TO_DC_BPT:
            state = ModeState(DC_BPT, t)
        elif res.mode_command is ModeCommand.SWITCH_TO_AC:
            state = ModeState(AC, t)
        downtime = {ModeCommand.SWITCH_TO_DC_BPT: cfg.switch_ac_to_dc_s,
                    ModeCommand.SWITCH_TO_AC: cfg.switch_dc_to_ac_s}.get(res.mode_command, 0.0)
        if switch_downtime_s is not None and res.mode_command is not ModeCommand.STAY:
            downtime = (float(switch_downtime_s[res.mode_command.value]) if isinstance(switch_downtime_s, dict)
                        else float(switch_downtime_s))
            downtime_left += downtime
        # fraction of this slot during which energy can flow
        avail = 1.0 - min(downtime_left, slot_s) / slot_s
        downtime_left = max(0.0, downtime_left - slot_s)

        price = float(data.dispatch[slot_end])
        soc_before = soc
        kwh, grid, cash = 0.0, 0.0, 0.0
        if res.current_action is Action.EXPORT:
            kwh = max(0.0, min(cfg.export_power_kw * dt * avail, (soc - cfg.min_soc_pct) / 100 * cap))
            soc -= kwh / cap * 100
            grid = -kwh
            cash = price / 1000 * kwh - cfg.degradation_cost_per_kwh * kwh
        elif res.current_action is Action.CHARGE:
            p = cfg.dc_charge_power_kw if state.mode == DC_BPT else cfg.ac_charge_power_kw
            kwh = max(0.0, min(p * dt * avail, (100 - soc) / 100 * cap))
            soc += kwh / cap * 100
            grid = kwh / cfg.eta
            cash = -price / 1000 * kwh / cfg.eta
        meter += grid
        cum_cash += cash
        soc_src.add(SocSample(slot_end, state.mode, meter, soc if state.mode == DC_BPT else None))

        # baseline: plain AC charging from plug-in until target
        if baseline_soc < cfg.target_soc_pct:
            b = min(cfg.ac_charge_power_kw * dt, (cfg.target_soc_pct - baseline_soc) / 100 * cap)
            baseline_soc += b / cap * 100
            baseline_cost += price / 1000 * b / cfg.eta

        cur = res.full_plan[0] if res.full_plan else None
        rows.append({
            "t": t, "slot_end": slot_end, "preset": preset,
            "action": res.current_action.value, "mode_before": mode_before,
            "mode_command": res.mode_command.value, "mode_after": state.mode,
            "switch_downtime_s": downtime, "slot_availability": avail,
            "forecast_rrp": next((p.rrp for p in snap.forecast if p.interval_end == slot_end), None),
            "sell_price": cur.sell_price if cur else None, "buy_price": cur.buy_price if cur else None,
            "actual_rrp": price, "energy_kwh": kwh, "grid_kwh": grid,
            "soc_true_before": soc_before, "soc_true_after": soc,
            "soc_lower": snap.soc.lower, "soc_upper": snap.soc.upper, "soc_method": snap.soc.method,
            "cashflow": cash, "cum_cashflow": cum_cash,
            "baseline_cost_cum": baseline_cost, "actual_profit_cum": baseline_cost + cum_cash,
            "plan_exports": sum(s.action is Action.EXPORT for s in res.full_plan),
            "plan_net_profit_est": res.net_profit,
            "plan_switches": res.mode_switches_planned,
            "overrides": ";".join(f"{o.rule}@{o.interval_end:%H:%M}" for o in res.safety_overrides_applied),
            "flags": ";".join(snap.flags.active()),
        })
        t = slot_end
    return pd.DataFrame(rows)


def summarise(df: pd.DataFrame, scenario: str) -> dict:
    """Estimated profit = the planner's first-cycle estimate (full plan at plug-in)."""
    return {
        "scenario": scenario, "preset": df["preset"].iloc[0],
        "exports": int((df["action"] == "EXPORT").sum()),
        "export_kwh": float(df.loc[df["action"] == "EXPORT", "energy_kwh"].sum()),
        "mode_switches": int((df["mode_command"] != "STAY").sum()),
        "est_profit_at_plugin": float(df["plan_net_profit_est"].iloc[0]),
        "net_cashflow": float(df["cum_cashflow"].iloc[-1]),
        "baseline_cost": float(df["baseline_cost_cum"].iloc[-1]),
        "actual_profit": float(df["actual_profit_cum"].iloc[-1]),
        "final_soc": float(df["soc_true_after"].iloc[-1]),
        "min_soc": float(df["soc_true_after"].min()),
    }


def run_scenario(name: str, preset: str, data: Optional[Data] = None, save: bool = True,
                 **kw) -> pd.DataFrame:
    sc = SCENARIOS[name]
    df = run_session(sc.session_date, preset, data or load_data(sc.data), **kw)
    if save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(RESULTS_DIR / f"{name}_{preset}.csv", index=False, float_format="%.4f")
    return df


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=list(SCENARIOS), action="append")
    ap.add_argument("--preset", choices=PRESETS, action="append")
    a = ap.parse_args(argv)
    cache: Dict[str, Data] = {}
    rows = []
    for name in a.scenario or list(SCENARIOS):
        kind = SCENARIOS[name].data
        if kind not in cache:
            cache[kind] = load_data(kind)
        for preset in a.preset or PRESETS:
            rows.append(summarise(run_scenario(name, preset, cache[kind]), name))
    summary = pd.DataFrame(rows)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(RESULTS_DIR / "summary.csv", index=False, float_format="%.4f")
    with pd.option_context("display.width", 200, "display.float_format", "{:.2f}".format):
        print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
