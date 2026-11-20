"""Assemble a merged 5-minute price forecast from the latest PD / P5MIN runs.

Conventions
-----------
* A 5-min slot is identified by its interval-ENDING time ``end``; it covers
  (end - 5 min, end].
* At decision time ``t`` (on the 5-min grid) the horizon starts with the slot
  ending at t + 5 min (the interval now in progress) and steps forward.
* A 30-min PD value for the interval ending T is held constant over the six
  slots ending T-25, T-20, ..., T.
* lead_time = slot end - LASTCHANGED of the run it came from.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .config import Thresholds
from .sources import ForecastRun

SLOT = timedelta(minutes=5)


def mwh_to_kwh(price_per_mwh: float) -> float:
    """Convert $/MWh to $/kWh."""
    return price_per_mwh / 1000.0


@dataclass(frozen=True)
class ForecastPoint:
    interval_end: datetime          # target 5-min interval (interval-ending)
    rrp: float                      # $/MWh
    source: str                     # "P5MIN" or "PD"
    run_id: str
    published: datetime             # LASTCHANGED of the run
    lead_time: timedelta            # interval_end - published
    native_interval_end: datetime   # the source's own interval (30-min for PD)

    @property
    def interval_start(self) -> datetime:
        return self.interval_end - SLOT

    @property
    def lead_minutes(self) -> float:
        return self.lead_time.total_seconds() / 60.0

    @property
    def native_lead_time(self) -> timedelta:
        """Lead time of the source interval (e.g. the 30-min PD interval)."""
        return self.native_interval_end - self.published

    @property
    def rrp_per_kwh(self) -> float:
        return mwh_to_kwh(self.rrp)


@dataclass(frozen=True)
class UnknownPeriod:
    start: datetime
    end: datetime
    reason: str          # "beyond_horizon" (forecast ends before departure) or "missing"


@dataclass
class AssembledForecast:
    points: List[ForecastPoint]
    unknown: List[UnknownPeriod]
    discarded: Dict[str, int] = field(default_factory=dict)  # invalid native rows per source
    p5min_used: bool = False


def _slots(run: ForecastRun, thresholds: Thresholds) -> Tuple[Dict[datetime, ForecastPoint], int]:
    """Expand one run to 5-min slots; drop invalid values. Returns (slots, n_discarded)."""
    per = 30 if run.kind == "PD" else 5
    n_sub = per // 5
    out: Dict[datetime, ForecastPoint] = {}
    discarded = 0
    for target, rrp in zip(run.frame["target"], run.frame["rrp"]):
        target = target.to_pydatetime()
        if rrp is None or math.isnan(rrp) or not (thresholds.price_floor <= rrp <= thresholds.price_cap):
            discarded += 1
            continue
        for k in range(n_sub):
            end = target - k * SLOT
            out[end] = ForecastPoint(end, float(rrp), run.kind, run.run_id, run.published,
                                     end - run.published, target)
    return out, discarded


def assemble(
    t: datetime,
    pd_run: Optional[ForecastRun],
    p5min_run: Optional[ForecastRun],
    thresholds: Thresholds,
    departure: Optional[datetime] = None,
) -> AssembledForecast:
    """Merge P5MIN (near term) and PD (after) into 5-min points from t onwards.

    ``p5min_run`` should already be None if it is unavailable or too stale;
    the caller decides that from the age thresholds.
    """
    for run in (pd_run, p5min_run):
        if run is not None and run.published > t:
            raise ValueError(f"look-ahead: {run.kind} run {run.run_id} published {run.published} > t={t}")

    pd_slots, pd_bad = _slots(pd_run, thresholds) if pd_run else ({}, 0)
    p5_slots, p5_bad = _slots(p5min_run, thresholds) if p5min_run else ({}, 0)
    near_end = t + timedelta(minutes=thresholds.p5min_window_minutes)

    covered = [e for e in list(pd_slots) + list(p5_slots) if e > t]
    last_covered = max(covered) if covered else t
    if departure is not None:
        # last slot needed is the one containing departure
        n = math.ceil((departure - t) / SLOT)
        horizon_end = t + max(n, 0) * SLOT
    else:
        horizon_end = last_covered

    points: List[ForecastPoint] = []
    missing: List[datetime] = []
    end = t + SLOT
    while end <= horizon_end:
        p = p5_slots.get(end) if end <= near_end else None
        p = p or pd_slots.get(end)
        if p is None:
            missing.append(end)
        else:
            points.append(p)
        end += SLOT

    unknown: List[UnknownPeriod] = []
    for e in missing:
        reason = "beyond_horizon" if e > last_covered else "missing"
        prev = unknown[-1] if unknown else None
        if prev and prev.end == e - SLOT and prev.reason == reason:
            unknown[-1] = UnknownPeriod(prev.start, e, reason)
        else:
            unknown.append(UnknownPeriod(e - SLOT, e, reason))

    discarded = {}
    if pd_bad:
        discarded["PD"] = pd_bad
    if p5_bad:
        discarded["P5MIN"] = p5_bad
    return AssembledForecast(points, unknown, discarded,
                             p5min_used=any(p.source == "P5MIN" for p in points))
