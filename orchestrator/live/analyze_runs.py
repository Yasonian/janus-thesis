"""Summarise live experiment runs (one run directory per preset) and cross-check offline.

    python -m orchestrator.live.analyze_runs live_logs/*_jan10_* --out live_logs/jan10_results

Per run (from plant.jsonl, decisions.jsonl, switches.jsonl):
  SoC trace, executed exports/charges, EXPORT requests that could not execute,
  guard blocks, switch count/direction/timings, revenue, charge cost, net cashflow,
  and profit vs the Baseline run (= net cashflow - Baseline net cashflow).
Cross-check: decision/scenario_runner.run_session for the same day and preset
(offline replay, same energy rules, no SiL).
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd


def read_jsonl(path: Path) -> List[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def summarise_run(run_dir: Path) -> dict:
    run = json.loads((run_dir / "run.json").read_text())
    plant = pd.DataFrame(read_jsonl(run_dir / "plant.jsonl"))
    dec = pd.DataFrame(read_jsonl(run_dir / "decisions.jsonl"))
    sw = read_jsonl(run_dir / "switches.jsonl")
    blocked = dec["guard"].apply(lambda g: isinstance(g, dict) and not g.get("ok")).sum() if len(dec) else 0
    fwd = [s for s in sw if s["to_mode"] == "DC_BPT"]
    rev = [s for s in sw if s["to_mode"] == "AC"]
    col = lambda xs, k: [s[k] for s in xs if s.get(k) is not None]  # noqa: E731
    rev_cash = plant.loc[plant.cashflow > 0, "cashflow"].sum()
    cost = -plant.loc[plant.cashflow < 0, "cashflow"].sum()
    return {
        "preset": run["preset"], "run_dir": run_dir.name,
        "cycles": len(dec), "plant_steps": len(plant),
        "exports_executed": int((plant.executed == "EXPORT").sum()),
        "export_requests_not_executed": int(((plant.requested == "EXPORT") & (plant.executed != "EXPORT")).sum()),
        "charges": int((plant.executed == "CHARGE").sum()),
        "export_kwh": float(-plant.loc[plant.kwh < 0, "kwh"].sum()) + 0.0,   # + 0.0: no "-0.00"
        "import_kwh_grid": float(plant.loc[plant.grid_kwh > 0, "grid_kwh"].sum()),
        "switches": len(sw), "switches_ac_to_dc": len(fwd), "switches_dc_to_ac": len(rev),
        "guard_blocks": int(blocked),
        "fwd_cmd_to_charging_s": col(fwd, "cmd_to_charging_s"),
        "rev_cmd_to_charging_s": col(rev, "cmd_to_charging_s"),
        "fwd_event_s": col(fwd, "cmd_to_charging_started_event_s"),
        "rev_event_s": col(rev, "cmd_to_charging_started_event_s"),
        "responder_delay_s": col(sw, "responder_delay_s"),
        "label_adjusted": sum(1 for s in sw if s.get("label_adjusted")),
        "revenue": float(rev_cash), "charge_cost": float(cost), "net_cashflow": float(plant.cashflow.sum()),
        "soc_start": float(plant.soc_before.iloc[0]), "soc_end": float(plant.soc_after.iloc[-1]),
        "soc_min": float(plant.soc_after.min()),
        # run validity: decision cadence must stay at the configured wall period
        "max_cycle_gap_s": float(dec["wall"].diff().max()) if len(dec) > 1 else None,
        "stalls": len(read_jsonl(run_dir / "stalls.jsonl")),
    }


def offline(session: date, preset: str) -> dict:
    from orchestrator.decision.scenario_runner import load_data, run_session, summarise
    if preset == "Baseline":
        return {}
    df = run_session(session, preset, load_data("real"))
    s = summarise(df, "offline")
    rev = df.loc[df.cashflow > 0, "cashflow"].sum()
    return {"offline_exports": s["exports"], "offline_switches": s["mode_switches"],
            "offline_revenue": float(rev), "offline_net_cashflow": s["net_cashflow"],
            "offline_baseline_cost": s["baseline_cost"], "offline_final_soc": s["final_soc"]}


def plot(runs: Dict[str, Path], out: Path, session: date) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as md
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    colors = {"Baseline": "#777777", "A": "#1f77b4", "B": "#2ca02c", "C": "#d62728"}
    price = None
    for preset, d in runs.items():
        p = pd.DataFrame(read_jsonl(d / "plant.jsonl"))
        t = pd.to_datetime(p.slot_end)
        a1.step(t, p.soc_after, where="pre", label=preset, color=colors.get(preset), lw=1.6)
        ex = p.executed == "EXPORT"
        a1.scatter(t[ex], p.soc_after[ex], s=10, color=colors.get(preset))
        if price is None:
            price = (t, p.price)
    a1.axhline(20, ls="--", lw=0.8, color="#999"); a1.axhline(80, ls=":", lw=0.8, color="#999")
    a1.set_ylabel("Battery SoC (%)"); a1.legend(); a1.grid(alpha=.3)
    a1.set_title(f"{session:%b %d %Y} NSW1, live SiL replay (dots = executed exports)", fontsize=10)
    a2.step(price[0], price[1], where="pre", color="#444", lw=1)
    a2.set_yscale("symlog", linthresh=300); a2.set_ylabel("Dispatch RRP $/MWh"); a2.grid(alpha=.3)
    a2.xaxis.set_major_formatter(md.DateFormatter("%H:%M"))
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)


def decision_table(run_dir: Path) -> pd.DataFrame:
    dec = pd.DataFrame(read_jsonl(run_dir / "decisions.jsonl"))
    plant = pd.DataFrame(read_jsonl(run_dir / "plant.jsonl"))
    why = pd.json_normalize(dec["why"]).add_prefix("why_")
    soc = pd.json_normalize(dec["soc"]).add_prefix("soc_est_")
    g = dec["guard"].apply(lambda x: "" if not isinstance(x, dict) else ("ok" if x["ok"] else ";".join(x["reasons"])))
    out = pd.concat([dec[["cycle", "sim_time", "system_mode", "action", "mode_command", "forecast_rrp",
                          "sell_price", "buy_price", "pd_run"]], soc, why], axis=1)
    out["guard"] = g
    out["command_sent"] = dec["command_sent"].apply(lambda c: c["target"] if isinstance(c, dict) else "")
    out["overrides"] = dec["overrides"].apply(lambda o: ";".join(o) if o else "")
    if len(plant):
        out = out.merge(plant[["cycle", "executed", "price", "kwh", "cashflow", "soc_after", "cum_cashflow"]]
                        .rename(columns={"price": "actual_rrp", "soc_after": "soc_true_after"}), on="cycle", how="left")
    return out


def markdown_tables(df: pd.DataFrame, runs: Dict[str, Path]) -> str:
    """Results / cross-check / switch-timing tables in the README format."""
    cols = list(df.preset)
    g = lambda k, fmt="{:.2f}": [fmt.format(v) if pd.notna(v) else "n/a" for v in df[k]]  # noqa: E731
    rows = [("Exports executed (5-min slots)", g("exports_executed", "{:d}")),
            ("Energy exported (kWh)", g("export_kwh")), ("Charge slots", g("charges", "{:d}")),
            ("Grid import (kWh)", g("import_kwh_grid")), ("Revenue ($)", g("revenue")),
            ("Charge cost ($)", g("charge_cost")), ("**Net cashflow ($)**", g("net_cashflow")),
            ("**Profit vs Baseline ($)**", g("profit_vs_baseline")), ("Final SoC (%)", g("soc_end", "{:.1f}")),
            ("Minimum SoC (%)", g("soc_min", "{:.1f}")), ("Mode switches", g("switches", "{:d}")),
            ("Guard blocks", g("guard_blocks", "{:d}"))]
    md = ["| | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    md += [f"| {name} | " + " | ".join(v) + " |" for name, v in rows]
    off = df[df.preset != "Baseline"]
    if "offline_net_cashflow" in off:
        md += ["", "| | " + " | ".join(off.preset) + " |", "|---" * (len(off) + 1) + "|",
               "| Net cashflow live / offline ($) | " + " | ".join(
                   f"{a:.2f} / {b:.2f}" for a, b in zip(off.net_cashflow, off.offline_net_cashflow)) + " |",
               "| Exports live / offline | " + " | ".join(
                   f"{a} / {int(b)}" for a, b in zip(off.exports_executed, off.offline_exports)) + " |",
               "| Switches live / offline | " + " | ".join(
                   f"{a} / {int(b)}" for a, b in zip(off.switches, off.offline_switches)) + " |"]
    md += ["", "| Preset | Decision time | Direction | Cmd → Charging (s) | Cmd → ChargingStarted (s) | V2G-Key `elapsed_ms` |",
           "|---|---|---|---|---|---|"]
    n = 0
    for preset, d in runs.items():
        for s in read_jsonl(d / "switches.jsonl"):
            n += 1
            md.append(f"| {preset} | {s['sim_time'][11:16]} | {s['from_mode']}→{s['to_mode']} | "
                      f"{s['cmd_to_charging_s']} | {s.get('cmd_to_charging_started_event_s')} | {s['v2g_key_elapsed_ms']} |")
    if n == 0:
        md.append("| (no switches) | | | | | |")
    return "\n".join(md) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--session", default="2026-01-10")
    ap.add_argument("--no-offline", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    runs = {}
    rows = []
    for r in map(Path, a.runs):
        s = summarise_run(r)
        runs[s["preset"]] = r
        if not a.no_offline:
            s.update(offline(date.fromisoformat(a.session), s["preset"]))
        rows.append(s)
        decision_table(r).to_csv(out / f"decisions_{s['preset']}.csv", index=False, float_format="%.4f")
    df = pd.DataFrame(rows)
    base = df.loc[df.preset == "Baseline", "net_cashflow"]
    if len(base):
        df["profit_vs_baseline"] = df["net_cashflow"] - float(base.iloc[0])
    order = {"Baseline": 0, "A": 1, "B": 2, "C": 3}
    df = df.sort_values("preset", key=lambda s: s.map(order)).reset_index(drop=True)
    df.to_csv(out / "summary.csv", index=False, float_format="%.4f")
    (out / "summary.json").write_text(df.to_json(orient="records", indent=2))
    plot(runs, out / "soc_traces.png", date.fromisoformat(a.session))
    (out / "tables.md").write_text(markdown_tables(df, runs))
    with pd.option_context("display.width", 250, "display.max_columns", 50, "display.float_format", "{:.2f}".format):
        print(df.drop(columns=["run_dir"]).T.to_string())


if __name__ == "__main__":
    main()
