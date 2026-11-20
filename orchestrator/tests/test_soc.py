from datetime import datetime, timedelta

import pytest

from orchestrator.inputs.soc import estimate_soc
from orchestrator.inputs.sources import ReplaySocSource, SocSample

T0 = datetime(2026, 1, 1, 4, 5)


def mins(m):
    return T0 + timedelta(minutes=m)


@pytest.fixture
def est(config):
    def _est(samples, **user_kw):
        user = config.user.with_overrides(**user_kw)
        return estimate_soc(samples, user, config.soc, config.thresholds)
    return _est


def test_direct_reading_range(est, config):
    e = est([SocSample(T0, "DC", 0.0, 55.0)])
    tol = config.soc.direct_tolerance_pct
    assert (e.lower, e.upper, e.method) == (55 - tol, 55 + tol, "direct")
    assert not e.uncertain


def test_ac_estimate_widens_then_dc_resets(est, config):
    cap = config.user.battery_capacity_kwh          # 60 kWh
    samples = [SocSample(T0, "DC", 0.0, 40.0)]
    widths, mids = [], []
    for k in range(1, 9):                            # 8 x 3 kWh in AC, no readings
        samples.append(SocSample(mins(5 * k), "AC", 3.0 * k))
        e = est(samples)
        widths.append(e.width)
        mids.append(e.mid)
        assert e.method == "meter_estimate"
    assert all(b > a for a, b in zip(widths, widths[1:]))
    eff = config.soc.ac_charge_efficiency
    assert mids[-1] == pytest.approx(40 + 100 * 24 * eff / cap)
    assert widths[-1] == pytest.approx(2 * (config.soc.direct_tolerance_pct
                                            + config.soc.ac_uncertainty_fraction * 100 * 24 / cap))
    assert widths[-1] == pytest.approx(10.0) and not est(samples).uncertain  # threshold is strict >
    samples.append(SocSample(mins(45), "AC", 25.0))
    assert est(samples).uncertain

    samples.append(SocSample(mins(50), "DC", 24.0, 81.0))
    e = est(samples)
    assert e.method == "direct" and e.width == pytest.approx(2 * config.soc.direct_tolerance_pct)
    assert e.mid == 81.0 and e.anchor_time == mins(50) and not e.uncertain


def test_discharge_moves_mid_down_and_still_widens(est, config):
    s = [SocSample(T0, "DC", 10.0, 70.0), SocSample(mins(5), "DC", 4.0)]   # 6 kWh out
    e = est(s)
    eff, cap = config.soc.ac_charge_efficiency, config.user.battery_capacity_kwh
    assert e.mid == pytest.approx(70 - 100 * 6 / eff / cap) and e.width > 2.0


def test_user_entry_when_no_reading(est, config):
    e = est([SocSample(T0, "AC", 0.0)], user_soc=30.0)
    tol = config.soc.user_entry_tolerance_pct
    assert (e.lower, e.upper, e.method) == (30 - tol, 30 + tol, "user_entry")


def test_no_reading_no_user_uses_min_soc(est):
    e = est([SocSample(T0, "AC", 0.0)], min_soc=20.0, user_soc=None)
    assert (e.lower, e.method) == (20.0, "assumed_minimum") and e.upper == 100.0
    assert e.uncertain


def test_invalid_reading_discarded(est):
    e = est([SocSample(T0, "DC", 0.0, 50.0), SocSample(mins(5), "DC", 0.0, 140.0)])
    assert e.mid == 50.0 and e.invalid_readings == 1
    e = est([SocSample(T0, "DC", 0.0, -3.0)], user_soc=None)
    assert e.method == "assumed_minimum" and e.invalid_readings == 1


def test_uncertain_threshold_configurable(config):
    from orchestrator.inputs.config import load_config
    s = [SocSample(T0, "DC", 0.0, 50.0), SocSample(mins(5), "AC", 12.0)]  # half = 1 + 2 = 3
    for width_thr, expect in [(10.0, False), (5.0, True)]:
        cfg = load_config(overrides={"soc": {"uncertain_width_pct": width_thr}})
        assert estimate_soc(s, cfg.user, cfg.soc, cfg.thresholds).uncertain is expect


def test_efficiency_applied_to_ac_charging(config):
    from orchestrator.inputs.config import load_config
    s = [SocSample(T0, "DC", 0.0, 20.0), SocSample(mins(5), "AC", 6.0)]   # 6 kWh metered in
    for eff, mid in [(0.90, 29.0), (1.0, 30.0)]:
        cfg = load_config(overrides={"soc": {"ac_charge_efficiency": eff}})
        assert estimate_soc(s, cfg.user, cfg.soc, cfg.thresholds).mid == pytest.approx(mid)


def test_range_clipped_to_0_100(est):
    e = est([SocSample(T0, "DC", 0.0, 99.5)])
    assert e.upper == 100.0


def test_snapshot_soc_flags_and_no_lookahead(make_layer, config):
    src = ReplaySocSource([
        SocSample(mins(0), "DC", 0.0, 20.0),
        SocSample(mins(5), "AC", 40.0),          # 40 kWh -> half 1 + 6.67, width 15.3
        SocSample(mins(10), "DC", 40.0, 200.0),  # invalid -> ignored
        SocSample(mins(15), "DC", 40.0, 85.0),
    ])
    layer = make_layer(soc_source=src)
    s = layer.get_snapshot(mins(0) - timedelta(minutes=5))   # before any sample
    assert s.soc.method == "assumed_minimum"
    s = layer.get_snapshot(mins(5))
    assert s.soc.method == "meter_estimate" and s.flags.soc_uncertain
    s = layer.get_snapshot(mins(10))
    assert s.flags.soc_invalid_reading and s.soc.mid == pytest.approx(20 + 100 * 40 * config.soc.ac_charge_efficiency / 60)
    s = layer.get_snapshot(mins(15))
    assert s.soc.method == "direct" and not s.flags.soc_uncertain


def test_soc_csv_replay(tmp_path):
    f = tmp_path / "soc.csv"
    f.write_text("time,mode,meter_energy_kwh,soc_reading\n"
                 "2026-01-01T04:05,DC,0,42\n2026-01-01T04:10,ac,1.5,\n")
    src = ReplaySocSource.from_csv(f)
    assert len(src.samples_until(mins(5))) == 2 and src.samples_until(mins(4))[0].soc_reading == 42


def test_overfull_estimate_clamps_to_100(est):
    # KNOWN LIMITATION: estimate above 100 % clamps both bounds -> zero width.
    e = est([SocSample(T0, "DC", 0.0, 50.0), SocSample(mins(5), "AC", 40.0)])
    assert (e.lower, e.upper) == (100.0, 100.0)
