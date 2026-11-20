"""Switch trigger guard and switch timing record (pure logic, no MQTT).

Guard (mandatory): a set_power_path command is sent only if ALL of:
  * the target differs from the orchestrator's system mode;
  * the ACTIVE context's ``session_info.state`` is in ``allowed_states``
    (Charging or a deliberate pause) and was received within ``max_state_age_s``;
  * no ``switch_in_progress`` is open on either V2G-Key (seen without a matching
    ``switch_complete``);
  * no earlier command is still awaiting its result / switch_complete.

Switch record: every event between the command and the new context reaching
``Charging`` is timestamped (wall clock, at receipt by the orchestrator).
Headline metric = command sent -> new-context session_info.state == Charging.
For comparison with real hardware the EV responder's delay
(switch_complete -> new-session command) is subtracted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class ContextStatus:
    state: Optional[str] = None
    t_rx: Optional[float] = None       # wall time the state was received


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reasons: Tuple[str, ...]


def check_guard(system_mode: str, target: str, active: ContextStatus, open_switches: Dict[str, float],
                command_pending: bool, now: float, allowed_states: Sequence[str],
                max_state_age_s: float) -> GuardResult:
    reasons: List[str] = []
    if target == system_mode:
        reasons.append(f"already_in_{target}")
    if active.state is None or active.t_rx is None:
        reasons.append("active_state_unknown")
    else:
        if active.state not in allowed_states:
            reasons.append(f"active_state={active.state}")
        age = now - active.t_rx
        if age > max_state_age_s:
            reasons.append(f"active_state_stale({age:.1f}s)")
    if open_switches:
        reasons.append("switch_in_progress_open:" + ",".join(sorted(open_switches)))
    if command_pending:
        reasons.append("command_pending")
    return GuardResult(not reasons, tuple(reasons))


def command_label(target: str, key_label: str) -> str:
    """Mode argument to send to the V2G-Key of the context being LEFT.

    Each VirtualV2GKey keeps its own current_mode label (initialised "AC", not
    synced with the other instance) and rejects set_power_path(mode) when
    mode == its label. The CP pulse it performs is direction-agnostic, so if
    the honest label (``target``) would be rejected, the other label is sent.
    """
    return target if target != key_label else ("AC" if target == "DC_BPT" else "DC_BPT")


@dataclass
class SwitchRecord:
    switch_id: str
    from_mode: str
    to_mode: str
    key: str                     # V2G-Key commanded (context being left)
    label_sent: str
    sim_time: str
    t_cmd: float
    events: List[Tuple[float, str, object]] = field(default_factory=list)
    t_new_charging: Optional[float] = None
    timed_out: bool = False

    def add(self, t: float, name: str, data: object = None) -> None:
        self.events.append((t, name, data))

    def first(self, name: str, pred=None) -> Optional[float]:
        for t, n, d in self.events:
            if n == name and (pred is None or pred(d)):
                return t
        return None

    @property
    def done(self) -> bool:
        return self.t_new_charging is not None or self.timed_out

    def summary(self) -> dict:
        rel = lambda t: None if t is None else round(t - self.t_cmd, 3)  # noqa: E731
        t_prog = self.first("switch_in_progress")
        t_comp = self.first("switch_complete")
        t_resp = self.first("responder_start_session")
        comp = next((d for _, n, d in self.events if n == "switch_complete"), {}) or {}
        cp = [(rel(t), d) for t, n, d in self.events if n == "cp_state"]
        total = rel(self.t_new_charging)
        responder_delay = None if (t_resp is None or t_comp is None) else round(t_resp - t_comp, 3)
        return {
            "switch_id": self.switch_id, "from_mode": self.from_mode, "to_mode": self.to_mode,
            "key": self.key, "label_sent": self.label_sent, "label_adjusted": self.label_sent != self.to_mode,
            "sim_time": self.sim_time, "t_cmd": self.t_cmd, "timed_out": self.timed_out,
            # headline
            "cmd_to_charging_s": total,
            "responder_delay_s": responder_delay,
            "cmd_to_charging_minus_responder_s": (None if total is None or responder_delay is None
                                                  else round(total - responder_delay, 3)),
            # same end point from the ChargingStarted session_event (ms resolution;
            # session_info is only published ~1 Hz)
            "cmd_to_charging_started_event_s": rel(self.first("new_session_event", lambda d: d == "ChargingStarted")),
            # breakdown, seconds after the command
            "t_cmd_result": rel(self.first("cmd_result")),
            "t_switch_in_progress": rel(t_prog),
            "t_switch_complete": rel(t_comp),
            "v2g_key_elapsed_ms": comp.get("elapsed_ms"),
            "cp_states": cp,
            "t_old_paused": rel(self.first("old_session_event", lambda d: str(d).startswith("ChargingPaused"))),
            "t_old_finished": rel(self.first("old_state", lambda d: d in ("Finished", "FinishedEV", "Unplugged"))),
            "t_responder_start_session": rel(t_resp),
            "new_states": [(rel(t), d) for t, n, d in self.events if n == "new_state"],
            "events": [(rel(t), n, d) for t, n, d in self.events],
        }
