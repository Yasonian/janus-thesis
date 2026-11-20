from datetime import datetime, timedelta

import pytest

from orchestrator.inputs.config import load_config
from orchestrator.inputs.sources import ArchivePriceSource

from conftest import p5min_frame

D = datetime(2026, 1, 1)
# PD runs on 1 Jan: 01 @04:02:30, 02 @04:32:16, 03 @05:02:16, 04 @05:32:17, 05 @06:02:47


def at(h, m):
    return D.replace(hour=h, minute=m)


def test_fresh_pd_not_stale(make_layer):
    f = make_layer().get_snapshot(at(5, 5)).flags
    assert not f.pd_stale and not f.safe_mode and not f.pd_unavailable
    assert f.p5min_unavailable          # no P5MIN files yet
    assert not f.p5min_stale


def test_pd_stale_at_45_min(prices, make_layer):
    src = prices.without_runs("PD", ["2026010103"])  # latest becomes 04:32:16
    layer = make_layer(prices_=src)
    s = layer.get_snapshot(at(5, 15))                # age 42.7 min
    assert s.runs["PD"].run_id == "2026010102" and not s.flags.pd_stale
    s = layer.get_snapshot(at(5, 20))                # age 47.7 min
    assert s.flags.pd_stale and not s.flags.safe_mode
    assert s.forecast and {p.run_id for p in s.forecast} == {"2026010102"}  # still used


def test_pd_stale_threshold_from_config(prices, make_layer):
    cfg = load_config(overrides={"staleness": {"pd_max_age_minutes": 50}})
    layer = make_layer(prices_=prices.without_runs("PD", ["2026010103"]), config_=cfg)
    assert not layer.get_snapshot(at(5, 20)).flags.pd_stale   # 47.7 < 50
    assert layer.get_snapshot(at(5, 25)).flags.pd_stale       # 52.7 > 50


def test_safe_mode_at_60_min(prices, make_layer):
    src = prices.without_runs("PD", ["2026010103", "2026010104", "2026010105"])
    layer = make_layer(prices_=src)
    s = layer.get_snapshot(at(5, 30))   # 04:32:16 -> 57.7 min
    assert s.flags.pd_stale and not s.flags.safe_mode
    s = layer.get_snapshot(at(5, 35))   # 62.7 min
    assert s.flags.pd_stale and s.flags.safe_mode
    assert s.forecast                   # data still reported; decision logic decides


def test_safe_mode_before_first_run(make_layer):
    s = make_layer(start=at(0, 0)).get_snapshot(at(4, 0))
    f = s.flags
    assert f.pd_unavailable and f.p5min_unavailable and f.safe_mode
    assert s.forecast == [] and s.runs["PD"] is None


def test_p5min_stale_falls_back_to_pd(pd_frame, make_layer):
    p5 = p5min_frame(at(4, 5) + timedelta(seconds=40), at(4, 10))
    layer = make_layer(prices_=ArchivePriceSource(pd_frame, p5))
    s = layer.get_snapshot(at(4, 15))   # age 9.3 min -> used
    assert not s.flags.p5min_stale and s.runs["P5MIN"].used
    s = layer.get_snapshot(at(4, 20))   # age 14.3 min -> stale, PD everywhere
    assert s.flags.p5min_stale and not s.runs["P5MIN"].used
    assert {p.source for p in s.forecast} == {"PD"}


def test_fresh_p5min_keeps_safe_mode_off(pd_frame, make_layer):
    src = ArchivePriceSource(pd_frame, p5min_frame(at(5, 30), at(5, 35)))
    src = src.without_runs("PD", ["2026010103", "2026010104", "2026010105"])
    s = make_layer(prices_=src).get_snapshot(at(5, 35))
    assert s.flags.pd_stale and not s.flags.safe_mode
