"""Planner unit tests on hand-built snapshots.

A uniform synthetic error table is used (q10 -50, q25 -20, q50 -10, q75 +20,
q90 +50 in every group) so the planner logic is tested independently of the
January error table, which has sparse high-price groups.
"""
from datetime import datetime, timedelta

import pytest

from orchestrator.decision.config import load_decision_config
from orchestrator.decision.planner import AC, DC_BPT, Action, ModeCommand, ModeState, plan
from orchestrator.inputs.config import load_config
from orchestrator.inputs.error_dist import ALL_PRICES, LEAD_BINS, PRICE_BINS
from orchestrator.inputs.error_lookup import ErrorLookup
from orchestrator.inputs.forecast import ForecastPoint, UnknownPeriod
from orchestrator.inputs.snapshot import Snapshot, StatusFlags
from orchestrator.inputs.soc import SocEstimate

T0 = datetime(2026, 1, 11, 18, 0)
SLOT = timedelta(minutes=5)
PRESETS = ["Naive", "A", "B", "C"]
Q = {"q10": -50.0, "q25": -20.0, "q50": -10.0, "q75": 20.0, "q90": 50.0}


@pytest.fixture(scope="module")
def lookup():
    cell = {**Q, "count": 1000, "mean": 0.0, "std": 30.0}
    return ErrorLookup({lb: {pb: dict(cell) for pb, *_ in PRICE_BINS + [(ALL_PRICES,)]}
                        for lb, *_ in LEAD_BINS})


@pytest.fixture(scope="module")
def cfg():
    return load_decision_config()


def snap(prices, soc=60.0, half_width=1.0, departure_slots=None, unknown_slots=0, **flags):
    published = T0 - timedelta(minutes=27)
    pts = [ForecastPoint(T0 + (i + 1) * SLOT, float(p), "PD", "test", published,
                         T0 + (i + 1) * SLOT - published, T0 + (i + 1) * SLOT) for i, p in enumerate(prices)]
    n = departure_slots or (len(prices) + unknown_slots)
    unknown = []
    if unknown_slots:
        start = T0 + len(prices) * SLOT
        unknown = [UnknownPeriod(start, start + unknown_slots * SLOT, "beyond_horizon")]
    lo, hi = soc - half_width, soc + half_width
    flags.setdefault("soc_uncertain", hi - lo > 10)
    return Snapshot(
        t=T0, forecast=pts, unknown=unknown, runs={}, latest_dispatch_rrp=None,
        latest_dispatch_interval=None, discarded_prices={},
        soc=SocEstimate(lo, hi, "direct", T0, 0.0, 0, flags["soc_uncertain"]),
        user=load_config().user, departure=T0 + n * SLOT, flags=StatusFlags(**flags))


def actions(res):
    return [s.action for s in res.full_plan]


@pytest.mark.parametrize("preset", PRESETS)
def test_flat_prices_no_export_charges_to_target(preset, cfg, lookup):
    res = plan(snap([60] * 156), ModeState(), preset, cfg, lookup)
    acts = actions(res)
    assert Action.EXPORT not in acts
    assert res.full_plan[-1].soc_after >= cfg.target_soc_pct
    need = (cfg.target_soc_pct - 60) / 100 * cfg.capacity_kwh
    assert acts.count(Action.CHARGE) == -(-need // (cfg.ac_charge_power_kw * cfg.slot_h))  # ceil
    assert res.mode_command is ModeCommand.STAY and res.mode_switches_planned == 0


@pytest.mark.parametrize("preset", PRESETS)
def test_single_high_interval_exported_by_all_presets(preset, cfg, lookup):
    prices = [60] * 156
    prices[10] = 500
    res = plan(snap(prices), ModeState(), preset, cfg, lookup)
    assert [i for i, a in enumerate(actions(res)) if a is Action.EXPORT] == [10]
    assert res.full_plan[-1].soc_after >= cfg.target_soc_pct
    assert res.net_profit == pytest.approx(res.total_export_revenue - res.total_charge_cost)


def test_a_adjusted_price_below_naive(cfg, lookup):
    prices = [60] * 156
    prices[10] = 500
    naive = plan(snap(prices), ModeState(), "Naive", cfg, lookup)
    a = plan(snap(prices), ModeState(), "A", cfg, lookup)
    assert naive.full_plan[10].action is Action.EXPORT and a.full_plan[10].action is Action.EXPORT
    assert naive.full_plan[10].sell_price == 500
    assert a.full_plan[10].sell_price == 500 + Q["q50"] < naive.full_plan[10].sell_price
    assert a.total_export_revenue < naive.total_export_revenue


def test_soc_floor_blocks_export(cfg, lookup):
    prices = [60] * 156
    prices[0] = 5000                                  # first slot: no chance to charge before it
    res = plan(snap(prices, soc=20.9, half_width=0.5), ModeState(), "Naive", cfg, lookup)
    assert res.current_action is not Action.EXPORT
    assert any(o.rule == "soc_floor" and o.interval_end == T0 + SLOT for o in res.safety_overrides_applied)
    assert min(s.soc_after for s in res.full_plan) - 0.5 >= cfg.min_soc_pct - 1e-9


def test_departure_guarantee_forces_charge(cfg, lookup):
    # 40 slots to departure; 40 -> 80 % needs 39 AC slots; slot 0 is the most expensive
    prices = [9000] + [50] * 39
    res = plan(snap(prices, soc=40.0, half_width=0.5), ModeState(), "Naive", cfg, lookup)
    assert res.current_action is Action.CHARGE
    assert any(o.rule == "departure_guarantee" for o in res.safety_overrides_applied)


def test_departure_guarantee_not_fired_with_time_to_spare(cfg, lookup):
    prices = [9000] + [50] * 80
    res = plan(snap(prices, soc=40.0, half_width=0.5), ModeState(), "Naive", cfg, lookup)
    assert res.current_action is not Action.CHARGE
    assert not any(o.rule == "departure_guarantee" for o in res.safety_overrides_applied)


@pytest.mark.parametrize("preset", PRESETS)
def test_safe_mode_cancels_exports(preset, cfg, lookup):
    prices = [60] * 156
    prices[0] = prices[10] = 500
    res = plan(snap(prices, safe_mode=True), ModeState(), preset, cfg, lookup)
    assert Action.EXPORT not in actions(res)
    assert res.current_action is Action.CHARGE                 # 60 % < target
    assert {o.rule for o in res.safety_overrides_applied} >= {"safe_mode"}


def test_horizon_gap_never_exports_unknown(cfg, lookup):
    res = plan(snap([60] * 100, unknown_slots=56, soc=90.0), ModeState(), "Naive", cfg, lookup)
    assert all(s.action is not Action.EXPORT for s in res.full_plan[100:])
    assert all(s.sell_price is None for s in res.full_plan[100:])


def test_uncertain_soc_plans_from_lower_bound(cfg, lookup):
    res = plan(snap([60] * 156, soc=60.0, half_width=8.0), ModeState(), "Naive", cfg, lookup)
    assert res.soc_plan_start == 52.0
    assert actions(res).count(Action.CHARGE) == -(-((80 - 52) / 100 * 60) // (7.4 / 12))


def test_alternating_prices_no_chatter(cfg, lookup):
    prices = [500 if i % 2 == 0 else 60 for i in range(12)] + [60] * 144
    res = plan(snap(prices, soc=70.0), ModeState(), "Naive", cfg, lookup)
    exports = [i for i, a in enumerate(actions(res)) if a is Action.EXPORT]
    assert exports == list(range(0, 12, 2))
    assert res.mode_command is ModeCommand.SWITCH_TO_DC_BPT
    assert res.mode_switches_planned == 2          # into DC-BPT once, back to AC once


def test_dwell_blocks_immediate_switch_back(cfg, lookup):
    st = ModeState(DC_BPT, T0 - timedelta(minutes=5))       # switched 5 min ago
    res = plan(snap([60] * 156, soc=70.0), st, "Naive", cfg, lookup)
    assert res.mode_command is ModeCommand.STAY            # dwell 15 min not yet met
    st = ModeState(DC_BPT, T0 - timedelta(minutes=15))
    assert plan(snap([60] * 156, soc=70.0), st, "Naive", cfg, lookup).mode_command is ModeCommand.SWITCH_TO_AC


def test_export_blocked_by_dwell_in_ac(cfg, lookup):
    prices = [60] * 156
    prices[0] = 500
    st = ModeState(AC, T0 - timedelta(minutes=5))           # just switched back to AC
    res = plan(snap(prices), st, "Naive", cfg, lookup)
    assert res.current_action is Action.IDLE and res.mode_command is ModeCommand.STAY
    assert any(o.rule == "dwell_blocks_export" for o in res.safety_overrides_applied)


@pytest.mark.parametrize("peak,expect_export", [(100, False), (150, True)])
def test_profitability_drops_marginal_export(peak, expect_export, cfg, lookup):
    # break-even ~ $119/MWh: 0.833 kWh export vs two $60 AC charge slots (1.23 kWh) / eta
    prices = [60] * 156
    prices[10] = peak
    res = plan(snap(prices), ModeState(), "Naive", cfg, lookup)
    assert (Action.EXPORT in actions(res)) is expect_export
    if expect_export:
        assert res.total_export_revenue > 0


@pytest.mark.parametrize("margin,expect_export", [(20, True), (70, False)])
def test_minimum_margin(margin, expect_export, lookup):
    # sell 150, buy-back 60: 150 - 60/0.9 - 20 = 63.3 $/MWh
    cfg = load_decision_config(overrides={"planner": {"minimum_margin_per_mwh": margin}})
    prices = [60] * 156
    prices[10] = 150
    res = plan(snap(prices), ModeState(), "Naive", cfg, lookup)
    assert (Action.EXPORT in actions(res)) is expect_export


def test_minimum_margin_applies_to_surplus_exports(lookup):
    # SoC above target: export needs no buy-back but must still clear the margin
    # against the cheapest known buy-back (60): 90 - 66.7 - 20 = 3.3 < 20
    cfg = load_decision_config()
    prices = [60] * 156
    prices[10] = 90
    res = plan(snap(prices, soc=95.0), ModeState(), "Naive", cfg, lookup)
    assert Action.EXPORT not in actions(res)
    prices[10] = 150
    assert Action.EXPORT in actions(plan(snap(prices, soc=95.0), ModeState(), "Naive", cfg, lookup))
