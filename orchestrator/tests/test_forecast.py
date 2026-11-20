from collections import Counter
from datetime import datetime, timedelta

import pytest

from orchestrator.inputs.forecast import _slots, assemble, mwh_to_kwh
from orchestrator.inputs.sources import ArchivePriceSource

from conftest import p5min_frame

D = datetime(2026, 1, 1)
CHECK_TIMES = [D.replace(hour=4, minute=5), D.replace(hour=4, minute=30), D.replace(hour=4, minute=35),
               D.replace(hour=12, minute=0), datetime(2026, 1, 15, 17, 55), datetime(2026, 1, 31, 23, 50)]


@pytest.mark.parametrize("t", CHECK_TIMES)
def test_no_look_ahead(make_layer, prices, t):
    snap = make_layer().get_snapshot(t)
    assert snap.forecast
    assert all(p.published <= t for p in snap.forecast)
    run = snap.runs["PD"]
    assert run.published <= t
    # and it is the latest such run: nothing else published in (run.published, t]
    assert not [r for r in prices.runs("PD") if run.published < r.published <= t]
    assert {p.run_id for p in snap.forecast} == {run.run_id}
    # horizon starts at the interval in progress
    assert snap.forecast[0].interval_end == t + timedelta(minutes=5)


def test_latest_run_selection_0410(make_layer):
    snap = make_layer().get_snapshot(D.replace(hour=4, minute=10))
    assert snap.runs["PD"].run_id == "2026010101"
    assert snap.runs["PD"].published == D.replace(hour=4, minute=2, second=30)


def test_run_published_exactly_at_t_is_available(prices):
    run = prices.latest_run("PD", D.replace(hour=4, minute=2, second=30))
    assert run is not None and run.run_id == "2026010101"
    assert prices.latest_run("PD", D.replace(hour=4, minute=2, second=29)) is None


def test_resampling_six_slots_per_30min(prices, config):
    run = prices.latest_run("PD", D.replace(hour=4, minute=10))
    slots, bad = _slots(run, config.thresholds)
    assert bad == 0
    per_native = Counter(p.native_interval_end for p in slots.values())
    assert len(per_native) == len(run.frame)
    assert set(per_native.values()) == {6}
    for p in slots.values():
        row = run.frame[run.frame["target"] == p.native_interval_end].iloc[0]
        assert p.rrp == row["rrp"]
        assert p.native_interval_end - timedelta(minutes=30) < p.interval_end <= p.native_interval_end


def test_resampling_in_snapshot_on_half_hour(make_layer):
    snap = make_layer().get_snapshot(D.replace(hour=5, minute=0))
    counts = Counter(p.native_interval_end for p in snap.forecast)
    assert set(counts.values()) == {6}
    ends = [p.interval_end for p in snap.forecast]
    assert ends == sorted(ends) and len(set(ends)) == len(ends)
    assert all(b - a == timedelta(minutes=5) for a, b in zip(ends, ends[1:]))


def test_partially_elapsed_interval_is_trimmed(make_layer):
    snap = make_layer().get_snapshot(D.replace(hour=4, minute=10))
    first = [p for p in snap.forecast if p.native_interval_end == D.replace(hour=4, minute=30)]
    assert [p.interval_end.minute for p in first] == [15, 20, 25, 30]


@pytest.mark.parametrize("t", CHECK_TIMES)
def test_lead_times_positive_and_from_lastchanged(make_layer, pd_frame, t):
    snap = make_layer().get_snapshot(t)
    run_rows = pd_frame[pd_frame["run_id"] == snap.runs["PD"].run_id]
    lastchanged = run_rows["published"].iloc[0].to_pydatetime()
    for p in snap.forecast:
        assert p.published == lastchanged
        assert p.lead_time == p.interval_end - lastchanged
        assert p.lead_time > timedelta(0)
        assert p.native_lead_time == p.native_interval_end - lastchanged


def test_horizon_gap_when_departure_beyond_forecast(make_layer):
    t = D.replace(hour=4, minute=10)
    dep = datetime(2026, 1, 2, 9, 0)    # run 2026010101 ends at 2026-01-02 04:00
    snap = make_layer(departure=dep.isoformat()).get_snapshot(t)
    assert snap.flags.horizon_gap and not snap.flags.missing_intervals
    assert len(snap.unknown) == 1
    u = snap.unknown[0]
    assert (u.start, u.end, u.reason) == (datetime(2026, 1, 2, 4, 0), dep, "beyond_horizon")
    assert snap.forecast[-1].interval_end == datetime(2026, 1, 2, 4, 0)


def test_no_gap_and_truncated_when_departure_inside(make_layer):
    snap = make_layer(departure="07:30").get_snapshot(D.replace(hour=4, minute=10))
    assert snap.departure == D.replace(hour=7, minute=30)
    assert not snap.flags.horizon_gap and snap.unknown == []
    assert snap.forecast[-1].interval_end == D.replace(hour=7, minute=30)


def test_departure_hhmm_resolves_after_session_start(make_layer):
    layer = make_layer(departure="03:00", start=D.replace(hour=4, minute=5))
    assert layer.departure == datetime(2026, 1, 2, 3, 0)


def test_mwh_to_kwh():
    assert mwh_to_kwh(100.0) == pytest.approx(0.1)


def test_p5min_merged_into_first_hour(pd_frame, make_layer):
    t = D.replace(hour=4, minute=10)
    p5 = p5min_frame(D.replace(hour=4, minute=5, second=40), D.replace(hour=4, minute=10), rrp=123.0)
    snap = make_layer(prices_=ArchivePriceSource(pd_frame, p5)).get_snapshot(t)
    near = [p for p in snap.forecast if p.interval_end <= t + timedelta(minutes=60)]
    far = [p for p in snap.forecast if p.interval_end > t + timedelta(minutes=60)]
    # P5MIN run covers slots ending 04:10..05:05 (04:10 has already ended); the
    # window runs to 05:10, which P5MIN lacks, so that one slot falls back to PD
    assert [p.source for p in near] == ["P5MIN"] * 11 + ["PD"]
    assert near[10].interval_end == D.replace(hour=5, minute=5)
    assert {p.source for p in far} == {"PD"}
    assert all(p.rrp == 123.0 for p in near[:11])
    assert snap.runs["P5MIN"].used and not snap.flags.p5min_stale


def test_invalid_prices_discarded_as_missing(pd_frame, make_layer, config):
    bad = pd_frame.copy()
    target = datetime(2026, 1, 1, 6, 0)
    bad.loc[(bad["run_id"] == "2026010101") & (bad["target"] == target), "rrp"] = 99999.0
    snap = make_layer(prices_=ArchivePriceSource(bad)).get_snapshot(D.replace(hour=4, minute=10))
    assert snap.flags.invalid_prices and snap.discarded_prices == {"PD": 1}
    assert snap.flags.missing_intervals and not snap.flags.horizon_gap
    assert [(u.start, u.end, u.reason) for u in snap.unknown] == [
        (target - timedelta(minutes=30), target, "missing")]


def test_assemble_rejects_future_run(prices, config):
    run = prices.latest_run("PD", D.replace(hour=5))
    with pytest.raises(ValueError, match="look-ahead"):
        assemble(D.replace(hour=4, minute=10), run, None, config.thresholds)


def test_departure_passed_flag(make_layer):
    layer = make_layer(departure="05:00")
    s = layer.get_snapshot(D.replace(hour=5, minute=0))
    assert s.flags.departure_passed and not s.flags.horizon_gap
    assert s.forecast[-1].interval_end == datetime(2026, 1, 2, 4, 0)
    assert not layer.get_snapshot(D.replace(hour=4, minute=55)).flags.departure_passed
