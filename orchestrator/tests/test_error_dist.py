import json
from datetime import datetime

import pandas as pd
import pytest

from orchestrator.inputs.error_dist import (ALL_PRICES, LEAD_BINS, PRICE_BINS, RATIO_BINS,
                                            actual_30min, bin_label, build_pairs, build_table)
from orchestrator.inputs.error_lookup import ErrorLookup, default_lookup, get_error_adjustment

LEADS = [b[0] for b in LEAD_BINS]
PRICES = [b[0] for b in PRICE_BINS]


@pytest.fixture(scope="module")
def pairs(pd_frame, dispatch_frame):
    return build_pairs(pd_frame, dispatch_frame)


@pytest.fixture(scope="module")
def table(pairs):
    return build_table(pairs)


# representative forecast used to express ratio spreads in $/MWh
REP_PRICE = {"300-1000": 650.0, ">1000": 1000.0}


def spread(g, pb=None):
    """q90 - q10 in $/MWh (ratio groups scaled by a representative forecast)."""
    s = g["q90"] - g["q10"]
    return s * REP_PRICE[pb] if g.get("kind") == "ratio" else s


def test_ratio_kind_only_above_300(table):
    for lb in LEADS:
        for pb in PRICES:
            assert table[lb][pb]["kind"] == ("ratio" if pb in RATIO_BINS else "absolute")
        assert table[lb][ALL_PRICES]["kind"] == "absolute"


def test_pair_count(pairs, pd_frame):
    assert len(pd_frame) == 82584
    # dropped: PD targets after the last complete dispatch interval (2026-02-01 00:00)
    beyond = (pd_frame["target"] > datetime(2026, 2, 1, 0, 0)).sum()
    assert len(pairs) == len(pd_frame) - beyond == 80492


def test_actual_is_mean_of_six(dispatch_frame, pairs):
    act = actual_30min(dispatch_frame)
    d = dispatch_frame.set_index("target")["rrp"]
    six = d[datetime(2026, 1, 1, 4, 5):datetime(2026, 1, 1, 4, 30)]
    assert len(six) == 6
    assert act[datetime(2026, 1, 1, 4, 30)] == pytest.approx(six.mean())
    row = pairs[(pairs["run_id"] == "2026010101") & (pairs["target"] == datetime(2026, 1, 1, 4, 30))].iloc[0]
    assert row["error"] == pytest.approx(six.mean() - 56.98)


def test_incomplete_intervals_dropped(dispatch_frame):
    act = actual_30min(dispatch_frame.iloc[1:])      # remove 00:05 -> 00:30 interval incomplete
    assert datetime(2026, 1, 1, 0, 30) not in act.index
    assert datetime(2026, 1, 1, 1, 0) in act.index


def test_lead_times_positive(pairs):
    assert (pairs["lead_h"] > 0).all()
    assert pairs["lead_h"].min() == pytest.approx(25.666 / 60, abs=1e-3)


def test_table_shape_and_counts(table, pairs):
    assert set(table) == set(LEADS)
    for lb in LEADS:
        assert set(table[lb]) == set(PRICES) | {ALL_PRICES}
        assert sum(table[lb][pb]["count"] for pb in PRICES) == table[lb][ALL_PRICES]["count"]
    assert sum(table[lb][ALL_PRICES]["count"] for lb in LEADS) == len(pairs)


def test_short_lead_normal_price_median_near_zero(table):
    assert abs(table["0-2h"]["0-100"]["q50"]) < 5.0


@pytest.mark.parametrize("pb", ["0-100", "100-300"])   # well-populated price bins
def test_spread_wider_for_longer_leads(table, pb):
    short = spread(table["0-2h"][pb])
    for lb in LEADS[1:]:
        assert spread(table[lb][pb]) > short


@pytest.mark.parametrize("lb", LEADS)
def test_spread_wider_for_higher_prices(table, lb):
    # the <0 bin is excluded: its spread is close to the 0-100 bin's and not ordered.
    # Ratio bins are compared in $/MWh at REP_PRICE.
    s = [spread(table[lb][pb], pb) for pb in PRICES[1:]]
    assert s == sorted(s) and len(set(s)) == len(s)


def test_saved_table_matches_rebuild(table):
    lk = default_lookup()
    for lb in LEADS:
        for pb in PRICES:
            assert lk.table[lb][pb]["count"] == table[lb][pb]["count"]
            assert lk.table[lb][pb]["q50"] == pytest.approx(table[lb][pb]["q50"])
            assert lk.table[lb][pb]["kind"] == table[lb][pb]["kind"]


def test_lookup_known_bin():
    lk = default_lookup()
    g = lk.table["6-12h"]["100-300"]
    assert g["count"] >= 100
    assert get_error_adjustment(8.0, 150.0, "q90") == g["q90"]
    assert get_error_adjustment(8.0, 150.0, 0.25) == g["q25"]
    # bin edges are [lo, hi)
    assert lk.group(2.0, 100.0) == ("2-6h", "100-300")
    assert lk.group(1.999, 99.99) == ("0-2h", "0-100")


def test_merged_groups_pool_members(table, pairs):
    for pb, label, members in [("300-1000", "0-12h", ["0-2h", "2-6h", "6-12h"]),
                               (">1000", "0-6h", ["0-2h", "2-6h"])]:
        m = table[members[0]][pb]["merged"]
        assert m["lead_bin"] == label and m["members"] == members
        assert m["count"] == sum(table[lb][pb]["count"] for lb in members) >= 100
        sel = pairs[(pairs["price_bin"] == pb) & pairs["lead_bin"].isin(members)]["ratio"]
        assert m["kind"] == "ratio" and m["q50"] == pytest.approx(sel.quantile(0.5))
        assert all(table[lb][pb]["merged"] == m for lb in members)
    assert "merged" not in table["12-24h"]["300-1000"] and "merged" not in table["6-12h"][">1000"]


@pytest.mark.parametrize("lead,price,expect", [
    (1.0, 500.0, ("0-12h", "300-1000")),     # 6 pairs -> merged
    (3.0, 500.0, ("0-12h", "300-1000")),     # 52 pairs -> merged
    (8.0, 500.0, ("6-12h", "300-1000")),     # 103 pairs: not thin, keeps its own group
    (1.0, 5000.0, ("0-6h", ">1000")),        # 34 -> merged
    (3.0, 5000.0, ("0-6h", ">1000")),        # 77 -> merged
    (8.0, 5000.0, ("6-12h", ">1000")),
])
def test_lookup_thin_group_uses_merged_group(lead, price, expect):
    lk = default_lookup()
    assert lk.group(lead, price) == expect
    lb = bin_label(lead, LEAD_BINS)
    pb = bin_label(price, PRICE_BINS)
    cell = lk.table[lb][pb]
    want = cell if cell["count"] >= 100 else cell["merged"]
    # ratio groups: adjustment = forecast * (ratio - 1)
    assert lk.get_error_adjustment(lead, price, "q50") == pytest.approx(price * (want["q50"] - 1))
    assert lk.adjusted_price(lead, price, "q50") == pytest.approx(price * want["q50"])


def test_lookup_falls_back_to_all_prices_when_merged_still_thin():
    lk = ErrorLookup(default_lookup().table, min_count=200)
    # 0-12h/300-1000 merged = 161 < 200 -> lead bin's all-price group
    assert lk.group(1.0, 500.0) == ("0-2h", ALL_PRICES)
    assert lk.get_error_adjustment(1.0, 500.0, "q90") == lk.table["0-2h"][ALL_PRICES]["q90"]
    # with the threshold lowered the group's own value is used
    lk5 = ErrorLookup(lk.table, min_count=5)
    assert lk5.get_error_adjustment(1.0, 500.0, "q50") == pytest.approx(
        500.0 * (lk.table["0-2h"]["300-1000"]["q50"] - 1))


def test_summary_csv_has_merged_rows():
    from orchestrator.inputs.error_lookup import ORCHESTRATOR_DIR
    s = pd.read_csv(ORCHESTRATOR_DIR / "data" / "error_distributions_summary.csv")
    assert (s["group"] == "original").sum() == 25
    assert set(s.loc[s["price_bin"].isin(RATIO_BINS), "error_kind"]) == {"ratio"}
    assert set(s.loc[~s["price_bin"].isin(RATIO_BINS), "error_kind"]) == {"absolute"}
    m = s[s["group"] == "merged"].set_index(["lead_bin", "price_bin"])
    assert set(m.index) == {("0-12h", "300-1000"), ("0-6h", ">1000")}
    assert m.loc[("0-12h", "300-1000"), "count"] == 161 and m.loc[("0-6h", ">1000"), "count"] == 111


def test_lookup_clamps_out_of_range():
    lk = default_lookup()
    assert lk.group(-1.0, 50.0) == ("0-2h", "0-100")       # negative lead -> first bin
    assert lk.group(500.0, 50.0) == ("24h+", "0-100")
    assert lk.group(30.0, -5000.0) == ("24h+", "<0")
    assert lk.group(30.0, 1e9)[0] == "24h+"
    assert lk.get_error_adjustment(-1.0, 50.0, "q10") == lk.table["0-2h"]["0-100"]["q10"]


def test_lookup_rejects_unknown_quantile():
    with pytest.raises(ValueError):
        get_error_adjustment(1.0, 50.0, 0.33)
