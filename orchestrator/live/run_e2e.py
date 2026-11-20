"""End-to-end SiL test: EVerest (JANUS 2-context) + orchestrator + price feeder + EV responder.

    python -m orchestrator.live.run_e2e [--everest-config config/config-sil-janus-2ctx-bpt.yaml]
                                        [--duration 200] [--preset Naive]

1. restarts the EVerest manager in the dev container with the given config;
2. starts a broker-side capture (mosquitto_sub -F '%U %t %p', broker container) of
   everest/#, everest_api/# and janus/#;
3. starts the orchestrator node, the price feeder and the EV responder as separate
   processes (the responder plugs the car into AC once the AC EVSE is ready; the
   orchestrator's clock starts when that session reaches Charging);
4. after --duration seconds stops everything and collects all logs into
   orchestrator/live_logs/<timestamp>/.

No switch is triggered by this script: every set_power_path comes from the orchestrator.
"""
from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

from .common import ORCH_DIR, load_live_config

NET = "janus-dev-net"
BROKER = "everest-workspace_devcontainer-mqtt-server-1"
PY = sys.executable
# switch- and session-relevant topics only (telemetry/powermeter would be ~180 MB per 13 h session)
CAPTURE_TOPICS = [
    "janus/#", "everest_external/#",
    "everest/modules/+/impl/v2g_key/#",
    "everest/modules/+/impl/evse/var/session_event",
    "everest/modules/+/impl/evse/var/selected_protocol",
    "everest/modules/+/impl/main/cmd/execute_charging_session",
    "everest/modules/+/impl/charger/var/display_parameters",
    "everest/modules/+/impl/charger/var/selected_service_parameters",
    "everest_api/+/var/session_info",
]


def sh(*args, check=False):
    return subprocess.run(args, capture_output=True, text=True, check=check)


def clear_retained(cfg):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="janus_e2e_cleanup")
    c.connect(cfg["broker"]["host"], int(cfg["broker"]["port"]))
    c.loop_start()
    for t in (cfg["topics"]["clock"], cfg["topics"]["state"], cfg["topics"]["user"]):
        c.publish(t, b"", qos=1, retain=True).wait_for_publish(2)
    c.loop_stop()
    c.disconnect()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--everest-config", default="config/config-sil-janus-2ctx-bpt.yaml")
    ap.add_argument("--duration", type=float, default=200, help="wall seconds (ignored with --until-departure)")
    ap.add_argument("--until-departure", action="store_true", help="run the whole session to departure")
    ap.add_argument("--sim-start", help="decision-time plug-in, e.g. 2026-01-10T18:00")
    ap.add_argument("--data", help="price data for feeder and plant: real | synthetic_spike")
    ap.add_argument("--preset")
    ap.add_argument("--tag", default="")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    cfg = load_live_config(a.config)
    run_dir = ORCH_DIR / cfg["orchestrator"]["log_dir"] / (datetime.now().strftime("%Y%m%dT%H%M%S") + (f"_{a.tag}" if a.tag else ""))
    if a.until_departure:   # whole session: cycles to departure plus boot margin
        a.duration = 13 * 12 * float(cfg["clock"]["wall_seconds_per_cycle"]) + 120
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"run dir: {run_dir}")

    sh("docker", "exec", NET, "bash", "-c", "pkill -9 -f bin/manager; true")
    sh("docker", "exec", BROKER, "sh", "-c", "pkill mosquitto_sub; true")
    time.sleep(1)
    clear_retained(cfg)
    sh("docker", "exec", "-d", BROKER, "sh", "-c",
       "mosquitto_sub -F '%U %t %p' -q 1 " + " ".join(f"-t '{t}'" for t in CAPTURE_TOPICS) + " > /tmp/e2e.mqtt 2>&1")
    time.sleep(1)
    mgr_timeout = int(a.duration + 60)
    sh("docker", "exec", "-d", "-e", "MQTT_SERVER_ADDRESS=mqtt-server", "-e", "MQTT_SERVER_PORT=1883", NET,
       "bash", "-c", f"cd /workspace/everest-core && timeout {mgr_timeout} ./build/dist/bin/manager "
                     f"--conf {a.everest_config} > /tmp/e2e-manager.log 2>&1")
    t0 = time.time()

    procs = {}
    extra = ["--config", a.config] if a.config else []
    sim = ["--sim-start", a.sim_start] if a.sim_start else []
    node_args = [PY, "-m", "orchestrator.live.node", "--duration", str(a.duration), "--log-dir", str(run_dir), *sim, *extra]
    if a.preset:
        node_args += ["--preset", a.preset]
    if a.until_departure:
        node_args += ["--until-departure"]
    feeder_args = [PY, "-m", "orchestrator.live.price_feeder", *sim, *extra] + (["--data", a.data] if a.data else [])
    resp_args = [PY, "-m", "orchestrator.live.ev_responder", "--log-dir", str(run_dir), *extra] + \
        (["--plant-data", a.data] if a.data else [])
    for name, args in [("node", node_args), ("price_feeder", feeder_args), ("ev_responder", resp_args)]:
        procs[name] = subprocess.Popen(args, cwd=ORCH_DIR.parent, stdout=open(run_dir / f"{name}.log", "w"),
                                       stderr=subprocess.STDOUT)
        time.sleep(1)

    try:
        procs["node"].wait(timeout=a.duration + 30)
    except subprocess.TimeoutExpired:
        pass
    for name, p in procs.items():
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
    for p in procs.values():
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    sh("docker", "exec", NET, "bash", "-c", "pkill -9 -f bin/manager; true")
    sh("docker", "exec", BROKER, "sh", "-c", "pkill mosquitto_sub; true")
    time.sleep(1)
    sh("docker", "cp", f"{BROKER}:/tmp/e2e.mqtt", str(run_dir / "broker_capture.mqtt"))
    sh("docker", "cp", f"{NET}:/tmp/e2e-manager.log", str(run_dir / "everest_manager.log"))
    (run_dir / "run.json").write_text(json.dumps({"everest_config": a.everest_config, "duration_s": a.duration,
                                                  "preset": a.preset, "sim_start": a.sim_start, "data": a.data,
                                                  "until_departure": a.until_departure, "started": t0,
                                                  "capture_topics": CAPTURE_TOPICS}, indent=2))
    sw = run_dir / "switches.jsonl"
    print(f"elapsed {time.time() - t0:.0f} s")
    if sw.exists():
        for line in sw.read_text().splitlines():
            s = json.loads(line)
            print(f"{s['from_mode']}->{s['to_mode']}: cmd->Charging {s['cmd_to_charging_s']} s, "
                  f"responder {s['responder_delay_s']} s, adjusted {s['cmd_to_charging_minus_responder_s']} s "
                  f"[{s['finish_reason']}]")
    else:
        print("no switches recorded")


if __name__ == "__main__":
    main()
