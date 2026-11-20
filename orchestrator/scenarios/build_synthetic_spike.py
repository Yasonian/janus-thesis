"""Build the synthetic short-spike scenario from the 11 Jan 2026 (flat) NSW1 data.

    .venv/bin/python scenarios/build_synthetic_spike.py

Interpretation of "18:00 and 18:05" (session plugs in at 18:00): the two 5-min
intervals that START at 18:00 and 18:05, i.e. SETTLEMENTDATE (interval-ending)
18:05 and 18:10, are set to $1,500. The PD "18:00 half-hour" is the interval
18:00-18:30 (DATETIME 18:30). It is set to $1,500 in the last PD run published
before 18:00 only (2026011128, LASTCHANGED 17:33:02); later runs are untouched,
so from 18:02:51 onwards the forecast reverts to the real one.

Writes canonical frames (run_id, published, target, region, rrp) to
scenarios/synthetic_spike/{pd,dispatch}.csv covering 11 Jan 00:00 - 12 Jan 08:00.
"""
from datetime import datetime
from pathlib import Path

from orchestrator.inputs.aemo_csv import DISPATCH_SPEC, PD_SPEC, load_tables
from orchestrator.inputs.config import load_config
from orchestrator.inputs.replay import find_files

OUT = Path(__file__).resolve().parent / "synthetic_spike"
SPIKE = 1500.0
DISPATCH_SLOTS = [datetime(2026, 1, 11, 18, 5), datetime(2026, 1, 11, 18, 10)]
PD_TARGET = datetime(2026, 1, 11, 18, 30)
WINDOW = (datetime(2026, 1, 11, 0, 0), datetime(2026, 1, 12, 8, 0))


def build(out_dir: Path = OUT) -> dict:
    cfg = load_config()
    pd_frame = load_tables(find_files(cfg, "pd"), PD_SPEC, cfg.region)
    disp = load_tables(find_files(cfg, "dispatch"), DISPATCH_SPEC, cfg.region)

    pd_win = pd_frame[(pd_frame["published"] >= WINDOW[0]) & (pd_frame["published"] < WINDOW[1])].copy()
    disp_win = disp[(disp["target"] > WINDOW[0]) & (disp["target"] <= WINDOW[1])].copy()

    before = pd_win[pd_win["published"] < datetime(2026, 1, 11, 18, 0)]
    run_id = before.loc[before["published"].idxmax(), "run_id"]
    sel = (pd_win["run_id"] == run_id) & (pd_win["target"] == PD_TARGET)
    assert sel.sum() == 1, "expected exactly one PD row to modify"
    old_pd = float(pd_win.loc[sel, "rrp"].iloc[0])
    pd_win.loc[sel, "rrp"] = SPIKE

    dsel = disp_win["target"].isin(DISPATCH_SLOTS)
    assert dsel.sum() == 2
    old_disp = disp_win.loc[dsel, "rrp"].tolist()
    disp_win.loc[dsel, "rrp"] = SPIKE

    out_dir.mkdir(parents=True, exist_ok=True)
    pd_win.to_csv(out_dir / "pd.csv", index=False)
    disp_win.to_csv(out_dir / "dispatch.csv", index=False)
    info = {"pd_run": run_id, "pd_published": str(before["published"].max()), "pd_target": str(PD_TARGET),
            "pd_old_rrp": old_pd, "dispatch_slots": [str(s) for s in DISPATCH_SLOTS],
            "dispatch_old_rrp": old_disp, "pd_rows": len(pd_win), "dispatch_rows": len(disp_win)}
    return info


if __name__ == "__main__":
    print(build())
