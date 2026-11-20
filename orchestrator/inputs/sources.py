"""Data sources feeding the input layer.

Two kinds of source, each with a historical (replay) implementation and a
placeholder for the future live/MQTT implementation:

* PriceSource  -- AEMO forecast runs (PD, P5MIN) and dispatch prices
* SocSource    -- SoC readings, charging mode and energy-meter totals

Every query takes the decision time ``t`` and must return only what was
available at ``t`` (published <= t). This is the no-look-ahead boundary.
"""
from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence

import pandas as pd

from .aemo_csv import CANONICAL_COLUMNS, DISPATCH_SPEC, P5MIN_SPEC, PD_SPEC, load_tables


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ForecastRun:
    kind: str               # "PD" or "P5MIN"
    run_id: str
    published: datetime     # LASTCHANGED
    frame: pd.DataFrame     # canonical columns, one row per target interval


class PriceSource(Protocol):
    def latest_run(self, kind: str, t: datetime) -> Optional[ForecastRun]:
        """Latest run of ``kind`` with published <= t, or None."""

    def latest_dispatch(self, t: datetime) -> Optional[pd.Series]:
        """Latest dispatch price row with published <= t, or None."""


class ArchivePriceSource:
    """Historical PriceSource over canonical frames loaded from MMSDM archives."""

    def __init__(
        self,
        pd_frame: Optional[pd.DataFrame] = None,
        p5min_frame: Optional[pd.DataFrame] = None,
        dispatch_frame: Optional[pd.DataFrame] = None,
    ):
        self._runs: Dict[str, List[ForecastRun]] = {}
        self._published: Dict[str, List[datetime]] = {}
        for kind, frame in (("PD", pd_frame), ("P5MIN", p5min_frame)):
            runs = []
            if frame is not None and len(frame):
                for run_id, g in frame.groupby("run_id", sort=False):
                    pubs = g["published"].unique()
                    if len(pubs) != 1:
                        raise ValueError(f"{kind} run {run_id} has {len(pubs)} LASTCHANGED values")
                    runs.append(
                        ForecastRun(kind, str(run_id), pd.Timestamp(pubs[0]).to_pydatetime(),
                                    g.sort_values("target").reset_index(drop=True))
                    )
            runs.sort(key=lambda r: (r.published, r.run_id))
            self._runs[kind] = runs
            self._published[kind] = [r.published for r in runs]
        self._dispatch = (
            dispatch_frame.sort_values("published").reset_index(drop=True)
            if dispatch_frame is not None and len(dispatch_frame) else None
        )

    @classmethod
    def from_files(cls, region: str, pd_files: Sequence[Path] = (),
                   p5min_files: Sequence[Path] = (), dispatch_files: Sequence[Path] = ()):
        return cls(
            load_tables(pd_files, PD_SPEC, region) if pd_files else None,
            load_tables(p5min_files, P5MIN_SPEC, region) if p5min_files else None,
            load_tables(dispatch_files, DISPATCH_SPEC, region) if dispatch_files else None,
        )

    def runs(self, kind: str) -> List[ForecastRun]:
        return list(self._runs.get(kind, []))

    def latest_run(self, kind: str, t: datetime) -> Optional[ForecastRun]:
        pubs = self._published.get(kind, [])
        i = bisect.bisect_right(pubs, t)   # runs[:i] all have published <= t
        return self._runs[kind][i - 1] if i else None

    def latest_dispatch(self, t: datetime) -> Optional[pd.Series]:
        if self._dispatch is None:
            return None
        avail = self._dispatch[self._dispatch["published"] <= t]
        if avail.empty:
            return None
        return avail.loc[avail["target"].idxmax()]

    def without_runs(self, kind: str, run_ids: Sequence[str]) -> "ArchivePriceSource":
        """Copy with some runs removed (used to simulate missing publications)."""
        drop = set(run_ids)
        frames = {k: _concat([r.frame for r in rs if not (k == kind and r.run_id in drop)])
                  for k, rs in self._runs.items()}
        return ArchivePriceSource(frames.get("PD"), frames.get("P5MIN"), self._dispatch)


def _concat(frames: List[pd.DataFrame]) -> Optional[pd.DataFrame]:
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=CANONICAL_COLUMNS)


class LivePriceSource:
    """PriceSource fed at runtime (Week 4: from MQTT ``janus/prices/forecast``).

    Runs are buffered as they arrive. ``latest_run(kind, t)`` applies the same
    availability rule as ArchivePriceSource: only runs with published <= t, and
    the latest of those. A run that arrives early (e.g. a replay feeder running
    ahead of the clock) is therefore never used before its LASTCHANGED.

    Message format accepted by ``ingest_message``::

        {"kind": "PD", "run_id": "2026011128", "published": "2026-01-11T17:33:02",
         "region": "NSW1", "points": [{"target": "2026-01-11T18:00:00", "rrp": 28.9}, ...]}
        {"kind": "DISPATCH", "published": "...", "target": "...", "rrp": 57.0}
    """

    def __init__(self, region: str = "NSW1"):
        self.region = region
        self._runs: Dict[str, Dict[str, ForecastRun]] = {}
        self._dispatch: List[tuple] = []          # (published, target, rrp), sorted

    def ingest_run(self, kind: str, run_id: str, published: datetime,
                   points: Sequence[tuple]) -> ForecastRun:
        """points: (target, rrp) pairs. Re-sending a run_id replaces it."""
        frame = pd.DataFrame({
            "run_id": str(run_id), "published": pd.Timestamp(published),
            "target": pd.to_datetime([t for t, _ in points]),
            "region": self.region, "rrp": [float(r) for _, r in points],
        }).sort_values("target").reset_index(drop=True)
        run = ForecastRun(kind, str(run_id), published, frame)
        self._runs.setdefault(kind, {})[run.run_id] = run
        return run

    def ingest_dispatch(self, published: datetime, target: datetime, rrp: float) -> None:
        bisect.insort(self._dispatch, (published, target, float(rrp)))

    def ingest_message(self, msg: dict) -> None:
        kind = msg["kind"]
        published = datetime.fromisoformat(msg["published"])
        if kind == "DISPATCH":
            self.ingest_dispatch(published, datetime.fromisoformat(msg["target"]), msg["rrp"])
            return
        if msg.get("region", self.region) != self.region:
            return
        self.ingest_run(kind, msg["run_id"], published,
                        [(datetime.fromisoformat(p["target"]), p["rrp"]) for p in msg["points"]])

    def n_runs(self, kind: str) -> int:
        return len(self._runs.get(kind, {}))

    def latest_run(self, kind: str, t: datetime) -> Optional[ForecastRun]:
        avail = [r for r in self._runs.get(kind, {}).values() if r.published <= t]
        return max(avail, key=lambda r: (r.published, r.run_id)) if avail else None

    def latest_dispatch(self, t: datetime) -> Optional[pd.Series]:
        avail = [d for d in self._dispatch if d[0] <= t]
        if not avail:
            return None
        pub, target, rrp = max(avail, key=lambda d: d[1])
        return pd.Series({"published": pd.Timestamp(pub), "target": pd.Timestamp(target), "rrp": rrp})


# --------------------------------------------------------------------------- #
# SoC / energy meter
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SocSample:
    time: datetime
    mode: str                          # "AC" or "DC" (DC-BPT)
    meter_energy_kwh: float            # cumulative, signed: + into the EV, - out of it
    soc_reading: Optional[float] = None  # direct SoC from the EV (DC-BPT only), %


class SocSource(Protocol):
    def samples_until(self, t: datetime) -> List[SocSample]:
        """All samples with time <= t, oldest first."""


class ReplaySocSource:
    """Replay/stub SocSource over a fixed list of samples."""

    def __init__(self, samples: Sequence[SocSample] = ()):
        self._samples = sorted(samples, key=lambda s: s.time)
        self._times = [s.time for s in self._samples]

    def samples_until(self, t: datetime) -> List[SocSample]:
        return self._samples[: bisect.bisect_right(self._times, t)]

    @classmethod
    def from_csv(cls, path: Path | str) -> "ReplaySocSource":
        """CSV columns: time (ISO, NEM time), mode, meter_energy_kwh, soc_reading (may be blank)."""
        samples = []
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                samples.append(SocSample(
                    time=datetime.fromisoformat(r["time"]),
                    mode=r["mode"].strip().upper(),
                    meter_energy_kwh=float(r["meter_energy_kwh"]),
                    soc_reading=float(r["soc_reading"]) if r.get("soc_reading", "").strip() else None,
                ))
        return cls(samples)


class LiveSocSource:
    """SocSource fed at runtime (Week 4: from EVerest MQTT topics).

    Inputs, as observed in the JANUS 2-context SiL (sil-captures, 2026-10-18):

    * SoC reading, DC-BPT only: ``dc_hlc/impl/charger/var/display_parameters``
      field ``present_soc`` -- sent by the EV in every ISO 15118-20
      DC_ChargeLoopReq; whole percent (PyEvJosev floors it). No SoC exists on
      the AC side.
    * Energy: ``{ac,dc}_evse_manager/impl/evse/var/powermeter``
      ``energy_Wh_import.total`` / ``energy_Wh_export.total``. The two contexts
      have separate meters; their increments are summed into one signed
      cumulative figure. A meter total that goes down (reset) contributes 0.
    * Mode: set by the caller (the orchestrator owns the system mode).

    ``clock`` returns the decision-time clock used to stamp samples, so that
    ``samples_until(t)`` works on the same time axis as get_snapshot(t).
    """

    def __init__(self, clock: Callable[[], datetime], mode: str = "AC"):
        self._clock = clock
        self._samples: List[SocSample] = []
        self._mode = mode
        self._last_totals: Dict[str, tuple] = {}      # context -> (import_wh, export_wh)
        self._meter_kwh = 0.0

    @property
    def meter_kwh(self) -> float:
        return self._meter_kwh

    def set_mode(self, mode: str) -> None:
        self._mode = mode

    def _append(self, reading: Optional[float] = None) -> None:
        mode = "DC" if self._mode == "DC_BPT" else "AC"
        self._samples.append(SocSample(self._clock(), mode, self._meter_kwh, reading))

    def set_meter_baseline(self, context: str, import_wh: float = 0.0, export_wh: float = 0.0) -> None:
        """Declare a meter's starting totals (for meters known to start at zero).

        Without this the first reading of a meter is only a baseline, which is right
        for EVerest powermeters (arbitrary totals) but loses the first increment of a
        meter that starts at 0 (e.g. the battery-model meter).
        """
        self._last_totals[context] = (import_wh, export_wh)
        if not self._samples:
            self._append()

    def ingest_meter(self, context: str, import_wh: float, export_wh: float) -> None:
        prev = self._last_totals.get(context)
        self._last_totals[context] = (import_wh, export_wh)
        if prev is None:
            if not self._samples:
                self._append()
            return
        d_imp = import_wh - prev[0] if import_wh >= prev[0] else 0.0
        d_exp = export_wh - prev[1] if export_wh >= prev[1] else 0.0
        delta = (d_imp - d_exp) / 1000.0
        if delta != 0.0:
            self._meter_kwh += delta
            self._append()

    def ingest_soc_reading(self, present_soc: float) -> None:
        self._append(float(present_soc))

    def samples_until(self, t: datetime) -> List[SocSample]:
        return [s for s in self._samples if s.time <= t]
