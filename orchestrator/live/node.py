"""JANUS orchestrator node: the Weeks 2-3 decision logic, driven over MQTT.

    python -m orchestrator.live.node [--preset Naive] [--duration 240]

Inputs (subscribed)
  everest_api/{ac,dc}_evse_manager/var/session_info      session state (guard, timing, clock start)
  everest/modules/{ac,dc}_evse_manager/.../session_event  timing breakdown
  everest/modules/{ac,dc}_evse_manager/.../powermeter     energy -> LiveSocSource
  everest/modules/dc_hlc/.../display_parameters           present_soc (DC-BPT only) -> LiveSocSource
  everest/modules/{ac,dc}_v2g_key/.../var/*               switch_in_progress / switch_complete / cp_state / current_mode
  everest/modules/{ac,dc}_ev_manager/.../execute_charging_session   (observed: EV responder actions)
  janus/prices/forecast                                   -> LivePriceSource
  janus/user/settings  (retained, optional)               departure_time, target_soc, min_soc, user_soc, preset

Outputs (published)
  everest/modules/<key>/impl/v2g_key/cmd/set_power_path   the ONLY command to EVerest (charger side)
  janus/clock                  retained  decision-time clock (drives the price feeder)
  janus/orchestrator/state     retained  mode, SoC estimate, next decision time, guard status
  janus/orchestrator/decision            one message per decision cycle
  janus/orchestrator/switch_log          one record per switch (timing breakdown)

System mode is owned here: it starts at ``orchestrator.initial_mode`` and flips
when the V2G-Key of the context being left reports a successful switch_complete.
The per-instance V2G-Key current_mode labels are tracked only to pick a command
label the key will accept (see switching.command_label).
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from orchestrator.decision.config import load_decision_config
from orchestrator.decision.planner import ModeCommand, ModeState, plan
from orchestrator.inputs.config import load_config
from orchestrator.inputs.error_lookup import default_lookup
from orchestrator.inputs.snapshot import InputLayer
from orchestrator.inputs.soc import estimate_soc
from orchestrator.inputs.sources import LivePriceSource, LiveSocSource

from .common import (MODES, ORCH_DIR, SimClock, api_session_info_topic, cmd_topic, connect, everest_cmd,
                     load_live_config, log, other_mode, setup_logging, unwrap_var, var_topic)
from .switching import ContextStatus, SwitchRecord, check_guard, command_label

ORIGIN = "janus_orchestrator"
RESPONDER_ORIGIN = "janus_ev_responder"


class OrchestratorNode:
    def __init__(self, cfg: dict, preset: Optional[str] = None, log_dir: Optional[Path] = None,
                 sim_start: Optional[datetime] = None):
        self.cfg = cfg
        self.ctx = cfg["contexts"]
        self.topics = cfg["topics"]
        self.guard_cfg = cfg["guard"]
        self.preset = preset or cfg["orchestrator"]["preset"]
        self.dcfg = load_decision_config()
        self.icfg = load_config()
        self.lookup = default_lookup()
        self.lock = threading.RLock()

        c = cfg["clock"]
        self.clock = SimClock(sim_start or datetime.fromisoformat(c["sim_start"]), float(c["wall_seconds_per_cycle"]))
        self.meter_source = cfg["orchestrator"].get("meter_source", "everest")
        self.departure: Optional[datetime] = None
        self.system_mode: str = cfg["orchestrator"]["initial_mode"]
        self.last_switch_sim: Optional[datetime] = None
        self.prices = LivePriceSource(self.icfg.region)
        self.soc = LiveSocSource(clock=lambda: self.clock.now(), mode=self.system_mode)

        self.status: Dict[str, ContextStatus] = {m: ContextStatus() for m in MODES}
        self.key_mode = {self.ctx[m]["v2g_key"]: m for m in MODES}
        self.em_mode = {self.ctx[m]["evse_manager"]: m for m in MODES}
        self.ev_mode = {self.ctx[m]["ev_manager"]: m for m in MODES}
        self.key_labels: Dict[str, str] = {self.ctx[m]["v2g_key"]: "AC" for m in MODES}
        self.open_switches: Dict[str, float] = {}
        self.pending: Optional[dict] = None
        self.record: Optional[SwitchRecord] = None
        self.records: List[dict] = []
        self.user_overrides: dict = {}
        self.layer: Optional[InputLayer] = None
        self.last_decision: Optional[dict] = None
        self.next_k = 0

        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        self.log_dir = Path(log_dir or ORCH_DIR / cfg["orchestrator"]["log_dir"] / stamp)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.client = None

    # ------------------------------------------------------------------ setup

    def subscriptions(self) -> List[str]:
        subs = [self.topics["prices"], self.topics["user"]]
        for m in MODES:
            c = self.ctx[m]
            subs += [api_session_info_topic(c["evse_manager"]),
                     var_topic(c["evse_manager"], "evse", "session_event"),
                     var_topic(c["evse_manager"], "evse", "powermeter"),
                     cmd_topic(c["ev_manager"], "main", "execute_charging_session")]
            for v in ("switch_in_progress", "switch_complete", "cp_state", "current_mode"):
                subs.append(var_topic(c["v2g_key"], "v2g_key", v))
            subs.append(cmd_topic(c["v2g_key"], "v2g_key", "set_power_path") + f"/response/{ORIGIN}")
        subs.append(var_topic(self.ctx["DC_BPT"]["hlc"], "charger", "display_parameters"))
        if self.meter_source == "plant":
            subs.append(self.topics["plant_meter"])
        return subs

    def build_layer(self) -> InputLayer:
        d = self.dcfg
        sim_start = self.clock.sim_start
        hh, mm = (int(x) for x in d.departure.split(":"))
        dep = sim_start.replace(hour=hh, minute=mm)
        if dep <= sim_start:
            dep += timedelta(days=1)
        u = {"departure_time": dep.isoformat(), "target_soc": d.target_soc_pct, "min_soc": d.min_soc_pct,
             "battery_capacity_kwh": d.capacity_kwh, "user_soc": d.initial_soc_pct}
        u.update({k: v for k, v in self.user_overrides.items() if k in u})
        user = self.icfg.user.with_overrides(**u)
        layer = InputLayer(self.icfg, self.prices, self.soc, user, session_start=sim_start)
        self.departure = layer.departure
        return layer

    def start(self):
        self.layer = self.build_layer()
        if self.meter_source == "plant":
            # the battery-model meter is cumulative from 0: its first message is a real increment
            self.soc.set_meter_baseline("plant", 0.0, 0.0)
        self.client = connect(ORIGIN, self.cfg, self.on_message, self.subscriptions())

    # ------------------------------------------------------------------ helpers

    def _jsonl(self, name: str, obj: dict) -> None:
        with open(self.log_dir / f"{name}.jsonl", "a") as fh:
            fh.write(json.dumps(obj, default=str) + "\n")

    def _publish(self, topic: str, obj: dict, retain: bool = False) -> None:
        self.client.publish(topic, json.dumps(obj, default=str), qos=1, retain=retain)

    def _finish_record(self, reason: str) -> None:
        rec = self.record
        if rec is None:
            return
        if reason == "timeout":
            rec.timed_out = True
        s = rec.summary()
        s["finish_reason"] = reason
        self.records.append(s)
        self._jsonl("switches", s)
        self._publish(self.topics["switch_log"], s)
        log.info("SWITCH %s %s->%s: cmd->Charging %s s, responder delay %s s, adjusted %s s (%s)",
                 rec.switch_id, rec.from_mode, rec.to_mode, s["cmd_to_charging_s"], s["responder_delay_s"],
                 s["cmd_to_charging_minus_responder_s"], reason)
        self.record = None

    # ------------------------------------------------------------------ inbound

    def on_message(self, topic: str, payload: bytes, t: float):
        try:
            with self.lock:
                self._handle(topic, payload, t)
        except Exception:  # never let one bad message kill the network thread
            log.exception("error handling %s", topic)

    def _handle(self, topic: str, payload: bytes, t: float):
        parts = topic.split("/")
        if topic == self.topics["prices"]:
            self.prices.ingest_message(json.loads(payload))
            return
        if topic == self.topics.get("plant_meter"):
            m = json.loads(payload)
            self.soc.ingest_meter("plant", m["import_wh"], m["export_wh"])
            return
        if topic == self.topics["user"]:
            self.user_overrides = json.loads(payload)
            if "preset" in self.user_overrides:
                self.preset = self.user_overrides["preset"]
            self.layer = self.build_layer()
            log.info("user settings: %s", self.user_overrides)
            return
        if parts[0] == "everest_api":
            self._on_session_info(self.em_mode[parts[1]], json.loads(payload).get("state"), t)
            return

        module, name = parts[2], parts[-1]
        if "/cmd/set_power_path/response/" in topic:
            ret = json.loads(payload)["data"]["data"].get("retval")
            if self.record is not None:
                self.record.add(t, "cmd_result", ret)
            if not ret:
                log.warning("set_power_path rejected by %s", module)
                self.pending = None
                self._finish_record("rejected")
            return
        if name == "execute_charging_session":
            d = json.loads(payload)["data"]
            if d.get("origin") == RESPONDER_ORIGIN and self.record is not None:
                mode = self.ev_mode[module]
                which = "responder_start_session" if mode == self.record.to_mode else "responder_leave"
                self.record.add(t, which, d["args"]["value"])
            return

        data = unwrap_var(payload)
        if module in self.key_mode:
            self._on_key_var(module, name, data, t)
        elif name == "session_event" and self.record is not None:
            ev = data.get("event") or next(iter(data), None)
            mode = self.em_mode[module]
            which = "new_session_event" if mode == self.record.to_mode else "old_session_event"
            self.record.add(t, which, ev)
        elif name == "powermeter" and self.meter_source == "everest":
            self.soc.ingest_meter(self.em_mode[module], data["energy_Wh_import"]["total"],
                                  data.get("energy_Wh_export", {}).get("total", 0.0))
        elif name == "display_parameters" and data.get("present_soc") is not None:
            if self.system_mode == "DC_BPT":
                self.soc.ingest_soc_reading(data["present_soc"])

    def _on_session_info(self, mode: str, state: Optional[str], t: float):
        st = self.status[mode]
        changed = state != st.state
        st.state, st.t_rx = state, t
        if not changed:
            return
        log.info("%s session state -> %s", mode, state)
        rec = self.record
        if rec is not None:
            if mode == rec.to_mode:
                rec.add(t, "new_state", state)
                if state == "Charging" and rec.first("switch_complete") is not None:
                    rec.t_new_charging = t
                    self._finish_record("charging")
            else:
                rec.add(t, "old_state", state)
        if (not self.clock.started and mode == self.system_mode
                and state == self.cfg["clock"]["start_on_state"]):
            self.clock.start(t)
            log.info("decision clock started at %s (plug-in session %s)", self.clock.sim_start, state)

    def _on_key_var(self, key: str, name: str, data, t: float):
        rec = self.record
        mine = rec is not None and key == rec.key
        if name == "current_mode":
            self.key_labels[key] = data
        elif name == "cp_state":
            if mine:
                rec.add(t, "cp_state", data)
        elif name == "switch_in_progress":
            self.open_switches[key] = t
            if mine:
                rec.add(t, "switch_in_progress", data)
        elif name == "switch_complete":
            self.open_switches.pop(key, None)
            if mine:
                rec.add(t, "switch_complete", data)
            left = self.key_mode[key]
            if data.get("success") and left == self.system_mode:
                self.system_mode = other_mode(left)
                self.soc.set_mode(self.system_mode)
                log.info("system mode -> %s (switch_complete on %s, %s ms)", self.system_mode, key,
                         data.get("elapsed_ms"))
            elif not data.get("success"):
                log.error("switch FAILED on %s: %s", key, data)
                if mine:
                    self._finish_record("failed")
            self.pending = None

    # ------------------------------------------------------------------ decisions

    def guard(self, target: str, now: float):
        return check_guard(self.system_mode, target, self.status[self.system_mode], self.open_switches,
                           self.pending is not None, now, self.guard_cfg["allowed_states"],
                           float(self.guard_cfg["max_state_age_s"]))

    def send_switch(self, target: str, sim_t: datetime, now: float) -> SwitchRecord:
        key = self.ctx[self.system_mode]["v2g_key"]
        label = command_label(target, self.key_labels[key])
        cmd_id = f"{ORIGIN}-{uuid.uuid4().hex[:8]}"
        self.record = SwitchRecord(cmd_id, self.system_mode, target, key, label, sim_t.isoformat(), now)
        self.pending = {"cmd_id": cmd_id, "t": now, "key": key, "target": target}
        self.last_switch_sim = sim_t
        self.client.publish(cmd_topic(key, "v2g_key", "set_power_path"),
                            everest_cmd({"mode": label}, ORIGIN, cmd_id), qos=1)
        log.info("COMMAND set_power_path(%s) -> %s [%s -> %s]%s", label, key, self.system_mode, target,
                 "" if label == target else f" (label adjusted: key thinks it is {self.key_labels[key]})")
        return self.record

    def decide(self, k: int) -> dict:
        t = self.clock.cycle_time(k)
        now = time.time()
        snap = self.layer.get_snapshot(t)
        if self.preset == "Baseline":
            return self._decide_baseline(k, t, now, snap)
        res = plan(snap, ModeState(self.system_mode, self.last_switch_sim), self.preset, self.dcfg, self.lookup)
        cur = res.full_plan[0] if res.full_plan else None
        exports = [s for s in res.full_plan if s.action.value == "EXPORT"]
        charges = [s for s in res.full_plan if s.action.value == "CHARGE"]
        out = {
            "cycle": k, "sim_time": t.isoformat(), "wall": now, "preset": self.preset,
            "system_mode": self.system_mode, "action": res.current_action.value,
            "mode_command": res.mode_command.value,
            "forecast_rrp": next((p.rrp for p in snap.forecast if cur and p.interval_end == cur.interval_end), None),
            "sell_price": cur.sell_price if cur else None, "buy_price": cur.buy_price if cur else None,
            "soc": {"lower": snap.soc.lower, "upper": snap.soc.upper, "method": snap.soc.method},
            "pd_run": snap.runs["PD"].run_id if snap.runs.get("PD") else None,
            "flags": snap.flags.active(), "plan_net_profit": res.net_profit,
            "overrides": [f"{o.rule}@{o.interval_end:%H:%M}" for o in res.safety_overrides_applied],
            "command_sent": None, "guard": None,
            # why: the plan this action came from
            "why": {
                "plan_exports": len(exports), "plan_charges": len(charges),
                "next_export": exports[0].interval_end.isoformat() if exports else None,
                "best_planned_sell": max((s.sell_price for s in exports), default=None),
                "plan_revenue": res.total_export_revenue, "plan_charge_cost": res.total_charge_cost,
                "soc_plan_start": res.soc_plan_start, "planned_switches": res.mode_switches_planned,
                "lead_h": (cur.interval_end - snap.forecast[0].published).total_seconds() / 3600 if cur and snap.forecast else None,
            },
        }
        if res.mode_command is not ModeCommand.STAY:
            target = "DC_BPT" if res.mode_command is ModeCommand.SWITCH_TO_DC_BPT else "AC"
            g = self.guard(target, now)
            out["guard"] = {"ok": g.ok, "reasons": list(g.reasons)}
            if g.ok:
                rec = self.send_switch(target, t, now)
                out["command_sent"] = {"target": target, "key": rec.key, "label": rec.label_sent,
                                       "switch_id": rec.switch_id}
            else:
                log.info("cycle %d: planner wants %s, GUARD BLOCKED: %s", k, target, ", ".join(g.reasons))
        log.info("cycle %d @ %s: %s / %s, mode %s, SoC %.1f-%.1f (%s), fc %s",
                 k, t.strftime("%H:%M"), out["action"], out["mode_command"], self.system_mode,
                 snap.soc.lower, snap.soc.upper, snap.soc.method, out["forecast_rrp"])
        self.last_decision = out
        self._jsonl("decisions", out)
        self._publish(self.topics["decision"], out)
        return out

    def _decide_baseline(self, k: int, t: datetime, now: float, snap) -> dict:
        """Static AC-only baseline: plain AC charging until target, never switches."""
        action = "CHARGE" if snap.soc.mid < self.dcfg.target_soc_pct else "IDLE"
        out = {"cycle": k, "sim_time": t.isoformat(), "wall": now, "preset": "Baseline",
               "system_mode": self.system_mode, "action": action, "mode_command": "STAY",
               "forecast_rrp": snap.forecast[0].rrp if snap.forecast else None, "sell_price": None, "buy_price": None,
               "soc": {"lower": snap.soc.lower, "upper": snap.soc.upper, "method": snap.soc.method},
               "pd_run": snap.runs["PD"].run_id if snap.runs.get("PD") else None, "flags": snap.flags.active(),
               "plan_net_profit": None, "overrides": [], "command_sent": None, "guard": None,
               "why": {"rule": f"CHARGE while SoC mid {snap.soc.mid:.1f} < target {self.dcfg.target_soc_pct}"}}
        log.info("cycle %d @ %s: %s (baseline), SoC %.1f-%.1f", k, t.strftime("%H:%M"), action,
                 snap.soc.lower, snap.soc.upper)
        self.last_decision = out
        self._jsonl("decisions", out)
        self._publish(self.topics["decision"], out)
        return out

    def publish_state(self, now: float) -> None:
        sim_now = self.clock.now(now)
        est = estimate_soc(self.soc.samples_until(sim_now), self.layer.user, self.icfg.soc, self.icfg.thresholds)
        nxt_k = self.next_k
        state = {
            "wall": now, "clock_started": self.clock.started, "sim_time": sim_now.isoformat(),
            "mode": self.system_mode, "v2g_key_labels": self.key_labels,
            "soc": {"lower": est.lower, "upper": est.upper, "mid": est.mid, "method": est.method,
                    "meter_kwh": self.soc.meter_kwh},
            "next_decision_sim": self.clock.cycle_time(nxt_k).isoformat(),
            "next_decision_in_s": (round(self.clock.wall_of_cycle(nxt_k) - now, 2) if self.clock.started else None),
            "active_context_state": asdict(self.status[self.system_mode]),
            "guard_for_switch": dict(zip(("ok", "reasons"),
                                         (lambda g: (g.ok, list(g.reasons)))(self.guard(other_mode(self.system_mode), now)))),
            "open_switches": self.open_switches, "pending_command": self.pending,
            "last_decision": ({k: self.last_decision[k] for k in ("cycle", "sim_time", "action", "mode_command")}
                              if self.last_decision else None),
            "preset": self.preset, "price_runs": self.prices.n_runs("PD"),
        }
        self._publish(self.topics["state"], state, retain=True)

    # ------------------------------------------------------------------ main loop

    def run(self, duration_s: Optional[float] = None, until_departure: bool = False) -> List[dict]:
        t_end = None if duration_s is None else time.time() + duration_s
        last_tick = 0.0
        try:
            while t_end is None or time.time() < t_end:
                now = time.time()
                with self.lock:
                    if now - last_tick >= 1.0:
                        self._publish(self.topics["clock"], {"sim_time": self.clock.now(now).isoformat(),
                                                             "started": self.clock.started}, retain=True)
                        self.publish_state(now)
                        last_tick = now
                    if self.clock.started and now >= self.clock.wall_of_cycle(self.next_k):
                        late = now - self.clock.wall_of_cycle(self.next_k)
                        if late > self.clock.wall_seconds_per_cycle:
                            # the host stalled (e.g. slept): cycles now fire back to back on stale
                            # inputs. Recorded so the run can be identified as invalid.
                            stall = {"cycle": self.next_k, "late_s": round(late, 3), "wall": now,
                                     "sim_time": self.clock.cycle_time(self.next_k).isoformat()}
                            self._jsonl("stalls", stall)
                            log.error("CLOCK STALL: cycle %d fired %.1f s late (host sleep?) - run invalid",
                                      self.next_k, late)
                        if until_departure and self.clock.cycle_time(self.next_k) >= self.departure:
                            log.info("departure %s reached after %d cycles", self.departure, self.next_k)
                            break
                        self.decide(self.next_k)
                        self.next_k += 1
                    if self.record is not None and now - self.record.t_cmd > float(self.guard_cfg["switch_timeout_s"]):
                        self._finish_record("timeout")
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass
        with self.lock:
            if self.record is not None:
                self._finish_record("shutdown")
        return self.records

    def stop(self):
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--preset")
    ap.add_argument("--duration", type=float)
    ap.add_argument("--log-dir")
    ap.add_argument("--sim-start", type=datetime.fromisoformat)
    ap.add_argument("--until-departure", action="store_true")
    a = ap.parse_args(argv)
    setup_logging("orchestrator")
    node = OrchestratorNode(load_live_config(a.config), a.preset, Path(a.log_dir) if a.log_dir else None,
                            a.sim_start)
    node.start()
    log.info("logging to %s", node.log_dir)
    try:
        node.run(a.duration, a.until_departure)
    finally:
        node.stop()


if __name__ == "__main__":
    main()
