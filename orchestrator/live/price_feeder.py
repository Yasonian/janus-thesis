"""Price feeder: replays AEMO forecast runs onto MQTT, driven by the decision clock.

    python -m orchestrator.live.price_feeder [--data synthetic_spike|real]

Subscribes to ``topics.clock`` (published by the orchestrator) and publishes
every PD run whose LASTCHANGED <= clock time that has not been sent yet, one
message per run on ``topics.prices`` (format: inputs.sources.LivePriceSource).
A live AEMO/NEMweb poller would publish the same messages as runs appear.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from orchestrator.decision.scenario_runner import load_data

from .common import connect, load_live_config, log, setup_logging

ORIGIN = "janus_price_feeder"


def run_messages(frame: pd.DataFrame, kind: str = "PD", region: str = "NSW1"):
    """[(published, message)] sorted by published."""
    out = []
    for run_id, g in frame.groupby("run_id"):
        pub = pd.Timestamp(g["published"].iloc[0]).to_pydatetime()
        pts = [{"target": pd.Timestamp(t).isoformat(), "rrp": float(r)}
               for t, r in zip(g["target"], g["rrp"])]
        out.append((pub, {"kind": kind, "run_id": str(run_id), "published": pub.isoformat(),
                          "region": region, "points": pts}))
    return sorted(out, key=lambda x: x[0])


class PriceFeeder:
    def __init__(self, cfg: dict, data: str, not_before: Optional[datetime] = None):
        """not_before: skip runs published before this (only the latest such run is kept)."""
        self.cfg = cfg
        d = load_data(data)
        frames = [r.frame for r in d.prices.runs("PD")]
        q = run_messages(pd.concat(frames, ignore_index=True)) if frames else []
        if not_before is not None:
            older = [x for x in q if x[0] < not_before]
            q = older[-1:] + [x for x in q if x[0] >= not_before]
        self.queue = q
        self.sent = 0
        self.lock = threading.Lock()
        self.client = None
        log.info("loaded %d PD runs (%s)", len(self.queue), data)

    def start(self):
        self.client = connect(ORIGIN, self.cfg, self.on_message, [self.cfg["topics"]["clock"]])

    def on_message(self, topic: str, payload: bytes, t_rx: float):
        now = datetime.fromisoformat(json.loads(payload)["sim_time"])
        with self.lock:
            while self.sent < len(self.queue) and self.queue[self.sent][0] <= now:
                pub, msg = self.queue[self.sent]
                self.client.publish(self.cfg["topics"]["prices"], json.dumps(msg), qos=1)
                self.sent += 1
                log.info("published PD run %s (LASTCHANGED %s) at clock %s", msg["run_id"], pub, now)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--data")
    ap.add_argument("--sim-start", type=datetime.fromisoformat)
    a = ap.parse_args(argv)
    setup_logging("price_feeder")
    cfg = load_live_config(a.config)
    start = a.sim_start or datetime.fromisoformat(cfg["clock"]["sim_start"])
    f = PriceFeeder(cfg, a.data or cfg["orchestrator"]["data"], not_before=start - timedelta(hours=2))
    f.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
