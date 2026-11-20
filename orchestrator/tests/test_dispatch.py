import warnings
from datetime import datetime, timedelta

import pandas as pd

from orchestrator.inputs.aemo_csv import DISPATCH_SPEC, load_table, read_cid_csv
from orchestrator.inputs.sources import ArchivePriceSource


def test_dispatch_nsw1_row_count(dispatch_path, dispatch_frame):
    with open(dispatch_path) as fh:
        expected = sum(1 for l in fh if l.startswith("D,") and ",NSW1," in l)
    assert expected == 8928 == 31 * 288
    assert len(dispatch_frame) == expected
    assert dispatch_frame["run_id"].isna().all()


def test_dispatch_columns_match_spec(dispatch_path):
    t = read_cid_csv(dispatch_path)[DISPATCH_SPEC.key]
    for col in (DISPATCH_SPEC.published_col, DISPATCH_SPEC.target_col, DISPATCH_SPEC.region_col,
                DISPATCH_SPEC.rrp_col, DISPATCH_SPEC.intervention_col):
        assert col in t.frame.columns
    assert set(t.frame["INTERVENTION"]) == {"0"}


def test_dispatch_spec_verified_no_warning(dispatch_path):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        load_table(dispatch_path, DISPATCH_SPEC, "NSW1")


def test_dispatch_timestamps_5_min_apart(dispatch_frame):
    targets = dispatch_frame["target"].sort_values()
    assert targets.is_unique
    assert (targets.diff().dropna() == pd.Timedelta(minutes=5)).all()
    assert targets.iloc[0] == datetime(2026, 1, 1, 0, 5)
    assert targets.iloc[-1] == datetime(2026, 2, 1, 0, 0)


def test_latest_dispatch_in_snapshot_no_lookahead(pd_frame, dispatch_frame, make_layer):
    layer = make_layer(prices_=ArchivePriceSource(pd_frame, dispatch_frame=dispatch_frame))
    for t in [datetime(2026, 1, 1, 4, 10), datetime(2026, 1, 15, 17, 55)]:
        s = layer.get_snapshot(t)
        row = dispatch_frame[dispatch_frame["target"] == s.latest_dispatch_interval].iloc[0]
        assert row["published"] <= t and s.latest_dispatch_rrp == row["rrp"]
        # the in-progress interval (t, t+5] is only published ~9 s after t
        assert s.latest_dispatch_interval == t
        s2 = layer.get_snapshot(t + timedelta(minutes=5))
        assert s2.latest_dispatch_interval == t + timedelta(minutes=5)


def test_mii_revised_price_hidden_until_revision(pd_frame, dispatch_frame, make_layer):
    # 2026-01-03 12:00 interval was revised (MII) and republished at 12:25:16
    layer = make_layer(prices_=ArchivePriceSource(pd_frame, dispatch_frame=dispatch_frame))
    s = layer.get_snapshot(datetime(2026, 1, 3, 12, 20))
    assert s.latest_dispatch_interval == datetime(2026, 1, 3, 11, 50)
