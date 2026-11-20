"""SoC as a range (lower, upper) in %, estimated from whatever is available at t.

Priority, applied over the sample history up to t:

1. Direct reading (DC-BPT): range = reading +/- direct_tolerance_pct. Each
   valid reading re-anchors the estimate and resets the uncertainty.
2. Metered energy since the last anchor (AC mode, or DC between readings):
   mid  = anchor + battery_energy / capacity
   half = anchor_half_width + ac_uncertainty_fraction * |energy moved| / capacity
   so the range widens with every kWh moved without a reading.
3. No reading ever: the user-entered SoC is the anchor (+/- user_entry_tolerance_pct).
4. Nothing at all: lower = min_soc, upper = soc_max.

Readings outside [soc_min, soc_max] are discarded (counted, treated as missing).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from .config import SocParams, Thresholds, UserSettings
from .sources import SocSample


@dataclass(frozen=True)
class SocEstimate:
    lower: float
    upper: float
    method: str                     # "direct" | "meter_estimate" | "user_entry" | "assumed_minimum"
    anchor_time: Optional[datetime]  # time of the reading the estimate is built on
    energy_since_anchor_kwh: float  # signed metered energy since the anchor
    invalid_readings: int           # readings discarded as out of range
    uncertain: bool                 # width > uncertain_width_pct

    @property
    def width(self) -> float:
        return self.upper - self.lower

    @property
    def mid(self) -> float:
        return (self.lower + self.upper) / 2.0


def estimate_soc(
    samples: Sequence[SocSample],
    user: UserSettings,
    params: SocParams,
    limits: Thresholds,
) -> SocEstimate:
    lo_lim, hi_lim = limits.soc_min, limits.soc_max
    valid = lambda x: x is not None and lo_lim <= x <= hi_lim  # noqa: E731

    anchor_mid: Optional[float] = None
    anchor_half = 0.0
    anchor_kind = None
    anchor_time = None
    if valid(user.user_soc):
        anchor_mid, anchor_half, anchor_kind = user.user_soc, params.user_entry_tolerance_pct, "user_entry"

    battery_kwh = 0.0   # battery-side energy since anchor, signed
    moved_kwh = 0.0     # |energy| moved since anchor
    metered_kwh = 0.0   # metered energy since anchor, signed
    last_meter: Optional[float] = None
    invalid = 0
    eff = params.ac_charge_efficiency

    for s in samples:
        if s.soc_reading is not None:
            if valid(s.soc_reading):
                anchor_mid, anchor_half, anchor_kind = s.soc_reading, params.direct_tolerance_pct, "direct"
                anchor_time = s.time
                battery_kwh = moved_kwh = metered_kwh = 0.0
                last_meter = s.meter_energy_kwh
                continue
            invalid += 1
        if last_meter is not None:
            d = s.meter_energy_kwh - last_meter
            metered_kwh += d
            moved_kwh += abs(d)
            battery_kwh += d * eff if d >= 0 else d / eff   # losses both ways
        last_meter = s.meter_energy_kwh

    cap = user.battery_capacity_kwh
    if anchor_mid is None:
        lower, upper, method = user.min_soc, hi_lim, "assumed_minimum"
    else:
        mid = anchor_mid + 100.0 * battery_kwh / cap
        half = anchor_half + params.ac_uncertainty_fraction * 100.0 * moved_kwh / cap
        lower, upper = mid - half, mid + half
        method = anchor_kind if moved_kwh == 0 else "meter_estimate"

    lower = min(max(lower, lo_lim), hi_lim)
    upper = min(max(upper, lo_lim), hi_lim)
    return SocEstimate(
        lower=lower,
        upper=upper,
        method=method,
        anchor_time=anchor_time,
        energy_since_anchor_kwh=metered_kwh,
        invalid_readings=invalid,
        uncertain=(upper - lower) > params.uncertain_width_pct,
    )
