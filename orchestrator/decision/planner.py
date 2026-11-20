"""Greedy receding-horizon planner.

plan(snapshot, state, preset) builds a plan from t to departure in four steps
and returns only the current slot's action as executable:

1. adjust_prices  -- forecast + error quantile (per preset) -> sell / buy price
2. schedule       -- greedy exports (highest sell) + charges (lowest buy),
                     with a profitability loop
3. apply_safety   -- safe_mode, horizon gap, departure guarantee, SoC floor
4. derive_mode    -- EXPORT needs DC-BPT; CHARGE/IDLE prefer AC; minimum dwell

Energy convention (battery side): an EXPORT slot draws export_power * dt from
the battery and sells exactly that; a CHARGE slot adds charge_power * dt to the
battery and buys charge_power * dt / eta from the grid. eta is applied once.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from orchestrator.inputs.error_lookup import ErrorLookup, default_lookup
from orchestrator.inputs.snapshot import Snapshot

from .config import DecisionConfig, load_decision_config


class Action(str, Enum):
    CHARGE = "CHARGE"
    IDLE = "IDLE"
    EXPORT = "EXPORT"


class ModeCommand(str, Enum):
    STAY = "STAY"
    SWITCH_TO_DC_BPT = "SWITCH_TO_DC_BPT"
    SWITCH_TO_AC = "SWITCH_TO_AC"


AC, DC_BPT = "AC", "DC_BPT"


@dataclass(frozen=True)
class ModeState:
    mode: str = AC
    last_switch: Optional[datetime] = None   # None: plug-in mode, no dwell constraint yet

    def dwell_ok(self, t: datetime, dwell_min: float) -> bool:
        return self.last_switch is None or t - self.last_switch >= timedelta(minutes=dwell_min)


@dataclass
class Slot:
    end: datetime
    forecast: Optional[float]       # $/MWh, None if unknown
    lead_h: Optional[float]
    sell: Optional[float]           # adjusted $/MWh
    buy: Optional[float]

    @property
    def known(self) -> bool:
        return self.forecast is not None


@dataclass(frozen=True)
class PlanStep:
    interval_end: datetime
    action: Action
    sell_price: Optional[float]
    buy_price: Optional[float]
    soc_after: float


@dataclass(frozen=True)
class Override:
    rule: str                        # soc_floor | departure_guarantee | safe_mode | horizon_gap | dwell_blocks_export
    interval_end: datetime
    original: Action
    new: Action


@dataclass
class PlanResult:
    t: datetime
    preset: str
    current_action: Action
    mode_command: ModeCommand
    full_plan: List[PlanStep]
    safety_overrides_applied: List[Override]
    total_export_revenue: float
    total_charge_cost: float
    net_profit: float
    mode_switches_planned: int
    switch_downtime_s: float
    soc_plan_start: float            # SoC used for scheduling (mid, or lower bound if uncertain)


# --------------------------------------------------------------------------- #
# 1. Prices
# --------------------------------------------------------------------------- #

def adjust_prices(snapshot: Snapshot, preset: str, cfg: DecisionConfig,
                  lookup: ErrorLookup) -> List[Slot]:
    """One Slot per 5-min interval from t to departure; unknown intervals have no price."""
    if preset not in cfg.presets:
        raise ValueError(f"unknown preset {preset!r}; expected one of {list(cfg.presets)}")
    q = cfg.presets[preset]
    slots: Dict[datetime, Slot] = {}
    for p in snapshot.forecast:
        lead_h = p.lead_time.total_seconds() / 3600.0
        # q is None (raw forecast both sides) or (sell_q, buy_q); either side may be
        # None, meaning that side uses the raw forecast (e.g. sell-only adjustment)
        sell_q, buy_q = (None, None) if q is None else q
        sell = p.rrp if sell_q is None else p.rrp + lookup.get_error_adjustment(lead_h, p.rrp, sell_q)
        buy = p.rrp if buy_q is None else p.rrp + lookup.get_error_adjustment(lead_h, p.rrp, buy_q)
        slots[p.interval_end] = Slot(p.interval_end, p.rrp, lead_h, sell, buy)
    step = timedelta(minutes=cfg.slot_minutes)
    for u in snapshot.unknown:
        end = u.start + step
        while end <= u.end:
            slots.setdefault(end, Slot(end, None, None, None, None))
            end += step
    return [slots[k] for k in sorted(slots)]


# --------------------------------------------------------------------------- #
# 2. Greedy schedule
# --------------------------------------------------------------------------- #

def _export_value(s: Slot, cfg: DecisionConfig) -> float:
    e = cfg.export_power_kw * cfg.slot_h
    return (s.sell / 1000.0) * e - cfg.degradation_cost_per_kwh * e


def _charge_cost(buy: float, cfg: DecisionConfig) -> float:
    return (buy / 1000.0) * cfg.ac_charge_power_kw * cfg.slot_h / cfg.eta


def _margin(sell: float, buy: Optional[float], cfg: DecisionConfig) -> float:
    """$/MWh margin of exporting at ``sell`` and buying back at ``buy``.

    sell - buy / eta - degradation_cost_per_kwh * 1000. ``buy`` None means no
    known buy-back price; the export must then clear degradation alone.
    """
    return sell - (buy or 0.0) / cfg.eta - cfg.degradation_cost_per_kwh * 1000.0


def _pick_charges(slots: Sequence[Slot], exports: List[int], soc: float, cfg: DecisionConfig
                  ) -> Tuple[List[int], List[int], float]:
    """(target charge slots, export-funding charge slots, uncovered export kWh)."""
    e_x = cfg.export_power_kw * cfg.slot_h
    e_c = cfg.ac_charge_power_kw * cfg.slot_h
    cap = cfg.capacity_kwh
    surplus = max(0.0, soc - cfg.target_soc_pct) / 100.0 * cap
    need_target = max(0.0, cfg.target_soc_pct - soc) / 100.0 * cap
    need_export = max(0.0, len(exports) * e_x - surplus)

    ex = set(exports)
    pool = sorted((i for i in range(len(slots)) if i not in ex),
                  key=lambda i: (not slots[i].known, slots[i].buy if slots[i].known else 0.0, i))
    n_t = math.ceil(need_target / e_c - 1e-9)
    target = pool[:n_t]
    rest = pool[n_t:]
    if not exports or need_export <= 1e-9:
        return target, [], 0.0
    min_sell = min(slots[i].sell for i in exports)
    # only buy-back slots that leave every export above the minimum margin
    eligible = [i for i in rest if slots[i].known and _margin(min_sell, slots[i].buy, cfg) > cfg.minimum_margin_per_mwh]
    n_x = math.ceil(need_export / e_c - 1e-9)
    funding = eligible[:n_x]
    return target, funding, max(0.0, need_export - len(funding) * e_c)


def schedule(slots: Sequence[Slot], soc: float, cfg: DecisionConfig) -> List[Action]:
    e_x = cfg.export_power_kw * cfg.slot_h
    budget = max(0.0, 100.0 - cfg.min_soc_pct - cfg.export_buffer_pct) / 100.0 * cfg.capacity_kwh

    # a. exports: highest sell first, while there is exportable energy
    exports: List[int] = []
    cum = 0.0
    for i in sorted((i for i, s in enumerate(slots) if s.known and s.sell > 0),
                    key=lambda i: (-slots[i].sell, i)):
        if cum + e_x > budget + 1e-9:
            break
        exports.append(i)
        cum += e_x

    # b-c-e. charges for target + exports; drop the least profitable export until profitable
    while True:
        target, funding, uncovered = _pick_charges(slots, exports, soc, cfg)
        if exports and uncovered > 1e-9:
            exports.remove(min(exports, key=lambda i: (slots[i].sell, -i)))
            continue
        revenue = sum(_export_value(slots[i], cfg) for i in exports)
        cost = sum(_charge_cost(slots[i].buy, cfg) for i in funding)
        if exports and revenue <= cost:
            exports.remove(min(exports, key=lambda i: (_export_value(slots[i], cfg), -i)))
            continue
        # minimum margin: every export vs its buy-back price. Funded exports are
        # already filtered in _pick_charges; exports covered by SoC above target
        # are checked against the cheapest known buy-back still in the horizon.
        if exports and not funding:
            ex = set(exports)
            buys = [s.buy for i, s in enumerate(slots) if i not in ex and s.known]
            ref = min(buys) if buys else None
            low = min(exports, key=lambda i: (slots[i].sell, -i))
            if _margin(slots[low].sell, ref, cfg) <= cfg.minimum_margin_per_mwh:
                exports.remove(low)
                continue
        break

    # d. everything else idle
    actions = [Action.IDLE] * len(slots)
    for i in exports:
        actions[i] = Action.EXPORT
    for i in target + funding:
        actions[i] = Action.CHARGE
    return actions


# --------------------------------------------------------------------------- #
# 3. Safety overrides (identical for all presets)
# --------------------------------------------------------------------------- #

def apply_safety(snapshot: Snapshot, slots: Sequence[Slot], actions: List[Action],
                 soc_plan: float, cfg: DecisionConfig) -> Tuple[List[float], List[Override]]:
    """Mutates ``actions``; returns (soc_after per slot, overrides)."""
    overrides: List[Override] = []
    soc_low = snapshot.soc.lower            # all safety checks use the lower bound

    def flip(i, new, rule):
        overrides.append(Override(rule, slots[i].end, actions[i], new))
        actions[i] = new

    if snapshot.flags.safe_mode:
        for i, a in enumerate(actions):
            if a is Action.EXPORT:
                flip(i, Action.IDLE, "safe_mode")
        if slots and soc_low < cfg.target_soc_pct and actions[0] is not Action.CHARGE:
            flip(0, Action.CHARGE, "safe_mode")

    for i, s in enumerate(slots):
        if actions[i] is Action.EXPORT and not s.known:
            flip(i, Action.IDLE, "horizon_gap")

    if slots and snapshot.departure is not None and soc_low < cfg.target_soc_pct:
        hours = (cfg.target_soc_pct - soc_low) / 100.0 * cfg.capacity_kwh / cfg.ac_charge_power_kw
        latest_start = snapshot.departure - timedelta(hours=hours, minutes=cfg.departure_margin_minutes)
        if snapshot.t >= latest_start and actions[0] is not Action.CHARGE:
            flip(0, Action.CHARGE, "departure_guarantee")

    # chronological SoC walk: floor on the lower bound, no charging past 100 %
    d_x = cfg.export_power_kw * cfg.slot_h / cfg.capacity_kwh * 100.0
    d_c = cfg.ac_charge_power_kw * cfg.slot_h / cfg.capacity_kwh * 100.0
    offset = soc_plan - soc_low
    soc = soc_plan
    soc_after = []
    for i in range(len(slots)):
        if actions[i] is Action.EXPORT and soc - offset - d_x < cfg.min_soc_pct - 1e-9:
            flip(i, Action.IDLE, "soc_floor")
        if actions[i] is Action.EXPORT:
            soc -= d_x
        elif actions[i] is Action.CHARGE:
            if soc >= 100.0 - 1e-9:
                actions[i] = Action.IDLE         # full: not a safety override
            else:
                soc = min(100.0, soc + d_c)
        soc_after.append(soc)
    return soc_after, overrides


# --------------------------------------------------------------------------- #
# 4. Mode derivation
# --------------------------------------------------------------------------- #

def _mode_step(action: Action, upcoming: Sequence[Action], state: ModeState, t: datetime,
               cfg: DecisionConfig) -> Tuple[Action, ModeCommand, ModeState]:
    if action is Action.EXPORT:
        if state.mode == DC_BPT:
            return action, ModeCommand.STAY, state
        if state.dwell_ok(t, cfg.dwell_time_minutes):
            return action, ModeCommand.SWITCH_TO_DC_BPT, ModeState(DC_BPT, t)
        return Action.IDLE, ModeCommand.STAY, state          # cannot export in AC
    if state.mode == DC_BPT:
        n = int(cfg.dwell_time_minutes // cfg.slot_minutes)
        export_soon = any(a is Action.EXPORT for a in upcoming[:n])
        if not export_soon and state.dwell_ok(t, cfg.dwell_time_minutes):
            return action, ModeCommand.SWITCH_TO_AC, ModeState(AC, t)
    return action, ModeCommand.STAY, state


def _downtime(cmd: ModeCommand, cfg: DecisionConfig) -> float:
    return {ModeCommand.SWITCH_TO_DC_BPT: cfg.switch_ac_to_dc_s,
            ModeCommand.SWITCH_TO_AC: cfg.switch_dc_to_ac_s}.get(cmd, 0.0)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def plan(snapshot: Snapshot, state: ModeState = ModeState(), preset: Optional[str] = None,
         cfg: Optional[DecisionConfig] = None, lookup: Optional[ErrorLookup] = None) -> PlanResult:
    cfg = cfg or load_decision_config()
    preset = preset or snapshot.user.preset
    if cfg.presets.get(preset, "missing") is not None:
        lookup = lookup or default_lookup()

    slots = adjust_prices(snapshot, preset, cfg, lookup)
    soc_plan = snapshot.soc.lower if snapshot.flags.soc_uncertain else snapshot.soc.mid
    actions = schedule(slots, soc_plan, cfg)
    soc_after, overrides = apply_safety(snapshot, slots, actions, soc_plan, cfg)

    # mode: current slot is executed; the rest is simulated for the switch count
    t = snapshot.t
    step = timedelta(minutes=cfg.slot_minutes)
    st = state
    switches, downtime = 0, 0.0
    current_action, current_cmd = Action.IDLE, ModeCommand.STAY
    for i in range(len(slots)):
        new_a, cmd, st = _mode_step(actions[i], actions[i + 1:], st, t + i * step, cfg)
        if new_a is not actions[i]:
            if i == 0:
                overrides.append(Override("dwell_blocks_export", slots[0].end, actions[0], new_a))
            actions[i] = new_a
        if cmd is not ModeCommand.STAY:
            switches += 1
            downtime += _downtime(cmd, cfg)
        if i == 0:
            current_action, current_cmd = new_a, cmd

    # dwell may have turned planned exports into IDLE: recompute the SoC trajectory
    d_x = cfg.export_power_kw * cfg.slot_h / cfg.capacity_kwh * 100.0
    d_c = cfg.ac_charge_power_kw * cfg.slot_h / cfg.capacity_kwh * 100.0
    soc, soc_after = soc_plan, []
    for a in actions:
        soc = soc - d_x if a is Action.EXPORT else min(100.0, soc + d_c) if a is Action.CHARGE else soc
        soc_after.append(soc)

    revenue = sum(_export_value(s, cfg) for s, a in zip(slots, actions) if a is Action.EXPORT)
    known_buys = [s.buy for s in slots if s.known]
    fallback_buy = max(known_buys) if known_buys else 0.0     # unknown slots: costed pessimistically
    cost = sum(_charge_cost(s.buy if s.known else fallback_buy, cfg)
               for s, a in zip(slots, actions) if a is Action.CHARGE)
    return PlanResult(
        t=t, preset=preset, current_action=current_action, mode_command=current_cmd,
        full_plan=[PlanStep(s.end, a, s.sell, s.buy, x) for s, a, x in zip(slots, actions, soc_after)],
        safety_overrides_applied=overrides,
        total_export_revenue=revenue, total_charge_cost=cost, net_profit=revenue - cost,
        mode_switches_planned=switches, switch_downtime_s=downtime, soc_plan_start=soc_plan,
    )
