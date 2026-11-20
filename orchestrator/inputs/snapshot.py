"""get_snapshot(t): everything the decision logic needs at decision time t.

The input layer only REPORTS problems as flags; it never decides what to do.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .config import InputConfig, UserSettings
from .forecast import ForecastPoint, UnknownPeriod, assemble
from .soc import SocEstimate, estimate_soc
from .sources import ForecastRun, PriceSource, ReplaySocSource, SocSource


@dataclass(frozen=True)
class RunInfo:
    kind: str
    run_id: str
    published: datetime
    age_minutes: float
    used: bool          # False if available but not used (e.g. stale P5MIN)


@dataclass(frozen=True)
class StatusFlags:
    p5min_unavailable: bool = False   # no P5MIN run published <= t
    p5min_stale: bool = False         # latest P5MIN older than threshold -> PD used near-term
    pd_unavailable: bool = False      # no PD run published <= t
    pd_stale: bool = False            # latest PD older than threshold (still used)
    safe_mode: bool = False           # all price data older than threshold, or none at all
    horizon_gap: bool = False         # forecast ends before departure
    departure_passed: bool = False    # t >= departure; forecast returned to end of horizon
    missing_intervals: bool = False   # holes inside the forecast horizon
    invalid_prices: bool = False      # price values outside floor/cap were discarded
    soc_uncertain: bool = False       # SoC range wider than threshold
    soc_invalid_reading: bool = False  # SoC readings outside 0-100 were discarded

    def active(self) -> List[str]:
        return [k for k, v in asdict(self).items() if v]


@dataclass(frozen=True)
class Snapshot:
    t: datetime
    forecast: List[ForecastPoint]
    unknown: List[UnknownPeriod]
    runs: Dict[str, Optional[RunInfo]]       # "PD" / "P5MIN" -> latest available run
    latest_dispatch_rrp: Optional[float]     # $/MWh, latest DISPATCHPRICE with LASTCHANGED <= t
    latest_dispatch_interval: Optional[datetime]  # its interval end (= t on the grid; (t, t+5] is published ~9 s after t)
    discarded_prices: Dict[str, int]
    soc: SocEstimate
    user: UserSettings
    departure: Optional[datetime]
    flags: StatusFlags = field(default_factory=StatusFlags)

    def to_dict(self) -> Dict[str, Any]:
        def conv(x):
            if isinstance(x, datetime):
                return x.isoformat()
            if isinstance(x, timedelta):
                return x.total_seconds() / 60.0
            if isinstance(x, dict):
                return {k: conv(v) for k, v in x.items()}
            if isinstance(x, (list, tuple)):
                return [conv(v) for v in x]
            return x
        d = conv(asdict(self))
        d["active_flags"] = self.flags.active()
        return d


class InputLayer:
    def __init__(
        self,
        config: InputConfig,
        prices: PriceSource,
        soc_source: Optional[SocSource] = None,
        user: Optional[UserSettings] = None,
        session_start: Optional[datetime] = None,
    ):
        self.config = config
        self.prices = prices
        self.soc_source = soc_source or ReplaySocSource()
        self.user = user or config.user
        # "HH:MM" departures resolve to the next occurrence after the session start
        self.departure = self.user.resolve_departure(session_start or config.replay_start)

    def _check_t(self, t: datetime) -> None:
        cyc = self.config.cycle_minutes
        if t.second or t.microsecond or t.minute % cyc:
            raise ValueError(f"t={t} is not on the {cyc}-minute decision grid")

    def get_snapshot(self, t: datetime) -> Snapshot:
        self._check_t(t)
        th = self.config.thresholds
        age = lambda run: (t - run.published).total_seconds() / 60.0  # noqa: E731

        pd_run: Optional[ForecastRun] = self.prices.latest_run("PD", t)
        p5_run: Optional[ForecastRun] = self.prices.latest_run("P5MIN", t)
        dispatch = self.prices.latest_dispatch(t)

        p5_stale = p5_run is not None and age(p5_run) > th.p5min_max_age_minutes
        p5_used = p5_run if (p5_run is not None and not p5_stale) else None
        dep_passed = self.departure is not None and t >= self.departure
        fc = assemble(t, pd_run, p5_used, th, None if dep_passed else self.departure)

        ages = [age(r) for r in (pd_run, p5_run) if r is not None]
        if dispatch is not None:
            ages.append((t - dispatch["published"].to_pydatetime()).total_seconds() / 60.0)
        safe = not ages or min(ages) > th.safe_mode_max_age_minutes

        soc = estimate_soc(self.soc_source.samples_until(t), self.user, self.config.soc, th)

        def info(run, used):
            return None if run is None else RunInfo(run.kind, run.run_id, run.published, age(run), used)

        flags = StatusFlags(
            p5min_unavailable=p5_run is None,
            p5min_stale=p5_stale,
            pd_unavailable=pd_run is None,
            pd_stale=pd_run is not None and age(pd_run) > th.pd_max_age_minutes,
            safe_mode=safe,
            horizon_gap=any(u.reason == "beyond_horizon" for u in fc.unknown),
            departure_passed=dep_passed,
            missing_intervals=any(u.reason == "missing" for u in fc.unknown),
            invalid_prices=bool(fc.discarded),
            soc_uncertain=soc.uncertain,
            soc_invalid_reading=soc.invalid_readings > 0,
        )
        return Snapshot(
            t=t,
            forecast=fc.points,
            unknown=fc.unknown,
            runs={"PD": info(pd_run, pd_run is not None),
                  "P5MIN": info(p5_run, fc.p5min_used)},
            latest_dispatch_rrp=None if dispatch is None else float(dispatch["rrp"]),
            latest_dispatch_interval=None if dispatch is None else dispatch["target"].to_pydatetime(),
            discarded_prices=fc.discarded,
            soc=soc,
            user=self.user,
            departure=self.departure,
            flags=flags,
        )
