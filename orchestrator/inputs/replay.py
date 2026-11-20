"""Historical replay driver: step t over a date range and call get_snapshot(t).

    python -m orchestrator.inputs.replay --start 2026-01-01T04:05 --end 2026-01-01T12:00 \
        --out snapshots.jsonl

Defaults come from config/inputs.yaml (replay.start / replay.end).
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, List, Optional

from .config import InputConfig, UserSettings, load_config
from .snapshot import InputLayer, Snapshot
from .sources import ArchivePriceSource, ReplaySocSource, SocSource


def find_files(config: InputConfig, kind: str) -> List[Path]:
    glob = config.data[f"{kind}_glob"]
    return sorted(config.data_dir().glob(glob))


def archive_source(config: InputConfig) -> ArchivePriceSource:
    """Load every PD (and, once available, P5MIN / DISPATCHPRICE) file in the data dir."""
    pd_files = find_files(config, "pd")
    if not pd_files:
        raise FileNotFoundError(f"no PD files matching {config.data['pd_glob']} in {config.data_dir()}")
    return ArchivePriceSource.from_files(
        config.region,
        pd_files=pd_files,
        p5min_files=find_files(config, "p5min"),
        dispatch_files=find_files(config, "dispatch"),
    )


def replay(
    layer: InputLayer,
    start: datetime,
    end: datetime,
    on_snapshot: Optional[Callable[[Snapshot], None]] = None,
) -> Iterator[Snapshot]:
    """Yield get_snapshot(t) for t = start, start+cycle, ..., <= end."""
    step = timedelta(minutes=layer.config.cycle_minutes)
    t = start
    while t <= end:
        snap = layer.get_snapshot(t)
        if on_snapshot:
            on_snapshot(snap)
        yield snap
        t += step


def build_layer(
    config: Optional[InputConfig] = None,
    prices: Optional[ArchivePriceSource] = None,
    soc_source: Optional[SocSource] = None,
    user: Optional[UserSettings] = None,
    start: Optional[datetime] = None,
) -> InputLayer:
    config = config or load_config()
    return InputLayer(config, prices or archive_source(config), soc_source, user,
                      session_start=start or config.replay_start)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--start", type=datetime.fromisoformat)
    ap.add_argument("--end", type=datetime.fromisoformat)
    ap.add_argument("--departure", help='"HH:MM" or ISO datetime (overrides config)')
    ap.add_argument("--soc-csv", type=Path, help="SoC/meter replay CSV (time,mode,meter_energy_kwh,soc_reading)")
    ap.add_argument("--out", type=Path, help="write one JSON snapshot per line")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    start, end = a.start or cfg.replay_start, a.end or cfg.replay_end
    user = cfg.user.with_overrides(departure_time=a.departure) if a.departure else cfg.user
    soc = ReplaySocSource.from_csv(a.soc_csv) if a.soc_csv else None
    layer = build_layer(cfg, soc_source=soc, user=user, start=start)

    flags: Counter = Counter()
    n = 0
    out = open(a.out, "w") if a.out else None
    try:
        for snap in replay(layer, start, end):
            n += 1
            flags.update(snap.flags.active())
            if out:
                out.write(json.dumps(snap.to_dict()) + "\n")
    finally:
        if out:
            out.close()
    print(f"{n} snapshots {start} -> {end}, departure {layer.departure}")
    for k, v in sorted(flags.items()):
        print(f"  {k:22s} {v}")


if __name__ == "__main__":
    main()
