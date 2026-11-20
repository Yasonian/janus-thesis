"""E7: out-of-sample test on February 2026 (offline, no SiL).

    python -m orchestrator.decision.out_of_sample        # writes experiments/e7_out_of_sample/

The error table stays the January 2026 one (inputs.yaml error_distributions.train_window);
the script rebuilds it from the data dir with the train window and uses that table,
so the February archives cannot leak into it. Every parameter (margin, quantiles,
battery, session) is unchanged from E2-E5/E8.

Session selection (written down in the README) -- rank every complete February session
D 18:00 -> D+1 07:00 (156 five-minute dispatch prices, NSW1) by the standard deviation
of its actual 5-min dispatch prices, excluding sessions whose plug-in evening D or
departure morning D+1 is a NSW public holiday:
    most volatile = rank 1, in between = ranks 2 and 4, flattest = last rank.
(In January the picks were rank 1, 2, 4 and last by calendar-day SD; ranks 2 and 4 are
also Jan 7 and Jan 25 by session SD.)

Strategies per session, all scored by decision/scenario_runner.run_session at actual
dispatch prices against the same plain-AC baseline:
    A, B, C            both sides risk-adjusted (E2 presets)
    A, B, C sell-only  sell side adjusted, buy = raw forecast (E4 variant)
    Unadjusted         raw forecast both sides (E4 cost-blind; identical for A/B/C)
    ToU $300, ToU $150 fixed time-of-use schedule (E5 and its variant)
    Optimal (b)        perfect-foresight MILP with the 15-min dwell (E8, decision/perfect_foresight)

Forecast-failure check: every PD forecast > $300/MWh for a 30-min interval inside a
session, from a run published between 30 min before plug-in and the interval's end,
is compared with the actual 30-min price (mean of the six 5-min dispatch prices).
"Failed" = actual < 50 % of the forecast.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

from orchestrator.inputs.aemo_csv import DISPATCH_SPEC, PD_SPEC, load_tables
from orchestrator.inputs.config import load_config
from orchestrator.inputs.error_dist import (LEAD_BINS, RATIO_BINS, actual_30min, bin_label, build_pairs,
                                            build_training_table)
from orchestrator.inputs.error_lookup import ORCHESTRATOR_DIR, ErrorLookup
from orchestrator.inputs.replay import find_files
from orchestrator.inputs.sources import ArchivePriceSource

from .config import load_decision_config
from .experiments import make_tou_planner, summary
from .perfect_foresight import baseline_cost, session_prices, solve
from .scenario_runner import Data, run_session

OUT_DIR = ORCHESTRATOR_DIR / "experiments" / "e7_out_of_sample"
MONTH = (date(2026, 2, 1), date(2026, 2, 28))          # plug-in evenings considered
# NSW gazetted public holidays touching Feb/Mar 2026: none in February (Australia Day
# is 26 Jan, Good Friday 3 Apr). Listed so the exclusion is explicit and checkable.
NSW_PUBLIC_HOLIDAYS = {date(2026, 1, 1), date(2026, 1, 26), date(2026, 4, 3), date(2026, 4, 4),
                       date(2026, 4, 5), date(2026, 4, 6), date(2026, 4, 25)}
RANKS = {"most volatile": 1, "in between (rank 2)": 2, "in between (rank 4)": 4, "flattest": -1}
PRESETS = ["A", "B", "C"]
HIGH = 300.0
FAIL_RATIO = 0.5
TOL = 1e-6


def _at(d: date, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(hh, mm))


def session_window(d: date, cfg):
    start, dep = _at(d, cfg.session_start), _at(d, cfg.departure)
    return start, dep + timedelta(days=1) if dep <= start else dep


def rank_sessions(dispatch: pd.Series, cfg) -> pd.DataFrame:
    rows = []
    d = MONTH[0]
    while d <= MONTH[1]:
        start, dep = session_window(d, cfg)
        x = dispatch[(dispatch.index > start) & (dispatch.index <= dep)]
        rows.append({"session": d, "weekday": d.strftime("%a"), "n_prices": len(x),
                     "complete": len(x) == 156,
                     "holiday": d in NSW_PUBLIC_HOLIDAYS or dep.date() in NSW_PUBLIC_HOLIDAYS,
                     "sd": x.std(), "mean": x.mean(), "min": x.min(), "max": x.max()})
        d += timedelta(days=1)
    df = pd.DataFrame(rows)
    ok = df[df.complete & ~df.holiday].sort_values("sd", ascending=False)
    df["rank"] = pd.Series(range(1, len(ok) + 1), index=ok.index)
    return df.sort_values(["rank", "session"]).reset_index(drop=True)


def pick_days(ranked: pd.DataFrame) -> Dict[str, date]:
    ok = ranked.dropna(subset=["rank"]).sort_values("rank")
    picks = {}
    for label, r in RANKS.items():
        row = ok.iloc[r - 1] if r > 0 else ok.iloc[r]
        picks[f"{row.session:%b %d}"] = (row.session, label)
    # chronological order
    return dict(sorted(picks.items(), key=lambda kv: kv[1][0]))


def forecast_failures(pd_frame: pd.DataFrame, actual: pd.Series, d: date, cfg) -> pd.DataFrame:
    start, dep = session_window(d, cfg)
    f = pd_frame[(pd_frame.target > start) & (pd_frame.target <= dep) &
                 (pd_frame.published >= start - timedelta(minutes=30)) &
                 (pd_frame.published < pd_frame.target) & (pd_frame.rrp > HIGH)].copy()
    if f.empty:
        return pd.DataFrame()
    f["actual"] = f.target.map(actual)
    f["lead_h"] = (f.target - f.published).dt.total_seconds() / 3600
    out = []
    for target, g in f.groupby("target"):
        g = g.sort_values("published")
        last = g.iloc[-1]
        out.append({"session": d, "target": target, "runs_above_300": len(g),
                    "max_forecast": g.rrp.max(), "first_forecast": g.rrp.iloc[0],
                    "first_lead_h": g.lead_h.iloc[0], "last_forecast": last.rrp,
                    "last_lead_h": last.lead_h, "actual": last.actual,
                    "actual_over_max": last.actual / g.rrp.max(),
                    "failed_vs_max": last.actual < FAIL_RATIO * g.rrp.max(),
                    "failed_vs_last": last.actual < FAIL_RATIO * last.rrp})
    return pd.DataFrame(out)


def month_ratio_check(pairs: pd.DataFrame, label: str) -> pd.DataFrame:
    """Share of forecasts >= $300 whose actual came in below 50 % of the forecast, by lead bin."""
    hi = pairs[pairs.forecast >= HIGH]
    rows = []
    for lb, *_ in LEAD_BINS:
        g = hi[hi.lead_bin == lb]
        rows.append({"month": label, "lead_bin": lb, "pairs": len(g),
                     "failed_lt_50pct": int((g.ratio < FAIL_RATIO).sum()),
                     "failed_share": float((g.ratio < FAIL_RATIO).mean()) if len(g) else np.nan,
                     "median_ratio": float(g.ratio.median()) if len(g) else np.nan})
    g = hi
    rows.append({"month": label, "lead_bin": "all", "pairs": len(g),
                 "failed_lt_50pct": int((g.ratio < FAIL_RATIO).sum()),
                 "failed_share": float((g.ratio < FAIL_RATIO).mean()) if len(g) else np.nan,
                 "median_ratio": float(g.ratio.median()) if len(g) else np.nan})
    return pd.DataFrame(rows)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    a = ap.parse_args(argv)
    out = a.out_dir
    out.mkdir(parents=True, exist_ok=True)

    icfg = load_config()
    cfg = load_decision_config()
    pd_frame = load_tables(find_files(icfg, "pd"), PD_SPEC, icfg.region)
    disp = load_tables(find_files(icfg, "dispatch"), DISPATCH_SPEC, icfg.region)
    dispatch = disp.set_index("target")["rrp"]
    assert dispatch.index.max() >= pd.Timestamp("2026-03-01 00:00"), "February DISPATCHPRICE archive missing"

    # 1. the error table is built from the January train window only
    jan_pairs, table = build_training_table(icfg, pd_frame, disp)
    assert jan_pairs.target.max() <= pd.Timestamp("2026-02-01 00:00"), "train window leaks into February"
    lookup = ErrorLookup(table, icfg.raw.get("error_distributions", {}).get("min_count", 100))

    # 2. session selection
    ranked = rank_sessions(dispatch, cfg)
    ranked.to_csv(out / "session_ranking.csv", index=False, float_format="%.4f")
    days = pick_days(ranked)

    # 3. strategies
    data = Data(ArchivePriceSource(pd_frame, dispatch_frame=disp), dispatch)
    normal = load_decision_config()
    sell_only = load_decision_config(overrides={"presets": {p: {"buy": None} for p in PRESETS}})
    blind = load_decision_config(overrides={"presets": {p: None for p in PRESETS}})
    rows: List[dict] = []
    for day, (d, profile) in days.items():
        base_rows = []
        def add(strategy, df):
            s = summary(df)
            base_rows.append(s["baseline_cost"])
            rows.append({"day": day, "profile": profile, "strategy": strategy, **s})
        for p in PRESETS:
            add(p, run_session(d, p, data, cfg=normal, lookup=lookup))
        for p in PRESETS:
            add(f"{p} sell-only", run_session(d, p, data, cfg=sell_only, lookup=lookup))
        blind_profit = []
        for p in PRESETS:
            s = summary(run_session(d, p, data, cfg=blind, lookup=lookup))
            blind_profit.append(s["profit_vs_baseline"])
            if p == "A":
                rows.append({"day": day, "profile": profile, "strategy": "Unadjusted", **s})
                base_rows.append(s["baseline_cost"])
        assert max(blind_profit) - min(blind_profit) < TOL, f"{day}: cost-blind presets differ {blind_profit}"
        for label, thr in (("ToU $300", 300.0), ("ToU $150", 150.0)):
            add(label, run_session(d, "Naive", data, cfg=normal, lookup=lookup,
                                   planner=make_tou_planner(export_threshold=thr)))
        prices = session_prices(dispatch, d, cfg).to_numpy()
        base = baseline_cost(prices, cfg, cfg.initial_soc_pct)
        assert max(abs(b - base) for b in base_rows) < 5e-5, f"{day}: baseline mismatch"
        r = solve(prices, cfg, cfg.initial_soc_pct, modes=True, dc_charge=False)
        r["schedule"].to_csv(out / f"schedule_{d:%b%d}_Optimal_b.csv", index_label="slot", float_format="%.4f")
        rows.append({"day": day, "profile": profile, "strategy": "Optimal (b)", "exports": r["exports"],
                     "export_kwh": r["export_kwh"], "charges": r["charges"], "switches": r["switches"],
                     "net_cashflow": r["net_cashflow"], "baseline_cost": base,
                     "profit_vs_baseline": base + r["net_cashflow"],
                     "final_soc": r["final_soc"], "min_soc": r["min_soc"]})
        rows.append({"day": day, "profile": profile, "strategy": "Baseline", "exports": 0, "export_kwh": 0.0,
                     "switches": 0, "net_cashflow": -base, "baseline_cost": base, "profit_vs_baseline": 0.0,
                     "final_soc": cfg.target_soc_pct,
                     "min_soc": cfg.initial_soc_pct})
    res = pd.DataFrame(rows)
    res.to_csv(out / "results.csv", index=False, float_format="%.4f")

    order = ["Baseline", "A", "B", "C", "A sell-only", "B sell-only", "C sell-only", "Unadjusted",
             "ToU $300", "ToU $150", "Optimal (b)"]
    day_order = list(days)
    piv = res.pivot(index="day", columns="strategy", values="profit_vs_baseline").reindex(day_order)[order]
    for day, r in piv.iterrows():
        for s in order:
            assert r["Optimal (b)"] >= r[s] - TOL, f"{day}: {s} {r[s]:.4f} exceeds Optimal (b)"
    volatile = next(k for k, (_, p) in days.items() if p == "most volatile")
    piv.loc["Total"] = piv.loc[day_order].sum()
    piv.loc[f"Total excl. {volatile}"] = piv.loc[[x for x in day_order if x != volatile]].sum()
    pct = piv.div(piv["Optimal (b)"], axis=0) * 100
    soc_min = res.pivot(index="day", columns="strategy", values="min_soc").reindex(day_order)[order]
    soc_fin = res.pivot(index="day", columns="strategy", values="final_soc").reindex(day_order)[order]
    piv.to_csv(out / "profit_table.csv", float_format="%.4f")
    pct.to_csv(out / "pct_of_optimal_b.csv", float_format="%.2f")
    soc_min.to_csv(out / "min_soc.csv", float_format="%.2f")
    soc_fin.to_csv(out / "final_soc.csv", float_format="%.2f")

    # 4. forecast failures: selected sessions, every February session, and month-level ratios
    actual = actual_30min(disp)
    fails = pd.concat([forecast_failures(pd_frame, actual, d, cfg) for d in ranked.session], ignore_index=True)
    fails.to_csv(out / "high_forecasts_feb_sessions.csv", index=False, float_format="%.4f")
    all_pairs = build_pairs(pd_frame, disp)
    feb_pairs = all_pairs[(all_pairs.target > pd.Timestamp("2026-02-01 00:00")) &
                          (all_pairs.target <= pd.Timestamp("2026-03-01 00:00"))]
    ratio = pd.concat([month_ratio_check(jan_pairs, "Jan 2026 (train)"),
                       month_ratio_check(feb_pairs, "Feb 2026 (test)")], ignore_index=True)
    ratio.to_csv(out / "high_forecast_outcomes_by_month.csv", index=False, float_format="%.4f")

    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.float_format", "{:.2f}".format):
        print("sessions ranked\n", ranked.to_string(index=False))
        print("\npicked", {k: (str(v[0]), v[1]) for k, v in days.items()})
        print("\nprofit vs baseline ($)\n", piv.to_string())
        print("\n% of Optimal (b)\n", pct.to_string())
        print("\nmin SoC\n", soc_min.to_string())
        print("\nfinal SoC\n", soc_fin.to_string())
        print("\nhigh forecasts in February sessions\n", fails.to_string(index=False))
        print("\nforecast >= $300 outcomes by month\n", ratio.to_string(index=False))
    print(f"\nchecks passed (error table January-only, baselines match, optimum >= all); written to {out}")


if __name__ == "__main__":
    main()
