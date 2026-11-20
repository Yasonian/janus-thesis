"""Shared helpers for the live processes: config, MQTT client, EVerest payloads, clock."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import paho.mqtt.client as mqtt
import yaml

ORCH_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LIVE_CONFIG = ORCH_DIR / "config" / "live.yaml"
MODES = ("AC", "DC_BPT")

log = logging.getLogger("janus")


def load_live_config(path=None) -> dict:
    return yaml.safe_load(Path(path or DEFAULT_LIVE_CONFIG).read_text())


def other_mode(mode: str) -> str:
    return "DC_BPT" if mode == "AC" else "AC"


# --------------------------------------------------------------------------- #
# EVerest topic / payload conventions
# --------------------------------------------------------------------------- #

def var_topic(module: str, impl: str, var: str) -> str:
    return f"everest/modules/{module}/impl/{impl}/var/{var}"


def cmd_topic(module: str, impl: str, cmd: str) -> str:
    return f"everest/modules/{module}/impl/{impl}/cmd/{cmd}"


def api_session_info_topic(evse_manager: str) -> str:
    return f"everest_api/{evse_manager}/var/session_info"


def unwrap_var(payload: bytes) -> Any:
    """everest/... Var payload: {"data":{"data": X},"msg_type":"Var"} -> X."""
    return json.loads(payload)["data"]["data"]


def everest_cmd(args: dict, origin: str, cmd_id: Optional[str] = None) -> str:
    return json.dumps({"data": {"args": args, "id": cmd_id or f"{origin}-{uuid.uuid4().hex[:8]}",
                                "origin": origin}, "msg_type": "Cmd"})


# --------------------------------------------------------------------------- #
# MQTT
# --------------------------------------------------------------------------- #

def connect(client_id: str, cfg: dict, on_message: Callable, subscriptions=()) -> mqtt.Client:
    """Connected paho client with its network loop running in a background thread."""
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)

    def _on_connect(client, userdata, flags, reason_code, properties):
        for t in subscriptions:
            client.subscribe(t, qos=1)
        log.info("%s connected to %s:%s, %d subscriptions", client_id,
                 cfg["broker"]["host"], cfg["broker"]["port"], len(subscriptions))

    c.on_connect = _on_connect
    c.on_message = lambda client, userdata, msg: on_message(msg.topic, msg.payload, time.time())
    c.connect(cfg["broker"]["host"], int(cfg["broker"]["port"]))
    c.loop_start()
    return c


# --------------------------------------------------------------------------- #
# Decision-time clock
# --------------------------------------------------------------------------- #

@dataclass
class SimClock:
    """Maps wall time to decision (NEM) time once started.

    One 5-minute decision cycle passes every ``wall_seconds_per_cycle`` wall
    seconds. Only the decision cadence is compressed; everything EVerest does
    (switch durations, charging) still runs in real wall time.
    """
    sim_start: datetime
    wall_seconds_per_cycle: float
    cycle_minutes: int = 5
    wall_start: Optional[float] = None

    @property
    def started(self) -> bool:
        return self.wall_start is not None

    def start(self, wall: Optional[float] = None) -> None:
        self.wall_start = time.time() if wall is None else wall

    def now(self, wall: Optional[float] = None) -> datetime:
        if not self.started:
            return self.sim_start
        wall = time.time() if wall is None else wall
        scale = self.cycle_minutes * 60.0 / self.wall_seconds_per_cycle
        return self.sim_start + timedelta(seconds=(wall - self.wall_start) * scale)

    def cycle_time(self, k: int) -> datetime:
        return self.sim_start + timedelta(minutes=self.cycle_minutes * k)

    def wall_of_cycle(self, k: int) -> float:
        return self.wall_start + k * self.wall_seconds_per_cycle


def setup_logging(name: str) -> None:
    logging.basicConfig(level=logging.INFO,
                        format=f"%(asctime)s.%(msecs)03d [{name}] %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
