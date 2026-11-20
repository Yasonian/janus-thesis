"""EV responder: SiL stand-in for what a real car does by itself.

    python -m orchestrator.live.ev_responder

A real EV re-negotiates on its own when the V2G-Key switches its power path.
In the SiL each context has its own simulated car (EvManager) that only acts
when told, so this process plays the car:

* at start-up, once the initial context's EVSE reports ``Unplugged`` (ready),
  it starts the initial session (plug-in);
* on every successful ``switch_complete`` from a V2G-Key it sends
  ``leave_program`` (unplug) to the car of the context that was LEFT — its
  wallbox no longer sees the car once the source is switched — then, after
  ``delay_s``, the session program to the car of the NEW context.

The context that was left is the one whose V2G-Key published switch_complete
(each key pulses only its own context's Control Pilot); the key's from/to
labels are not used because they are per-instance and not kept in sync.

Every action is published on ``topics.responder`` with wall timestamps, so the
orchestrator's switch log can subtract the responder delay.

With ``ev_responder.plant.enabled`` it also runs the decision-time battery
model (live/plant.py): every ``janus/orchestrator/decision`` is applied for one
slot at the actual dispatch price; the model's meter is published on
``topics.plant_meter`` and its SoC is pushed into the simulated car of the
active context (``set_soc`` via EvManager's modify_charging_session, only while
that session is Charging so its start-up sequence is never interrupted; new
sessions start with ``set_soc`` as their first command). The car then reports
that SoC over ISO 15118-20 (present_soc) in DC-BPT.

This is SiL plumbing, not orchestrator logic: the orchestrator only ever sends
charger-side commands (set_power_path).
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .common import (MODES, api_session_info_topic, cmd_topic, connect, everest_cmd, load_live_config,
                     log, other_mode, setup_logging, unwrap_var, var_topic)

ORIGIN = "janus_ev_responder"


class EvResponder:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rc = cfg["ev_responder"]
        self.ctx = cfg["contexts"]
        self.key_to_mode = {self.ctx[m]["v2g_key"]: m for m in MODES}
        self.plugged_in = False
        self.lock = threading.Lock()
        self.client = None
        self.states = {m: None for m in MODES}
        self.plant = None
        self.log_dir = None
        pc = self.rc.get("plant", {})
        if pc.get("enabled"):
            from orchestrator.decision.config import load_decision_config
            from orchestrator.decision.scenario_runner import load_data
            from .plant import BatteryPlant
            dcfg = load_decision_config()
            self.plant = BatteryPlant(dcfg, dcfg.initial_soc_pct)
            self.dispatch = load_data(pc.get("data", "real")).dispatch
            self.slot = timedelta(minutes=dcfg.slot_minutes)

    def subscriptions(self):
        subs = [var_topic(self.ctx[m]["v2g_key"], "v2g_key", "switch_complete") for m in MODES]
        subs += [api_session_info_topic(self.ctx[m]["evse_manager"]) for m in MODES]
        if self.plant is not None:
            subs.append(self.cfg["topics"]["decision"])
        return subs

    def program(self, mode: str) -> str:
        prog = self.rc["programs"][mode]
        if self.plant is not None:
            prog = f"set_soc {self.plant.soc:.3f};" + prog
        return prog

    # ------------------------------------------------------------------ plant

    def on_decision(self, d: dict):
        cmd = d.get("command_sent")
        mode = cmd["target"] if cmd else d["system_mode"]
        t = datetime.fromisoformat(d["sim_time"])
        slot_end = t + self.slot
        price = float(self.dispatch[slot_end])
        step = self.plant.apply(d["cycle"], d["sim_time"], slot_end.isoformat(), d["action"], mode, price)
        topics = self.cfg["topics"]
        self.client.publish(topics["plant_meter"], json.dumps(
            {"sim_time": slot_end.isoformat(), "import_wh": step.meter_import_wh,
             "export_wh": step.meter_export_wh, "soc": step.soc_after}), qos=1)
        self.client.publish(topics["plant_step"], json.dumps(asdict(step)), qos=1)
        if self.log_dir:
            with open(self.log_dir / "plant.jsonl", "a") as fh:
                fh.write(json.dumps(asdict(step)) + "\n")
        # push SoC into the active context's car, only while its session is running
        if self.states.get(mode) in ("Charging", "ChargingPausedEV", "ChargingPausedEVSE"):
            prog = f"set_soc {step.soc_after:.3f};" + self.rc["soc_push_tail"][mode]
            cid = self.rc["connector_ids"][mode]
            self.client.publish(f"everest_external/nodered/{cid}/carsim/cmd/modify_charging_session", prog, qos=1)
        log.info("plant cycle %d %s: %s in %s -> %s, %.2f kWh, price %.2f, cash %+.4f, SoC %.2f -> %.2f",
                 d["cycle"], t.strftime("%H:%M"), d["action"], mode, step.executed, step.kwh, price,
                 step.cashflow, step.soc_before, step.soc_after)

    def start(self):
        self.client = connect(ORIGIN, self.cfg, self.on_message, self.subscriptions())

    # ------------------------------------------------------------------ #

    def send_program(self, mode: str, program: str, reason: str, extra=None) -> float:
        ev = self.ctx[mode]["ev_manager"]
        topic = cmd_topic(ev, "main", "execute_charging_session")
        self.client.publish(topic, everest_cmd({"value": program}, ORIGIN), qos=1)
        sent = time.time()
        event = {"action": reason, "mode": mode, "ev_manager": ev, "program": program, "t_sent": sent,
                 **(extra or {})}
        self.client.publish(self.cfg["topics"]["responder"], json.dumps(event), qos=1)
        log.info("%s -> %s: %s", reason, ev, program)
        return sent

    def on_message(self, topic: str, payload: bytes, t_rx: float):
        if topic == self.cfg["topics"]["decision"]:
            with self.lock:
                self.on_decision(json.loads(payload))
            return
        if topic.startswith("everest_api/"):
            state = json.loads(payload).get("state")
            em = topic.split("/")[1]
            mode = next(m for m in MODES if self.ctx[m]["evse_manager"] == em)
            with self.lock:
                self.states[mode] = state
                if self.plugged_in or state != "Unplugged" or mode != self.rc["initial_mode"]:
                    return
                self.plugged_in = True
            m = self.rc["initial_mode"]
            self.send_program(m, self.program(m), "plug_in")
            return

        key = topic.split("/")[2]
        left = self.key_to_mode.get(key)
        evt = unwrap_var(payload)
        if left is None or not evt.get("success", False):
            log.warning("ignoring switch_complete on %s: %s", topic, evt)
            return
        new = other_mode(left)
        threading.Thread(target=self._respond, args=(left, new, evt, t_rx), daemon=True).start()

    def modify_program(self, mode: str, program: str, reason: str, extra=None) -> float:
        """Replace the car's running program (EvManager modify_charging_session).

        execute_charging_session is rejected while a program is still running
        ("already running"), and the AC program never ends by design, so the
        leave/unplug must go through modify.
        """
        cid = self.rc["connector_ids"][mode]
        self.client.publish(f"everest_external/nodered/{cid}/carsim/cmd/modify_charging_session", program, qos=1)
        sent = time.time()
        event = {"action": reason, "mode": mode, "connector_id": cid, "program": program, "t_sent": sent,
                 "via": "modify_charging_session", **(extra or {})}
        self.client.publish(self.cfg["topics"]["responder"], json.dumps(event), qos=1)
        log.info("%s -> connector %s (modify): %s", reason, cid, program)
        return sent

    def _respond(self, left: str, new: str, evt: dict, t_rx: float):
        self.modify_program(left, self.rc["leave_program"], "leave",
                            {"t_switch_complete_rx": t_rx, "key_event": evt})
        delay = float(self.rc.get("delay_s", 0.0))
        if delay > 0:
            time.sleep(delay)
        with self.lock:
            prog = self.program(new)
        t = self.send_program(new, prog, "start_session",
                              {"t_switch_complete_rx": t_rx, "configured_delay_s": delay})
        log.info("responder delay %.3f s (switch_complete rx -> session cmd)", t - t_rx)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--log-dir")
    ap.add_argument("--plant-data", help="override ev_responder.plant.data")
    a = ap.parse_args(argv)
    setup_logging("ev_responder")
    cfg = load_live_config(a.config)
    if a.plant_data:
        cfg["ev_responder"].setdefault("plant", {})["data"] = a.plant_data
    r = EvResponder(cfg)
    r.log_dir = Path(a.log_dir) if a.log_dir else None
    r.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
