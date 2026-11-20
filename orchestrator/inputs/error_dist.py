"""Build PD forecast-error distributions from historical PD + dispatch prices.

    python -m orchestrator.inputs.error_dist            # uses files in JANUS_AEMO_DATA_DIR

Pairs every PD row (run, 30-min target interval) with the actual price for that
interval = mean of the six 5-min dispatch prices ending T-25 ... T. Pairs with
fewer than six dispatch prices are dropped. All runs are kept, so one target
interval contributes one pair per run (at different lead times).

    error     = actual_mean - forecast_rrp            ($/MWh)
    lead_time = DATETIME - LASTCHANGED                (hours)

Only pairs whose target interval ends inside error_distributions.train_window
(inputs.yaml; January 2026) are used, so adding later monthly archives to the data
dir (e.g. February for the out-of-sample test E7) does not change the table.

Pairs are grouped by lead-time bin x FORECAST-price bin. Bins are half-open
[lo, hi); the first and last price bins are open-ended.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .aemo_csv import DISPATCH_SPEC, PD_SPEC, load_tables

LEAD_BINS: List[Tuple[str, float, float]] = [
    ("0-2h", 0, 2), ("2-6h", 2, 6), ("6-12h", 6, 12), ("12-24h", 12, 24), ("24h+", 24, math.inf),
]
PRICE_BINS: List[Tuple[str, float, float]] = [
    ("<0", -math.inf, 0), ("0-100", 0, 100), ("100-300", 100, 300),
    ("300-1000", 300, 1000), (">1000", 1000, math.inf),
]
ALL_PRICES = "all"           # per-lead-bin group with every price bin merged (lookup fallback)
# Price bins whose distribution is stored as a RATIO actual / forecast (dimensionless);
# all other bins store the ABSOLUTE error actual - forecast ($/MWh). Ratios break
# near zero, so they are used only where the forecast is >= $300.
RATIO_BINS = {"300-1000", ">1000"}
QUANTILES = {"q10": 0.10, "q25": 0.25, "q50": 0.50, "q75": 0.75, "q90": 0.90}

# Thin high-price groups are pooled across adjacent lead-time bins (never across
# price bins: error scale differs by orders of magnitude between price bins).
# {price_bin: {merged_lead_label: [member lead bins]}}
LEAD_MERGES: Dict[str, Dict[str, List[str]]] = {
    "300-1000": {"0-12h": ["0-2h", "2-6h", "6-12h"]},
    ">1000": {"0-6h": ["0-2h", "2-6h"]},
}

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "data"


def bin_label(value: float, bins: Sequence[Tuple[str, float, float]]) -> str:
    """Label of the [lo, hi) bin containing value; clamps to the edge bins."""
    if value < bins[0][2]:
        return bins[0][0]
    for label, lo, hi in bins:
        if lo <= value < hi:
            return label
    return bins[-1][0]


def actual_30min(dispatch: pd.DataFrame) -> pd.Series:
    """Mean of the six 5-min dispatch prices in each 30-min interval, indexed by interval end.

    Only complete intervals (exactly six prices) are returned.
    """
    s = dispatch.set_index("target")["rrp"].sort_index()
    # interval-ending: the 5-min price ending at e belongs to the 30-min interval ending ceil(e, 30min)
    end30 = s.index.ceil("30min")
    g = s.groupby(end30)
    out = pd.DataFrame({"actual": g.mean(), "n": g.size()})
    return out.loc[out["n"] == 6, "actual"]


def build_pairs(pd_frame: pd.DataFrame, dispatch: pd.DataFrame) -> pd.DataFrame:
    actual = actual_30min(dispatch)
    pairs = pd_frame.join(actual, on="target", how="inner")
    pairs = pairs.rename(columns={"rrp": "forecast"})
    pairs["error"] = pairs["actual"] - pairs["forecast"]
    pairs["ratio"] = pairs["actual"] / pairs["forecast"]   # used only for RATIO_BINS (forecast >= 300)
    pairs["lead_h"] = (pairs["target"] - pairs["published"]).dt.total_seconds() / 3600.0
    pairs["lead_bin"] = [bin_label(x, LEAD_BINS) for x in pairs["lead_h"]]
    pairs["price_bin"] = [bin_label(x, PRICE_BINS) for x in pairs["forecast"]]
    return pairs.reset_index(drop=True)


def filter_window(pairs: pd.DataFrame, start, end) -> pd.DataFrame:
    """Keep pairs whose 30-min target interval ENDS in (start, end].

    Bounds are datetimes or ISO strings; None leaves that side open.
    """
    keep = pd.Series(True, index=pairs.index)
    if start is not None:
        keep &= pairs["target"] > pd.Timestamp(start)
    if end is not None:
        keep &= pairs["target"] <= pd.Timestamp(end)
    return pairs[keep].reset_index(drop=True)


def train_window(cfg) -> Tuple[Optional[str], Optional[str]]:
    """(start, end) of the training window from inputs.yaml error_distributions.train_window."""
    w = (cfg.raw.get("error_distributions", {}) or {}).get("train_window") or {}
    return w.get("start"), w.get("end")


def build_training_table(cfg, pd_frame: pd.DataFrame, dispatch: pd.DataFrame):
    """(pairs, table) restricted to the configured training window."""
    pairs = filter_window(build_pairs(pd_frame, dispatch), *train_window(cfg))
    return pairs, build_table(pairs)


def _stats(err: pd.Series, kind: str = "absolute") -> Dict[str, float]:
    if len(err) == 0:
        return {"kind": kind, **{k: None for k in QUANTILES}, "count": 0, "mean": None, "std": None}
    q = err.quantile(list(QUANTILES.values())).to_numpy()
    return {"kind": kind, **{k: float(v) for k, v in zip(QUANTILES, q)},
            "count": int(len(err)), "mean": float(err.mean()),
            "std": float(err.std()) if len(err) > 1 else None}


def _group_stats(rows: pd.DataFrame, price_bin: str) -> Dict[str, float]:
    if price_bin in RATIO_BINS:
        return _stats(rows["ratio"], "ratio")
    return _stats(rows["error"], "absolute")


def build_table(pairs: pd.DataFrame) -> Dict[str, Dict[str, Dict[str, float]]]:
    """{lead_bin: {price_bin | "all": {kind, q10, q25, q50, q75, q90, count, mean, std[, merged]}}}

    ``kind`` is "ratio" (actual / forecast) for RATIO_BINS, else "absolute"
    (actual - forecast, $/MWh). The "all" fallback group is always absolute.

    Cells that belong to a LEAD_MERGES group also carry
    ``"merged": {"lead_bin": "0-12h", "members": [...], q10, ..., std}``,
    the stats of the pooled group, used by the lookup when the cell is thin.
    """
    table: Dict[str, Dict[str, Dict[str, float]]] = {}
    for lb, *_ in LEAD_BINS:
        sub = pairs[pairs["lead_bin"] == lb]
        table[lb] = {pb: _group_stats(sub[sub["price_bin"] == pb], pb) for pb, *_ in PRICE_BINS}
        table[lb][ALL_PRICES] = _stats(sub["error"])
    for pb, groups in LEAD_MERGES.items():
        for label, members in groups.items():
            sel = pairs[(pairs["price_bin"] == pb) & pairs["lead_bin"].isin(members)]
            merged = {"lead_bin": label, "members": list(members), **_group_stats(sel, pb)}
            for lb in members:
                table[lb][pb]["merged"] = merged
    return table


def summary_frame(table) -> pd.DataFrame:
    """One row per original group, then one row per merged group (group="merged").

    error_kind: "absolute" (quantiles in $/MWh) or "ratio" (actual / forecast).
    """
    stat_keys = [*QUANTILES, "count", "mean", "std"]
    rows = [{"group": "original", "lead_bin": lb, "price_bin": pb,
             "merged_into": table[lb][pb].get("merged", {}).get("lead_bin", ""),
             "error_kind": table[lb][pb]["kind"], **{k: table[lb][pb][k] for k in stat_keys}}
            for lb, *_ in LEAD_BINS for pb, *_ in PRICE_BINS]
    for pb, groups in LEAD_MERGES.items():
        for label, members in groups.items():
            m = table[members[0]][pb]["merged"]
            rows.append({"group": "merged", "lead_bin": label, "price_bin": pb,
                         "merged_into": "+".join(members), "error_kind": m["kind"],
                         **{k: m[k] for k in stat_keys}})
    return pd.DataFrame(rows)


def heatmap(table, path: Path, min_count: int = 100) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm

    leads = [b[0] for b in LEAD_BINS]
    prices = [b[0] for b in PRICE_BINS]
    counts = np.array([[table[lb][pb]["count"] for pb in prices] for lb in leads], dtype=float)
    fig, ax = plt.subplots(figsize=(7.5, 5))
    im = ax.imshow(np.where(counts > 0, counts, np.nan), cmap="viridis",
                   norm=LogNorm(vmin=1, vmax=max(counts.max(), 1)))
    for i in range(len(leads)):
        for j in range(len(prices)):
            n = int(counts[i, j])
            ax.text(j, i, f"{n:,}", ha="center", va="center", fontsize=9,
                    color="#d62728" if n < min_count else ("white" if n < 3000 else "black"),
                    fontweight="bold" if n < min_count else "normal")
    ax.set_xticks(range(len(prices)), [f"${p}" for p in prices])
    ax.set_yticks(range(len(leads)), leads)
    ax.set_xlabel("Forecast price bin ($/MWh)")
    ax.set_ylabel("Lead time bin")
    ax.set_title(f"PD-vs-dispatch pairs per group, NSW1 Jan 2026 (red < {min_count})", fontsize=10)
    fig.colorbar(im, ax=ax, label="pairs (log scale)")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main(argv=None) -> None:
    from .config import load_config
    from .replay import find_files

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--heatmap", type=Path,
                    default=DEFAULT_OUT_DIR.parent / "scenarios" / "error_distribution_heatmap.png")
    a = ap.parse_args(argv)

    cfg = load_config()
    pd_frame = load_tables(find_files(cfg, "pd"), PD_SPEC, cfg.region)
    dispatch = load_tables(find_files(cfg, "dispatch"), DISPATCH_SPEC, cfg.region)
    pairs, table = build_training_table(cfg, pd_frame, dispatch)

    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "error_distributions.json").write_text(json.dumps(table, indent=2))
    summary_frame(table).to_csv(a.out_dir / "error_distributions_summary.csv", index=False,
                                float_format="%.4f")
    heatmap(table, a.heatmap, cfg.raw.get("error_distributions", {}).get("min_count", 100))
    start, end = train_window(cfg)
    print(f"{len(pd_frame)} PD rows, {len(pairs)} pairs with target in ({start}, {end}], written to {a.out_dir}")


if __name__ == "__main__":
    main()
