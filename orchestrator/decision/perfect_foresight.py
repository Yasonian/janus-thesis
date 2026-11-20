"""E8: offline perfect-foresight benchmark over the five committed pricing days.

    python -m orchestrator.decision.perfect_foresight            # writes experiments/e8_perfect_foresight/

The best schedule a planner could have executed if the ACTUAL 5-min dispatch prices
had been known at plug-in, under the same energy rules and scoring as the offline
replay (decision/scenario_runner.run_session) used by E2-E5:

  session D 18:00 -> D+1 07:00, 156 five-minute slots, start SoC 60 %
  60 kWh battery, 20 % floor at every slot end, >= 80 % at departure, <= 100 %
  EXPORT  10 kW, lossless, degradation $0.02/kWh on exported energy (DC_BPT only)
  CHARGE  7.4 kW AC, grid energy = battery energy / eta (eta 0.9)
  cash    = sum price/1000 * e_t - 0.02 * e_t - price/1000 * c_t / eta
  profit  = cash + baseline cost (plain AC charging from plug-in to exactly 80 %)

Each slot is EXPORT, CHARGE or IDLE (binaries u_t, v_t, u_t + v_t <= 1). The energy
in a chosen slot is continuous in [0, full slot] -- a slight relaxation of the replay,
which always moves a full slot's energy unless the floor / 100 % clips it. Switch
downtime is not modelled (E2 setting: informational only).

Variants
  a   upper bound: no mode constraints at all
  b   mode binaries m_t (1 = DC_BPT, plug-in mode AC): export needs DC_BPT, AC
      charging needs AC, and a mode change is followed by >= 15 min (3 slots) in
      the new mode, i.e. the planner's dwell rule. A 1e-4 $ penalty per switch
      picks the fewest-switch schedule among the profit-optimal ones.
  --dc-charge  adds the replay's 50 kW DC charging while in DC_BPT (a: any slot).
               Off by default: the planner only ever plans 7.4 kW AC charging.

Solver: scipy.optimize.milp (HiGHS).
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from orchestrator.inputs.aemo_csv import DISPATCH_SPEC, load_tables
from orchestrator.inputs.config import load_config
from orchestrator.inputs.replay import find_files

from .config import DecisionConfig, load_decision_config
from .experiments import DAYS, DAY_PROFILE

ORCH_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = ORCH_DIR / "experiments" / "e8_perfect_foresight"
E4 = ORCH_DIR / "experiments" / "e4_cost_blind" / "results.csv"
E5 = ORCH_DIR / "experiments" / "e5_fixed_schedule" / "results.csv"
SWITCH_EPS = 1e-4          # $ per switch, tie-break only
TOL = 1e-6


def _at(d: date, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(hh, mm))


def session_prices(dispatch: pd.Series, d: date, cfg: DecisionConfig) -> pd.Series:
    """Actual RRP for each slot end of the session, in order (len 156)."""
    start, dep = _at(d, cfg.session_start), _at(d, cfg.departure)
    if dep <= start:
        dep += timedelta(days=1)
    ends = pd.date_range(start + timedelta(minutes=cfg.slot_minutes), dep, freq=f"{cfg.slot_minutes}min")
    s = dispatch.reindex(ends)
    if s.isna().any():
        raise ValueError(f"{d}: {int(s.isna().sum())} slots without a dispatch price")
    return s.astype(float)


def baseline_cost(prices: np.ndarray, cfg: DecisionConfig, soc0: float) -> float:
    """Exactly scenario_runner's baseline: plain AC charging from plug-in to the target."""
    cap, dt = cfg.capacity_kwh, cfg.slot_h
    soc, cost = soc0, 0.0
    for p in prices:
        if soc >= cfg.target_soc_pct:
            break
        b = min(cfg.ac_charge_power_kw * dt, (cfg.target_soc_pct - soc) / 100 * cap)
        soc += b / cap * 100
        cost += p / 1000 * b / cfg.eta
    return cost


def solve(prices: np.ndarray, cfg: DecisionConfig, soc0: float, modes: bool, dc_charge: bool) -> Dict:
    T = len(prices)
    cap, dt = cfg.capacity_kwh, cfg.slot_h
    e_x = cfg.export_power_kw * dt                 # kWh per full export slot
    e_ac = cfg.ac_charge_power_kw * dt             # kWh per full AC charge slot
    e_dc = cfg.dc_charge_power_kw * dt
    soc_min_kwh = cfg.min_soc_pct / 100 * cap
    soc_tgt_kwh = cfg.target_soc_pct / 100 * cap
    e0 = soc0 / 100 * cap

    # variable layout: e[T] c[T] u[T] v[T] (+ m[T] w[T] s[T] if modes) (+ w[T] if dc & !modes)
    idx: Dict[str, slice] = {}
    n = 0
    def block(name):
        nonlocal n
        idx[name] = slice(n, n + T)
        n += T
    for name in ["e", "c", "u", "v"]:
        block(name)
    if modes:
        block("m"); block("s")
    if dc_charge:
        block("w")                                  # w_t = 1: this charge slot runs at DC power

    cost = np.zeros(n)
    cost[idx["e"]] = -(prices / 1000 - cfg.degradation_cost_per_kwh)     # minimise -profit
    cost[idx["c"]] = prices / 1000 / cfg.eta
    if modes:
        cost[idx["s"]] = SWITCH_EPS

    lb, ub = np.zeros(n), np.ones(n)
    ub[idx["e"]] = e_x
    ub[idx["c"]] = e_dc if dc_charge else e_ac
    integrality = np.zeros(n)
    for name in ["u", "v"] + (["m"] if modes else []):
        integrality[idx[name]] = 1

    rows: List[Dict[int, float]] = []
    lo: List[float] = []
    hi: List[float] = []
    def add(coefs: Dict[int, float], low: float, high: float):
        rows.append(coefs); lo.append(low); hi.append(high)
    E, C, U, V = (idx[k].start for k in ["e", "c", "u", "v"])
    M = idx["m"].start if modes else None
    S = idx["s"].start if modes else None
    W = idx["w"].start if dc_charge else None

    for t in range(T):
        add({E + t: 1, U + t: -e_x}, -np.inf, 0)                       # e_t <= e_x u_t
        if dc_charge:
            add({C + t: 1, V + t: -e_ac, W + t: -(e_dc - e_ac)}, -np.inf, 0)   # c_t <= e_ac v_t + (e_dc-e_ac) w_t
            add({W + t: 1, V + t: -1}, -np.inf, 0)                     # w_t <= v_t
            if modes:
                add({W + t: 1, M + t: -1}, -np.inf, 0)                 # DC charging only in DC_BPT
        else:
            add({C + t: 1, V + t: -e_ac}, -np.inf, 0)
        add({U + t: 1, V + t: 1}, -np.inf, 1)                          # one action per slot
        # SoC at slot end: e0 + sum_{k<=t} (c_k - e_k) in [floor, 100 %]
        coefs = {}
        for k in range(t + 1):
            coefs[C + k] = 1
            coefs[E + k] = -1
        add(coefs, soc_min_kwh - e0, cap - e0)
        if modes:
            add({U + t: 1, M + t: -1}, -np.inf, 0)                     # export needs DC_BPT
            # AC charging needs AC mode: v_t <= 1 - m_t (+ w_t: a DC-powered charge may stay in DC_BPT)
            add({V + t: 1, M + t: 1, **({W + t: -1} if dc_charge else {})}, -np.inf, 1)
            prev = {M + t - 1: 1} if t > 0 else {}                     # +m_{t-1}; m_{-1} = 0 (AC at plug-in)
            neg_prev = {j: -v for j, v in prev.items()}                # -m_{t-1}
            add({S + t: 1, M + t: -1, **prev}, 0, np.inf)              # s_t - m_t + m_{t-1} >= 0
            add({S + t: 1, M + t: 1, **neg_prev}, 0, np.inf)           # s_t + m_t - m_{t-1} >= 0
            hold = int(cfg.dwell_time_minutes // cfg.slot_minutes)
            for k in range(t + 1, min(t + hold, T)):
                # a switch at t keeps the new mode through slots t .. t+hold-1
                add({M + t: 1, **neg_prev, M + k: -1}, -np.inf, 0)     # m_t - m_{t-1} - m_k <= 0
                add({M + t: -1, **prev, M + k: 1}, -np.inf, 1)         # m_{t-1} - m_t + m_k <= 1
    # departure: SoC >= target
    add({**{C + k: 1 for k in range(T)}, **{E + k: -1 for k in range(T)}}, soc_tgt_kwh - e0, np.inf)

    A = lil_matrix((len(rows), n))
    for r, coefs in enumerate(rows):
        for j, v in coefs.items():
            A[r, j] += v
    res = milp(cost, constraints=LinearConstraint(A.tocsr(), lo, hi), integrality=integrality,
               bounds=Bounds(lb, ub), options={"mip_rel_gap": 0.0})
    if res.status != 0:
        raise RuntimeError(f"solver status {res.status}: {res.message}")
    x = res.x
    e, c = x[idx["e"]], x[idx["c"]]
    soc_kwh = e0 + np.cumsum(c - e)
    cash = float(np.sum(prices / 1000 * e - cfg.degradation_cost_per_kwh * e - prices / 1000 * c / cfg.eta))
    if modes:
        m = np.round(x[idx["m"]]).astype(int)
    else:
        # implied modes: export slots are DC_BPT, charge slots AC, idle keeps the previous mode
        m, cur = np.zeros(T, int), 0
        for t in range(T):
            cur = 1 if e[t] > TOL else 0 if c[t] > TOL else cur
            m[t] = cur
    switches = int(np.sum(np.abs(np.diff(np.concatenate([[0], m])))))
    action = np.where(e > TOL, "EXPORT", np.where(c > TOL, "CHARGE", "IDLE"))
    sched = pd.DataFrame({"actual_rrp": prices, "action": action, "mode": np.where(m == 1, "DC_BPT", "AC"),
                          "export_kwh": e, "charge_kwh": c, "soc_after": soc_kwh / cap * 100})
    return {
        "net_cashflow": cash, "exports": int((e > TOL).sum()), "export_kwh": float(e.sum()),
        "charges": int((c > TOL).sum()), "charge_kwh": float(c.sum()), "switches": switches,
        "min_soc": float(soc_kwh.min() / cap * 100), "final_soc": float(soc_kwh[-1] / cap * 100),
        "schedule": sched,
    }


def existing_strategies() -> pd.DataFrame:
    """Per-day profit_vs_baseline of A, B, C (E2 risk-adjusted), Unadjusted (E4 cost-blind)
    and ToU (E5, $300 threshold) from the committed offline results."""
    e4, e5 = pd.read_csv(E4), pd.read_csv(E5)
    rows = []
    for _, r in e4[e4["variant"] == "risk-adjusted (E2)"].iterrows():
        rows.append({"day": r["day"], "strategy": r["preset"], **r[["profit_vs_baseline", "switches", "min_soc", "baseline_cost"]]})
    blind = e4[e4["variant"] == "cost-blind"]
    for day, g in blind.groupby("day", sort=False):
        assert g["profit_vs_baseline"].nunique() == 1, f"{day}: cost-blind presets differ"
        r = g.iloc[0]
        rows.append({"day": day, "strategy": "Unadjusted", **r[["profit_vs_baseline", "switches", "min_soc", "baseline_cost"]]})
    for _, r in e5[e5["strategy"] == "ToU"].iterrows():
        rows.append({"day": r["day"], "strategy": "ToU", **r[["profit_vs_baseline", "switches", "min_soc", "baseline_cost"]]})
    return pd.DataFrame(rows)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dc-charge", action="store_true", help="also run variants with 50 kW DC charging in DC_BPT")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    a = ap.parse_args(argv)

    cfg = load_decision_config()
    icfg = load_config()
    dispatch = load_tables(find_files(icfg, "dispatch"), DISPATCH_SPEC, icfg.region).set_index("target")["rrp"]
    prior = existing_strategies()
    a.out_dir.mkdir(parents=True, exist_ok=True)

    variants = [("Optimal (a)", False, False), ("Optimal (b)", True, False)]
    if a.dc_charge:
        variants += [("Optimal (a)+DC50", False, True), ("Optimal (b)+DC50", True, True)]
    rows = []
    for day, d in DAYS.items():
        prices = session_prices(dispatch, d, cfg)
        base = baseline_cost(prices.to_numpy(), cfg, cfg.initial_soc_pct)
        base_prev = prior.loc[prior["day"] == day, "baseline_cost"].unique()
        assert len(base_prev) == 1 and abs(base - base_prev[0]) < 5e-5, f"{day}: baseline {base:.4f} vs E4/E5 {base_prev}"
        for label, modes, dc in variants:
            r = solve(prices.to_numpy(), cfg, cfg.initial_soc_pct, modes, dc)
            r["schedule"].to_csv(a.out_dir / f"schedule_{d:%b%d}_{label.replace(' ', '_').replace('(', '').replace(')', '')}.csv",
                                 index_label="slot_end", float_format="%.4f")
            rows.append({"day": day, "profile": DAY_PROFILE[day], "strategy": label, "baseline_cost": base,
                         "net_cashflow": r["net_cashflow"], "profit_vs_baseline": base + r["net_cashflow"],
                         "exports": r["exports"], "export_kwh": r["export_kwh"], "charges": r["charges"],
                         "charge_kwh": r["charge_kwh"], "switches": r["switches"],
                         "min_soc": r["min_soc"], "final_soc": r["final_soc"]})
        for _, p in prior[prior["day"] == day].iterrows():
            rows.append({"day": day, "profile": DAY_PROFILE[day], "strategy": p["strategy"], "baseline_cost": base,
                         "profit_vs_baseline": p["profit_vs_baseline"], "switches": p["switches"], "min_soc": p["min_soc"]})
    res = pd.DataFrame(rows)

    # sanity: every strategy <= Optimal (b) <= Optimal (a), per day
    order = ["Optimal (a)", "Optimal (b)"] + (["Optimal (a)+DC50", "Optimal (b)+DC50"] if a.dc_charge else []) + \
            ["A", "B", "C", "Unadjusted", "ToU"]
    piv = res.pivot(index="day", columns="strategy", values="profit_vs_baseline").reindex(list(DAYS))[order]
    for day, r in piv.iterrows():
        assert r["Optimal (a)"] >= r["Optimal (b)"] - TOL, f"{day}: (b) exceeds (a)"
        for s in ["A", "B", "C", "Unadjusted", "ToU"]:
            assert r["Optimal (b)"] >= r[s] - TOL, f"{day}: {s} {r[s]:.4f} exceeds Optimal (b) {r['Optimal (b)']:.4f}"
    piv.loc["Total"] = piv.sum()
    pct = piv.div(piv["Optimal (b)"], axis=0) * 100
    pct_a = piv.div(piv["Optimal (a)"], axis=0) * 100

    res.to_csv(a.out_dir / "results.csv", index=False, float_format="%.4f")
    piv.to_csv(a.out_dir / "profit_table.csv", float_format="%.4f")
    pct.to_csv(a.out_dir / "pct_of_optimal_b.csv", float_format="%.2f")
    pct_a.to_csv(a.out_dir / "pct_of_optimal_a.csv", float_format="%.2f")
    with pd.option_context("display.width", 220, "display.float_format", "{:.2f}".format):
        print("profit vs plain-AC baseline ($)\n", piv.to_string())
        print("\n% of Optimal (b)\n", pct.to_string())
        print("\n% of Optimal (a)\n", pct_a.to_string())
        opt = res[res["strategy"].str.startswith("Optimal")][
            ["day", "strategy", "profit_vs_baseline", "exports", "export_kwh", "charges", "charge_kwh", "switches", "min_soc", "final_soc"]]
        print("\noptimal schedules\n", opt.to_string(index=False))
    print(f"\nsanity checks passed; written to {a.out_dir}")


if __name__ == "__main__":
    main()
