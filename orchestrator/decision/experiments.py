"""Offline experiments E3-E5 over the five committed pricing days (no SiL).

    python -m orchestrator.decision.experiments e3   # switch-duration sensitivity
    python -m orchestrator.decision.experiments e4   # cost-blind ablation
    python -m orchestrator.decision.experiments e5   # fixed time-of-use schedule

Every run is an offline replay (decision/scenario_runner.run_session) of the plug-in
session D 18:00 -> D+1 07:00 with real January 2026 NSW1 PD runs, the error table,
and the decision-time energy rules (export 10 kW DC_BPT only, charge 7.4 kW AC /
50 kW DC, eta 0.9, $0.02/kWh degradation), scored at actual dispatch prices.

"profit_vs_baseline" = net cashflow + the cost of plain AC charging from plug-in to
the 80 % target (the runner's baseline, exact to the partial last slot).

Results go to orchestrator/experiments/<experiment>/ (CSV, plots; README written
separately).
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from orchestrator.inputs.error_lookup import ErrorLookup
from orchestrator.inputs.snapshot import Snapshot

from .config import DecisionConfig, load_decision_config
from .planner import (Action, ModeCommand, ModeState, Override, PlanResult, PlanStep, _mode_step,
                      adjust_prices, apply_safety)
from .scenario_runner import Data, load_data, run_session

ORCH_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = ORCH_DIR / "experiments"

DAYS: Dict[str, date] = {
    "Jan 03": date(2026, 1, 3),    # mildly negative prices
    "Jan 07": date(2026, 1, 7),    # moderate volatility
    "Jan 10": date(2026, 1, 10),   # extreme spike
    "Jan 11": date(2026, 1, 11),   # calm
    "Jan 25": date(2026, 1, 25),   # moderate volatility
}
DAY_PROFILE = {"Jan 03": "mild negative", "Jan 07": "moderate", "Jan 10": "extreme spike",
               "Jan 11": "calm", "Jan 25": "moderate"}
PRESETS = ["A", "B", "C"]
E3_DURATIONS = [0.3, 1, 5, 10, 20, 30, 60, 120, 180]


def summary(df: pd.DataFrame) -> dict:
    """Run metrics from a run_session DataFrame."""
    exp = df["action"] == "EXPORT"
    return {
        "exports": int((exp & (df["energy_kwh"] > 0)).sum()),
        "export_kwh": float(df.loc[exp, "energy_kwh"].sum()),
        "charges": int(((df["action"] == "CHARGE") & (df["energy_kwh"] > 0)).sum()),
        "switches": int((df["mode_command"] != "STAY").sum()),
        "downtime_s_total": float(df.loc[df["mode_command"] != "STAY", "switch_downtime_s"].sum()),
        "revenue": float(df.loc[df["cashflow"] > 0, "cashflow"].sum()),
        "charge_cost": float(-df.loc[df["cashflow"] < 0, "cashflow"].sum()),
        "net_cashflow": float(df["cum_cashflow"].iloc[-1]),
        "baseline_cost": float(df["baseline_cost_cum"].iloc[-1]),
        "profit_vs_baseline": float(df["actual_profit_cum"].iloc[-1]),
        "final_soc": float(df["soc_true_after"].iloc[-1]),
        "min_soc": float(df["soc_true_after"].min()),
    }


# --------------------------------------------------------------------------- #
# E5: fixed time-of-use strategy (same signature as planner.plan)
# --------------------------------------------------------------------------- #

def _in_window(hour: float, window) -> bool:
    lo, hi = window
    return lo <= hour < hi if lo < hi else (hour >= lo or hour < hi)


def make_tou_planner(offpeak=(22, 6), peak=(14, 20), export_threshold=300.0):
    """Fixed schedule: charge in off-peak until target; export in peak if forecast > threshold.

    Uses the raw latest PD forecast for the current slot (no error adjustment, no
    optimisation). Safety rules (SoC floor on the lower bound, departure guarantee,
    safe_mode) and the mode/dwell constraints are the planner's own, unchanged.
    """
    def tou_plan(snap: Snapshot, state: ModeState, preset: str, cfg: DecisionConfig,
                 lookup: Optional[ErrorLookup] = None) -> PlanResult:
        slots = adjust_prices(snap, "Naive", cfg, lookup)      # raw forecast, unknowns kept
        soc_plan = snap.soc.lower if snap.flags.soc_uncertain else snap.soc.mid
        actions = [Action.IDLE] * len(slots)
        if slots:
            h = snap.t.hour + snap.t.minute / 60.0
            s0 = slots[0]
            if _in_window(h, peak) and s0.known and s0.forecast > export_threshold:
                actions[0] = Action.EXPORT
            elif _in_window(h, offpeak) and soc_plan < cfg.target_soc_pct:
                actions[0] = Action.CHARGE
        soc_after, overrides = apply_safety(snap, slots, actions, soc_plan, cfg)
        cur, cmd = Action.IDLE, None
        if slots:
            cur, cmd, _ = _mode_step(actions[0], actions[1:], state, snap.t, cfg)
            if cur is not actions[0]:
                overrides.append(Override("dwell_blocks_export", slots[0].end, actions[0], cur))
                actions[0] = cur
        return PlanResult(
            t=snap.t, preset="ToU", current_action=cur, mode_command=cmd or ModeCommand.STAY,
            full_plan=[PlanStep(s.end, a, s.sell, s.buy, x) for s, a, x in zip(slots, actions, soc_after)],
            safety_overrides_applied=overrides, total_export_revenue=0.0, total_charge_cost=0.0,
            net_profit=0.0, mode_switches_planned=int(cmd is not None and cmd is not ModeCommand.STAY),
            switch_downtime_s=0.0, soc_plan_start=soc_plan)
    return tou_plan


# --------------------------------------------------------------------------- #
# Experiments
# --------------------------------------------------------------------------- #

def run_e3(data: Data) -> pd.DataFrame:
    """Switch-duration sensitivity: same D for both directions, plus the 10 s/15 s reference
    and the informational (no downtime) E2 setting."""
    settings = [("informational", None)] + [(f"{d:g}", d) for d in E3_DURATIONS] + \
               [("ref 10/15", {"SWITCH_TO_DC_BPT": 10.0, "SWITCH_TO_AC": 15.0})]
    rows = []
    for day, d in DAYS.items():
        for preset in PRESETS:
            for label, dt in settings:
                df = run_session(d, preset, data, switch_downtime_s=dt)
                rows.append({"day": day, "profile": DAY_PROFILE[day], "preset": preset, "setting": label,
                             "switch_duration_s": dt if isinstance(dt, (int, float)) else None, **summary(df)})
    return pd.DataFrame(rows)


def run_e4(data: Data) -> pd.DataFrame:
    """Cost-blind ablation: every preset with the error-distribution adjustment removed
    (raw forecast for both sell and buy) vs the normal E2 replay."""
    blind = load_decision_config(overrides={"presets": {p: None for p in PRESETS}})
    normal = load_decision_config()
    rows = []
    for day, d in DAYS.items():
        for preset in PRESETS:
            for variant, cfg in (("risk-adjusted (E2)", normal), ("cost-blind", blind)):
                df = run_session(d, preset, data, cfg=cfg)
                rows.append({"day": day, "profile": DAY_PROFILE[day], "preset": preset, "variant": variant,
                             **summary(df)})
    return pd.DataFrame(rows)


def run_e5(data: Data) -> pd.DataFrame:
    """Fixed time-of-use schedule vs the E2 presets and the plain-charging baseline."""
    tou = make_tou_planner()
    rows = []
    for day, d in DAYS.items():
        df = run_session(d, "Naive", data, planner=tou)
        s = summary(df)
        rows.append({"day": day, "profile": DAY_PROFILE[day], "strategy": "ToU", **s})
        rows.append({"day": day, "profile": DAY_PROFILE[day], "strategy": "Baseline", "exports": 0,
                     "export_kwh": 0.0, "charges": None, "switches": 0, "downtime_s_total": 0.0,
                     "revenue": 0.0, "charge_cost": s["baseline_cost"], "net_cashflow": -s["baseline_cost"],
                     "baseline_cost": s["baseline_cost"], "profit_vs_baseline": 0.0,
                     "final_soc": 80.0, "min_soc": None})
        for preset in PRESETS:
            rows.append({"day": day, "profile": DAY_PROFILE[day], "strategy": preset,
                         **summary(run_session(d, preset, data))})
    return pd.DataFrame(rows)


def run_e4_sell_only(data: Data) -> pd.DataFrame:
    """E4 variant: risk adjustment on the SELL side only (buy = raw forecast), next to the
    original E4 variants (both sides adjusted = E2; neither side = cost-blind)."""
    variants = {
        "risk-adjusted (E2)": load_decision_config(),
        "sell-only": load_decision_config(overrides={"presets": {p: {"buy": None} for p in PRESETS}}),
        "cost-blind": load_decision_config(overrides={"presets": {p: None for p in PRESETS}}),
    }
    rows = []
    for day, d in DAYS.items():
        for preset in PRESETS:
            for variant, cfg in variants.items():
                q = cfg.presets[preset]
                rows.append({"day": day, "profile": DAY_PROFILE[day], "preset": preset, "variant": variant,
                             "sell_q": q[0] if q else None, "buy_q": q[1] if q else None,
                             **summary(run_session(d, preset, data, cfg=cfg))})
    return pd.DataFrame(rows)


def run_e5_tou150(data: Data) -> pd.DataFrame:
    """E5 variant: ToU export threshold $150/MWh, next to the original $300 ToU, the
    presets and the baseline."""
    rows = []
    for day, d in DAYS.items():
        for label, thr in (("ToU $300", 300.0), ("ToU $150", 150.0)):
            rows.append({"day": day, "profile": DAY_PROFILE[day], "strategy": label, "export_threshold": thr,
                         **summary(run_session(d, "Naive", data, planner=make_tou_planner(export_threshold=thr)))})
        base = rows[-1]["baseline_cost"]
        rows.append({"day": day, "profile": DAY_PROFILE[day], "strategy": "Baseline", "export_threshold": None,
                     "exports": 0, "export_kwh": 0.0, "charges": None, "switches": 0, "downtime_s_total": 0.0,
                     "revenue": 0.0, "charge_cost": base, "net_cashflow": -base, "baseline_cost": base,
                     "profit_vs_baseline": 0.0, "final_soc": 80.0, "min_soc": None})
        for preset in PRESETS:
            rows.append({"day": day, "profile": DAY_PROFILE[day], "strategy": preset, "export_threshold": None,
                         **summary(run_session(d, preset, data))})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Plots
# --------------------------------------------------------------------------- #

COLORS = {"A": "#1f77b4", "B": "#2ca02c", "C": "#d62728", "ToU": "#9467bd", "Baseline": "#777777"}


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def plot_e3(df: pd.DataFrame, out: Path) -> None:
    plt = _plt()
    sweep = df[df.switch_duration_s.notna()]
    fig, axes = plt.subplots(1, len(DAYS), figsize=(17, 3.8))
    for ax, day in zip(axes, DAYS):
        for p in PRESETS:
            x = sweep[(sweep.day == day) & (sweep.preset == p)].sort_values("switch_duration_s")
            ax.plot(x.switch_duration_s, x.profit_vs_baseline, marker="o", ms=3, color=COLORS[p], label=p)
            ref = df[(df.day == day) & (df.preset == p) & (df.setting == "informational")]
            ax.axhline(float(ref.profit_vs_baseline.iloc[0]), color=COLORS[p], ls=":", lw=0.8)
        ax.set_xscale("log"); ax.set_title(f"{day} ({DAY_PROFILE[day]})", fontsize=9)
        ax.set_xlabel("switch duration (s)"); ax.grid(alpha=.3)
    axes[0].set_ylabel("profit vs baseline ($)"); axes[0].legend(fontsize=8)
    fig.suptitle("E3: switch-duration sensitivity (dotted = no downtime, E2)", fontsize=10)
    fig.tight_layout(); fig.savefig(out / "e3_profit_vs_duration.png", dpi=130); plt.close(fig)

    # loss relative to no downtime, all days
    fig, ax = plt.subplots(figsize=(7, 4))
    for p in PRESETS:
        base = df[(df.preset == p) & (df.setting == "informational")].set_index("day").profit_vs_baseline
        x = sweep[sweep.preset == p].copy()
        x["loss"] = base.reindex(x.day).values - x.profit_vs_baseline.values
        tot = x.groupby("switch_duration_s").loss.sum()
        ax.plot(tot.index, tot.values, marker="o", color=COLORS[p], label=p)
    ax.set_xscale("log"); ax.set_xlabel("switch duration (s)"); ax.set_ylabel("profit lost vs no downtime ($, 5 days)")
    ax.set_title("E3: total profit lost to switch downtime", fontsize=10); ax.grid(alpha=.3); ax.legend()
    fig.tight_layout(); fig.savefig(out / "e3_loss_vs_duration.png", dpi=130); plt.close(fig)


def _grouped_bars(df: pd.DataFrame, key: str, groups: List[str], out_file: Path, title: str, colors) -> None:
    plt = _plt()
    import numpy as np
    fig, axes = plt.subplots(1, len(DAYS), figsize=(17, 3.8))
    for ax, day in zip(axes, DAYS):
        x = df[df.day == day].set_index(key).reindex(groups)
        ax.bar(np.arange(len(groups)), x.profit_vs_baseline.values, color=[colors(g) for g in groups])
        ax.set_xticks(np.arange(len(groups)), groups, rotation=30, fontsize=8)
        ax.axhline(0, color="#444", lw=0.8); ax.grid(axis="y", alpha=.3)
        ax.set_title(f"{day} ({DAY_PROFILE[day]})", fontsize=9)
        for i, v in enumerate(x.profit_vs_baseline.values):
            ax.annotate(f"{v:.2f}", (i, v), ha="center", va="bottom" if v >= 0 else "top", fontsize=7)
    axes[0].set_ylabel("profit vs baseline ($)")
    fig.suptitle(title, fontsize=10); fig.tight_layout(); fig.savefig(out_file, dpi=130); plt.close(fig)


def plot_e4(df: pd.DataFrame, out: Path) -> None:
    d = df.copy()
    d["group"] = d.preset + d.variant.map({"risk-adjusted (E2)": "", "cost-blind": " blind"})
    groups = [g for p in PRESETS for g in (p, f"{p} blind")]
    _grouped_bars(d, "group", groups, out / "e4_profit_by_day.png",
                  "E4: risk-adjusted (E2) vs cost-blind (raw forecast) presets",
                  lambda g: "#bbbbbb" if g.endswith("blind") else COLORS[g])


def plot_e5(df: pd.DataFrame, out: Path) -> None:
    groups = ["Baseline", "ToU", "A", "B", "C"]
    _grouped_bars(df, "strategy", groups, out / "e5_profit_by_day.png",
                  "E5: fixed time-of-use schedule vs planner presets", lambda g: COLORS[g])


def plot_e4_sell_only(df: pd.DataFrame, out: Path) -> None:
    short = {"risk-adjusted (E2)": "", "sell-only": " sell", "cost-blind": " blind"}
    d = df.copy()
    d["group"] = d.preset + d.variant.map(short)
    d = d[(d.variant != "cost-blind") | (d.preset == "A")].copy()   # cost-blind is identical for A/B/C
    d.loc[d.variant == "cost-blind", "group"] = "blind"
    groups = [g for p in PRESETS for g in (p, f"{p} sell")] + ["blind"]
    def color(g):
        return "#bbbbbb" if g == "blind" else (COLORS[g] if len(g) == 1 else
                                                {"A": "#9ecae1", "B": "#a1d99b", "C": "#fc9272"}[g[0]])
    _grouped_bars(d, "group", groups, out / "e4_sell_only_profit_by_day.png",
                  "E4 variant: both-side (E2) vs sell-only risk adjustment vs cost-blind", color)


def plot_e5_tou150(df: pd.DataFrame, out: Path) -> None:
    groups = ["Baseline", "ToU $300", "ToU $150", "A", "B", "C"]
    cmap = {**COLORS, "ToU $300": "#c5b0d5", "ToU $150": "#9467bd"}
    _grouped_bars(df, "strategy", groups, out / "e5_tou150_profit_by_day.png",
                  r"E5 variant: ToU export threshold \$150 vs \$300 vs planner presets", lambda g: cmap[g])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("experiment", choices=["e3", "e4", "e5", "e4-sell-only", "e5-tou150"])
    a = ap.parse_args(argv)
    data = load_data("real")
    live = ORCH_DIR / "live_logs"      # the two follow-up variants were requested under live_logs/
    out_dir, name, fn, plot = {
        "e3": (OUT_DIR, "e3_switch_duration", run_e3, plot_e3),
        "e4": (OUT_DIR, "e4_cost_blind", run_e4, plot_e4),
        "e5": (OUT_DIR, "e5_fixed_schedule", run_e5, plot_e5),
        "e4-sell-only": (live, "e4_sell_only", run_e4_sell_only, plot_e4_sell_only),
        "e5-tou150": (live, "e5_tou_150", run_e5_tou150, plot_e5_tou150),
    }[a.experiment]
    out = out_dir / name
    out.mkdir(parents=True, exist_ok=True)
    df = fn(data)
    df.to_csv(out / "results.csv", index=False, float_format="%.4f")
    plot(df, out)
    with pd.option_context("display.width", 250, "display.max_rows", 500, "display.float_format", "{:.2f}".format):
        print(df.to_string(index=False))


if __name__ == "__main__":
    main()
