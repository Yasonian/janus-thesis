"""Load config/inputs.yaml into typed settings, with per-run overrides."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "inputs.yaml"
PRESETS = ("A", "B", "C")


@dataclass(frozen=True)
class Thresholds:
    p5min_window_minutes: float
    p5min_max_age_minutes: float
    pd_max_age_minutes: float
    safe_mode_max_age_minutes: float
    price_floor: float
    price_cap: float
    soc_min: float
    soc_max: float


@dataclass(frozen=True)
class SocParams:
    direct_tolerance_pct: float
    user_entry_tolerance_pct: float
    ac_charge_efficiency: float
    ac_uncertainty_fraction: float
    uncertain_width_pct: float


@dataclass(frozen=True)
class UserSettings:
    departure_time: Optional[str]     # "HH:MM" or ISO datetime, see resolve_departure()
    target_soc: float
    min_soc: float
    battery_capacity_kwh: float
    preset: str
    user_soc: Optional[float] = None

    def __post_init__(self):
        if self.preset not in PRESETS:
            raise ValueError(f"preset must be one of {PRESETS}, got {self.preset!r}")
        if self.battery_capacity_kwh <= 0:
            raise ValueError("battery_capacity_kwh must be > 0")

    def with_overrides(self, **kw) -> "UserSettings":
        return replace(self, **kw)

    def resolve_departure(self, reference: datetime) -> Optional[datetime]:
        """Absolute departure time. "HH:MM" means the next occurrence after ``reference``."""
        if self.departure_time is None:
            return None
        s = str(self.departure_time)
        if len(s) <= 5 and ":" in s:
            hh, mm = (int(x) for x in s.split(":"))
            dep = datetime.combine(reference.date(), time(hh, mm))
            return dep if dep > reference else dep + timedelta(days=1)
        return datetime.fromisoformat(s)


@dataclass(frozen=True)
class InputConfig:
    region: str
    cycle_minutes: int
    data: Mapping[str, Any]
    thresholds: Thresholds
    soc: SocParams
    user: UserSettings
    replay_start: datetime
    replay_end: datetime
    raw: Mapping[str, Any] = field(repr=False, default_factory=dict)

    def data_dir(self) -> Path:
        env = os.environ.get(self.data.get("dir_env", ""), "")
        return Path(env or self.data["default_dir"]).expanduser()


def _deep_merge(base: dict, over: Mapping) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | str | None = None, overrides: Optional[Mapping] = None) -> InputConfig:
    """Load the YAML config. ``overrides`` is a nested dict merged on top of it."""
    with open(path or DEFAULT_CONFIG_PATH) as fh:
        raw = yaml.safe_load(fh)
    if overrides:
        raw = _deep_merge(raw, overrides)
    h, s, v = raw["horizon"], raw["staleness"], raw["validation"]
    return InputConfig(
        region=raw["region"],
        cycle_minutes=int(raw["cycle_minutes"]),
        data=raw["data"],
        thresholds=Thresholds(
            p5min_window_minutes=h["p5min_window_minutes"],
            p5min_max_age_minutes=s["p5min_max_age_minutes"],
            pd_max_age_minutes=s["pd_max_age_minutes"],
            safe_mode_max_age_minutes=s["safe_mode_max_age_minutes"],
            price_floor=v["price_floor"],
            price_cap=v["price_cap"],
            soc_min=v["soc_min"],
            soc_max=v["soc_max"],
        ),
        soc=SocParams(**raw["soc"]),
        user=UserSettings(**raw["user"]),
        replay_start=datetime.fromisoformat(str(raw["replay"]["start"])),
        replay_end=datetime.fromisoformat(str(raw["replay"]["end"])),
        raw=raw,
    )
