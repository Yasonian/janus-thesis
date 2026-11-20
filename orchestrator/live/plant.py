"""Decision-time battery model ("plant") for compressed-clock SiL experiments.

The decision clock runs ~30x faster than EVerest's real-time charging, so the
energy that physically flows in the SiL is meaningless for SoC and money. The
plant applies each executed decision for one full decision slot (5 min) with
the same rules as decision/scenario_runner.py:

* EXPORT (DC_BPT only): battery -export_power*dt, sold at the actual dispatch
  price, minus degradation; clamped at min_soc.
* CHARGE: battery +P*dt with P = dc_charge_power in DC_BPT, ac_charge_power in
  AC; grid import = battery energy / eta; clamped at 100 %.
* IDLE: nothing.

The mode a slot executes in is the mode after that cycle's command (a switch
commanded at t applies to (t, t+5]); a switch blocked by the guard leaves the
old mode. EXPORT requested while the mode is AC is not executable -> IDLE.
Switch downtime is informational, as in the offline runner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from orchestrator.decision.config import DecisionConfig


@dataclass
class PlantStep:
    cycle: int
    sim_time: str
    slot_end: str
    requested: str
    mode: str
    executed: str
    price: float
    kwh: float                 # battery side (+ in, - out)
    grid_kwh: float            # + import, - export
    cashflow: float            # $ (+ revenue)
    soc_before: float
    soc_after: float
    cum_cashflow: float
    meter_import_wh: float
    meter_export_wh: float


@dataclass
class BatteryPlant:
    cfg: DecisionConfig
    soc: float
    cum_cashflow: float = 0.0
    import_wh: float = 0.0
    export_wh: float = 0.0
    steps: List[PlantStep] = field(default_factory=list)

    def apply(self, cycle: int, sim_time: str, slot_end: str, action: str, mode: str,
              price: float) -> PlantStep:
        c = self.cfg
        dt, cap = c.slot_h, c.capacity_kwh
        before = self.soc
        executed, kwh, grid, cash = action, 0.0, 0.0, 0.0
        if action == "EXPORT":
            if mode != "DC_BPT":
                executed = "IDLE"
            else:
                kwh = -max(0.0, min(c.export_power_kw * dt, (self.soc - c.min_soc_pct) / 100 * cap))
                grid = kwh
                cash = price / 1000 * -kwh - c.degradation_cost_per_kwh * -kwh
        elif action == "CHARGE":
            p = c.dc_charge_power_kw if mode == "DC_BPT" else c.ac_charge_power_kw
            kwh = max(0.0, min(p * dt, (100 - self.soc) / 100 * cap))
            grid = kwh / c.eta
            cash = -price / 1000 * grid
        self.soc += kwh / cap * 100
        self.cum_cashflow += cash
        if grid > 0:
            self.import_wh += grid * 1000
        else:
            self.export_wh += -grid * 1000
        step = PlantStep(cycle, sim_time, slot_end, action, mode, executed, price, kwh, grid, cash,
                         before, self.soc, self.cum_cashflow, self.import_wh, self.export_wh)
        self.steps.append(step)
        return step


def baseline_cost(cfg: DecisionConfig, soc: float, prices: List[float]) -> float:
    """Plain AC charging from plug-in until target (same as scenario_runner)."""
    cost = 0.0
    for price in prices:
        if soc >= cfg.target_soc_pct:
            break
        b = min(cfg.ac_charge_power_kw * cfg.slot_h, (cfg.target_soc_pct - soc) / 100 * cfg.capacity_kwh)
        soc += b / cfg.capacity_kwh * 100
        cost += price / 1000 * b / cfg.eta
    return cost
