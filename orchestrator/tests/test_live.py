"""Unit tests for the Week 4 live layer (no broker needed)."""
from datetime import datetime, timedelta

import pytest

from orchestrator.inputs.config import load_config
from orchestrator.inputs.soc import estimate_soc
from orchestrator.inputs.sources import LivePriceSource, LiveSocSource
from orchestrator.live.common import SimClock, everest_cmd, other_mode, unwrap_var
from orchestrator.live.switching import ContextStatus, SwitchRecord, check_guard, command_label

T0 = datetime(2026, 1, 11, 18, 0)
ALLOWED = ["Charging", "ChargingPausedEV", "ChargingPausedEVSE"]


def run_msg(run_id, published, first_target, rrps):
    return {"kind": "PD", "run_id": run_id, "published": published.isoformat(), "region": "NSW1",
            "points": [{"target": (first_target + timedelta(minutes=30 * i)).isoformat(), "rrp": r}
                       for i, r in enumerate(rrps)]}


# ------------------------------------------------------------------ LivePriceSource

def test_live_price_source_availability_rule():
    src = LivePriceSource()
    src.ingest_message(run_msg("r1", T0 - timedelta(minutes=27), T0, [10, 20]))
    src.ingest_message(run_msg("r2", T0 + timedelta(minutes=3), T0 + timedelta(minutes=30), [30]))  # early arrival
    assert src.latest_run("PD", T0).run_id == "r1"                     # r2 not yet published
    assert src.latest_run("PD", T0 + timedelta(minutes=5)).run_id == "r2"
    assert src.latest_run("PD", T0 - timedelta(hours=1)) is None
    assert src.n_runs("PD") == 2


def test_live_price_source_feeds_input_layer(config):
    from orchestrator.inputs.snapshot import InputLayer
    src = LivePriceSource()
    src.ingest_message(run_msg("r1", T0 - timedelta(minutes=27), T0 + timedelta(minutes=30), [1500, 60, 60]))
    layer = InputLayer(config, src, None, config.user.with_overrides(departure_time="19:00"), session_start=T0)
    s = layer.get_snapshot(T0)
    assert [p.rrp for p in s.forecast[:6]] == [1500.0] * 6 and s.forecast[0].run_id == "r1"


def test_live_price_source_ignores_other_region_and_replaces_run():
    src = LivePriceSource("NSW1")
    m = run_msg("r1", T0, T0 + timedelta(minutes=30), [10])
    src.ingest_message({**m, "region": "VIC1"})
    assert src.n_runs("PD") == 0
    src.ingest_message(m)
    src.ingest_message(run_msg("r1", T0, T0 + timedelta(minutes=30), [99]))
    assert src.latest_run("PD", T0).frame["rrp"].tolist() == [99.0]


def test_live_price_source_dispatch():
    src = LivePriceSource()
    src.ingest_message({"kind": "DISPATCH", "published": "2026-01-11T18:00:09", "target": "2026-01-11T18:05", "rrp": 57})
    assert src.latest_dispatch(T0) is None
    assert src.latest_dispatch(T0 + timedelta(seconds=10))["rrp"] == 57


# ------------------------------------------------------------------ LiveSocSource

class FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_live_soc_source_sums_meters_and_handles_reset():
    clk = FakeClock(T0)
    s = LiveSocSource(clk)
    s.ingest_meter("AC", 1000.0, 0.0)            # baseline only
    clk.t += timedelta(minutes=1)
    s.ingest_meter("AC", 3000.0, 0.0)            # +2 kWh
    s.ingest_meter("DC_BPT", 500.0, 0.0)         # new meter: baseline
    s.ingest_meter("DC_BPT", 1500.0, 200.0)      # +1.0 -0.2
    s.ingest_meter("AC", 0.0, 0.0)               # reset: contributes 0
    s.ingest_meter("AC", 100.0, 0.0)             # +0.1
    assert s.meter_kwh == pytest.approx(2.9)
    assert all(x.soc_reading is None for x in s.samples_until(clk.t))


def test_live_soc_source_readings_and_no_lookahead(config):
    clk = FakeClock(T0)
    s = LiveSocSource(clk, mode="DC_BPT")
    s.ingest_meter("DC_BPT", 0.0, 0.0)
    clk.t = T0 + timedelta(minutes=2)
    s.ingest_soc_reading(30.0)
    clk.t = T0 + timedelta(minutes=7)
    s.ingest_soc_reading(31.0)
    got = s.samples_until(T0 + timedelta(minutes=5))
    assert [x.soc_reading for x in got if x.soc_reading is not None] == [30.0]
    assert got[-1].mode == "DC"
    est = estimate_soc(got, config.user, config.soc, config.thresholds)
    assert est.method == "direct" and est.mid == 30.0


# ------------------------------------------------------------------ guard

def guard(**kw):
    base = dict(system_mode="AC", target="DC_BPT", active=ContextStatus("Charging", 100.0),
                open_switches={}, command_pending=False, now=101.0, allowed_states=ALLOWED, max_state_age_s=5)
    base.update(kw)
    return check_guard(**base)


def test_guard_passes_when_charging_and_idle():
    g = guard()
    assert g.ok and g.reasons == ()


@pytest.mark.parametrize("kw,reason", [
    (dict(active=ContextStatus("Preparing", 100.0)), "active_state=Preparing"),
    (dict(active=ContextStatus(None, None)), "active_state_unknown"),
    (dict(active=ContextStatus("Charging", 90.0)), "active_state_stale"),
    (dict(open_switches={"ac_v2g_key": 99.0}), "switch_in_progress_open"),
    (dict(command_pending=True), "command_pending"),
    (dict(target="AC"), "already_in_AC"),
])
def test_guard_blocks(kw, reason):
    g = guard(**kw)
    assert not g.ok and any(r.startswith(reason) for r in g.reasons)


def test_guard_allows_deliberate_pause():
    assert guard(active=ContextStatus("ChargingPausedEV", 100.0)).ok


# ------------------------------------------------------------------ command label / record / clock

@pytest.mark.parametrize("target,label,expect", [
    ("DC_BPT", "AC", "DC_BPT"), ("AC", "DC_BPT", "AC"),
    ("AC", "AC", "DC_BPT"),          # dc_v2g_key still labelled AC: honest label would be rejected
    ("DC_BPT", "DC_BPT", "AC"),
])
def test_command_label(target, label, expect):
    assert command_label(target, label) == expect


def test_switch_record_summary():
    r = SwitchRecord("s1", "AC", "DC_BPT", "ac_v2g_key", "DC_BPT", T0.isoformat(), t_cmd=100.0)
    r.add(100.01, "cmd_result", True)
    r.add(100.02, "switch_in_progress", {"from_mode": "AC", "to_mode": "DC_BPT"})
    r.add(100.3, "cp_state", "E")
    r.add(100.5, "old_session_event", "ChargingPausedEV")
    r.add(101.0, "switch_complete", {"elapsed_ms": 961, "success": True})
    r.add(101.2, "responder_start_session", "prog")
    r.add(109.4, "new_session_event", "ChargingStarted")
    r.add(110.0, "new_state", "Charging")
    r.t_new_charging = 110.0
    s = r.summary()
    assert s["cmd_to_charging_s"] == 10.0 and s["responder_delay_s"] == pytest.approx(0.2)
    assert s["cmd_to_charging_minus_responder_s"] == pytest.approx(9.8)
    assert s["cmd_to_charging_started_event_s"] == pytest.approx(9.4)
    assert s["v2g_key_elapsed_ms"] == 961 and s["t_old_paused"] == 0.5 and not s["label_adjusted"]


def test_sim_clock():
    c = SimClock(T0, wall_seconds_per_cycle=10)
    assert c.now() == T0 and not c.started
    c.start(1000.0)
    assert c.now(1010.0) == T0 + timedelta(minutes=5)
    assert c.cycle_time(3) == T0 + timedelta(minutes=15) and c.wall_of_cycle(3) == 1030.0


def test_everest_payload_helpers():
    import json
    assert unwrap_var(b'{"data":{"data":"DC_BPT"},"msg_type":"Var"}') == "DC_BPT"
    m = json.loads(everest_cmd({"mode": "AC"}, "o", "id1"))
    assert m == {"data": {"args": {"mode": "AC"}, "id": "id1", "origin": "o"}, "msg_type": "Cmd"}
    assert other_mode("AC") == "DC_BPT" and other_mode("DC_BPT") == "AC"


# ------------------------------------------------------------------ battery model (plant)

from orchestrator.decision.config import load_decision_config
from orchestrator.live.plant import BatteryPlant, baseline_cost


def test_plant_matches_runner_rules():
    c = load_decision_config()
    p = BatteryPlant(c, 60.0)
    s = p.apply(0, "t0", "t1", "CHARGE", "AC", 100.0)               # 7.4 kW * 5 min
    assert s.kwh == pytest.approx(7.4 / 12) and s.grid_kwh == pytest.approx(7.4 / 12 / 0.9)
    assert s.cashflow == pytest.approx(-0.1 * 7.4 / 12 / 0.9)
    s = p.apply(1, "t1", "t2", "EXPORT", "DC_BPT", 1000.0)          # 10 kW * 5 min, degradation 0.02
    assert s.kwh == pytest.approx(-10 / 12) and s.cashflow == pytest.approx((1.0 - 0.02) * 10 / 12)
    s = p.apply(2, "t2", "t3", "CHARGE", "DC_BPT", 50.0)            # 50 kW in DC
    assert s.kwh == pytest.approx(50 / 12)
    assert p.import_wh == pytest.approx((7.4 / 12 + 50 / 12) / 0.9 * 1000) and p.export_wh == pytest.approx(10 / 12 * 1000)


def test_plant_export_in_ac_not_executable_and_floor():
    c = load_decision_config()
    p = BatteryPlant(c, 20.5)
    assert p.apply(0, "a", "b", "EXPORT", "AC", 5000.0).executed == "IDLE"
    s = p.apply(1, "b", "c", "EXPORT", "DC_BPT", 5000.0)            # clamped at min_soc 20 %
    assert p.soc == pytest.approx(20.0) and s.kwh == pytest.approx(-0.005 * 60)


def test_plant_baseline_cost():
    c = load_decision_config()
    # 60 -> 80 % = 12 kWh at 7.4 kW: 19 full slots + a partial one
    cost = baseline_cost(c, 60.0, [100.0] * 30)
    assert cost == pytest.approx(0.1 * 12 / 0.9)


def test_live_soc_source_zero_based_meter_keeps_first_increment():
    clk = FakeClock(T0)
    s = LiveSocSource(clk)
    s.set_meter_baseline("plant", 0.0, 0.0)
    clk.t += timedelta(minutes=1)
    s.ingest_meter("plant", 685.0, 0.0)              # first message already holds a slot's energy
    assert s.meter_kwh == pytest.approx(0.685)
    s2 = LiveSocSource(clk)
    s2.ingest_meter("plant", 685.0, 0.0)             # without the baseline it is swallowed
    assert s2.meter_kwh == 0.0
