"""Tests for the E3-E5 offline experiment machinery."""
from datetime import date, datetime, timedelta

import pytest

from orchestrator.decision.config import load_decision_config
from orchestrator.decision.experiments import _in_window, make_tou_planner
from orchestrator.decision.planner import AC, DC_BPT, Action, ModeCommand, ModeState
from orchestrator.decision.scenario_runner import load_data, run_session

from test_planner import T0, snap   # hand-built snapshot helper (T0 = 2026-01-11 18:00)


# ------------------------------------------------------------------ time windows

@pytest.mark.parametrize("h,window,inside", [
    (22.0, (22, 6), True), (3.5, (22, 6), True), (5.99, (22, 6), True), (6.0, (22, 6), False),
    (21.9, (22, 6), False), (14.0, (14, 20), True), (19.92, (14, 20), True), (20.0, (14, 20), False),
])
def test_in_window(h, window, inside):
    assert _in_window(h, window) is inside


# ------------------------------------------------------------------ ToU strategy

def test_tou_exports_in_peak_above_threshold_only():
    cfg = load_decision_config()
    tou = make_tou_planner()
    r = tou(snap([500] + [60] * 155), ModeState(), "Naive", cfg, None)          # 18:00, peak
    assert r.current_action is Action.EXPORT and r.mode_command is ModeCommand.SWITCH_TO_DC_BPT
    r = tou(snap([299] + [60] * 155), ModeState(), "Naive", cfg, None)
    assert r.current_action is Action.IDLE and r.mode_command is ModeCommand.STAY


def test_tou_charges_only_off_peak_below_target():
    cfg = load_decision_config()
    tou = make_tou_planner()
    late = snap([60] * 100)
    late = late.__class__(**{**late.__dict__, "t": datetime(2026, 1, 11, 23, 0)})
    assert tou(late, ModeState(), "Naive", cfg, None).current_action is Action.CHARGE
    full = snap([60] * 100, soc=85.0)
    full = full.__class__(**{**full.__dict__, "t": datetime(2026, 1, 11, 23, 0)})
    assert tou(full, ModeState(), "Naive", cfg, None).current_action is Action.IDLE
    # 18:00 is neither off-peak nor (with a low price) an export: idle
    assert tou(snap([60] * 156), ModeState(), "Naive", cfg, None).current_action is Action.IDLE


def test_tou_respects_soc_floor_and_returns_to_ac():
    cfg = load_decision_config()
    tou = make_tou_planner()
    r = tou(snap([5000] + [60] * 155, soc=20.9, half_width=0.5), ModeState(), "Naive", cfg, None)
    assert r.current_action is not Action.EXPORT
    st = ModeState(DC_BPT, T0 - timedelta(minutes=30))                          # dwell satisfied
    assert tou(snap([60] * 156), st, "Naive", cfg, None).mode_command is ModeCommand.SWITCH_TO_AC


# ------------------------------------------------------------------ downtime model

@pytest.fixture(scope="module")
def data(pd_path, dispatch_path):
    return load_data("real")


def test_downtime_none_is_unchanged(data):
    a = run_session(date(2026, 1, 10), "A", data)
    b = run_session(date(2026, 1, 10), "A", data, switch_downtime_s=None)
    assert a["cum_cashflow"].iloc[-1] == b["cum_cashflow"].iloc[-1]
    assert (a["slot_availability"] == 1.0).all()


def test_downtime_scales_switch_slot_energy(data):
    df = run_session(date(2026, 1, 10), "A", data, switch_downtime_s=60)
    sw = df[df.mode_command != "STAY"]
    assert len(sw) == 2 and sw.slot_availability.tolist() == pytest.approx([0.8, 0.8])
    first = sw.iloc[0]                       # 18:30 AC->DC_BPT with EXPORT: 10 kW * 4 min
    assert first.action == "EXPORT" and first.energy_kwh == pytest.approx(10 * 4 / 60)
    assert (df.loc[df.mode_command == "STAY", "slot_availability"] == 1.0).all()


def test_downtime_longer_than_a_slot_carries_over(data):
    df = run_session(date(2026, 1, 10), "A", data, switch_downtime_s=420)       # 7 min
    i = df.index[df.mode_command != "STAY"][0]
    assert df.loc[i, "slot_availability"] == 0.0
    assert df.loc[i + 1, "slot_availability"] == pytest.approx(0.6)


def test_downtime_asymmetric(data):
    df = run_session(date(2026, 1, 10), "A", data,
                     switch_downtime_s={"SWITCH_TO_DC_BPT": 10.0, "SWITCH_TO_AC": 15.0})
    sw = df[df.mode_command != "STAY"].set_index("mode_command")
    assert sw.loc["SWITCH_TO_DC_BPT", "switch_downtime_s"] == 10.0
    assert sw.loc["SWITCH_TO_AC", "switch_downtime_s"] == 15.0


# ------------------------------------------------------------------ follow-up variants

def test_sell_only_preset_adjusts_sell_but_not_buy():
    from orchestrator.decision.planner import plan
    from test_planner import Q
    from orchestrator.inputs.error_dist import ALL_PRICES, LEAD_BINS, PRICE_BINS
    from orchestrator.inputs.error_lookup import ErrorLookup
    cell = {**Q, "count": 1000, "mean": 0.0, "std": 30.0}
    lk = ErrorLookup({lb: {pb: dict(cell) for pb, *_ in PRICE_BINS + [(ALL_PRICES,)]} for lb, *_ in LEAD_BINS})
    cfg = load_decision_config(overrides={"presets": {"B": {"buy": None}}})
    assert cfg.presets["B"] == ("q25", None)
    step = plan(snap([500] + [60] * 155), ModeState(), "B", cfg, lk).full_plan[0]
    assert step.sell_price == 500 + Q["q25"] and step.buy_price == 500


def test_tou_threshold_parameter():
    cfg = load_decision_config()
    s = snap([200] + [60] * 155)
    assert make_tou_planner(export_threshold=300.0)(s, ModeState(), "Naive", cfg, None).current_action is Action.IDLE
    assert make_tou_planner(export_threshold=150.0)(s, ModeState(), "Naive", cfg, None).current_action is Action.EXPORT
