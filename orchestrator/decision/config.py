"""Load config/decision.yaml."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "decision.yaml"


@dataclass(frozen=True)
class DecisionConfig:
    capacity_kwh: float
    min_soc_pct: float
    target_soc_pct: float
    export_power_kw: float
    ac_charge_power_kw: float
    dc_charge_power_kw: float
    eta: float
    degradation_cost_per_kwh: float
    slot_minutes: int
    export_buffer_pct: float
    minimum_margin_per_mwh: float
    departure_margin_minutes: float
    dwell_time_minutes: float
    switch_ac_to_dc_s: float
    switch_dc_to_ac_s: float
    presets: Mapping[str, Optional[Tuple[Optional[str], Optional[str]]]]  # name -> (sell_q, buy_q) or None; a None side = raw forecast
    session_start: str
    departure: str
    initial_soc_pct: float

    @property
    def slot_h(self) -> float:
        return self.slot_minutes / 60.0


def _merge(base: dict, over: Mapping) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, Mapping) and isinstance(out.get(k), dict) else v
    return out


def load_decision_config(path=None, overrides: Optional[Mapping] = None) -> DecisionConfig:
    raw = yaml.safe_load(Path(path or DEFAULT_PATH).read_text())
    if overrides:
        raw = _merge(raw, overrides)
    b, p, m, s = raw["battery"], raw["planner"], raw["mode"], raw["session"]
    presets: Dict[str, Optional[Tuple[Optional[str], Optional[str]]]] = {
        name: None if q is None else (q.get("sell"), q.get("buy")) for name, q in raw["presets"].items()
    }
    return DecisionConfig(
        capacity_kwh=b["capacity_kwh"], min_soc_pct=b["min_soc_pct"], target_soc_pct=b["target_soc_pct"],
        export_power_kw=b["export_power_kw"], ac_charge_power_kw=b["ac_charge_power_kw"],
        dc_charge_power_kw=b["dc_charge_power_kw"], eta=b["efficiency_eta"],
        degradation_cost_per_kwh=b["degradation_cost_per_kwh"],
        slot_minutes=p["slot_minutes"], export_buffer_pct=p["export_buffer_pct"],
        minimum_margin_per_mwh=p["minimum_margin_per_mwh"],
        departure_margin_minutes=p["departure_margin_minutes"],
        dwell_time_minutes=m["dwell_time_minutes"], switch_ac_to_dc_s=m["switch_ac_to_dc_s"],
        switch_dc_to_ac_s=m["switch_dc_to_ac_s"], presets=presets,
        session_start=s["session_start"], departure=s["departure"],
        initial_soc_pct=s["initial_soc_pct"],
    )
