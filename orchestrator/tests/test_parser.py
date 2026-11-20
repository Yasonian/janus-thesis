import warnings

import pytest

from orchestrator.inputs.aemo_csv import (DISPATCH_SPEC, P5MIN_SPEC, PD_SPEC, load_table,
                                          normalise, read_cid_csv)


def _count_lines(path, pred):
    with open(path) as fh:
        return sum(1 for line in fh if pred(line))


def test_nsw1_row_count_matches_raw_file(pd_path, pd_frame):
    expected = _count_lines(pd_path, lambda l: l.startswith("D,") and ",NSW1," in l)
    assert expected == 82584
    assert len(pd_frame) == expected


def test_c_and_i_rows_skipped(pd_path):
    tables = read_cid_csv(pd_path)
    assert list(tables) == [("PREDISPATCH", "REGION_PRICES")]
    t = tables[("PREDISPATCH", "REGION_PRICES")]
    assert t.version == "2"
    assert len(t.frame) == _count_lines(pd_path, lambda l: l.startswith("D,")) == 412920
    assert t.frame.columns[0] == "PREDISPATCHSEQNO"
    # no header or comment content leaked into the data
    assert not (t.frame["PREDISPATCHSEQNO"] == "PREDISPATCHSEQNO").any()
    assert not t.frame.apply(lambda c: c.str.contains("END OF REPORT", na=False)).any().any()


def test_canonical_columns_and_types(pd_frame):
    assert list(pd_frame.columns) == ["run_id", "published", "target", "region", "rrp"]
    assert (pd_frame["region"] == "NSW1").all()
    assert pd_frame["rrp"].notna().all()
    assert pd_frame.groupby("run_id")["published"].nunique().max() == 1
    assert pd_frame["run_id"].nunique() == 1488


def test_intervention_filter_real_file(pd_path):
    raw = read_cid_csv(pd_path, filters={"REGIONID": "NSW1"})[PD_SPEC.key]
    # Real Jan-2026 file: every row has INTERVENTION == 0, so the filter keeps everything.
    assert set(raw.frame["INTERVENTION"]) == {"0"}
    assert len(normalise(raw, PD_SPEC)) == len(raw.frame)


SYNTH_PD = """C,SETP.WORLD,DVD_PREDISPATCHPRICE,AEMO,PUBLIC,2026/02/09,16:38:52,1,MONTHLY_ARCHIVE,1
I,PREDISPATCH,REGION_PRICES,2,PREDISPATCHSEQNO,RUNNO,REGIONID,PERIODID,INTERVENTION,RRP,LASTCHANGED,DATETIME
D,PREDISPATCH,REGION_PRICES,2,2026010101,1,NSW1,01,0,50,"2026/01/01 04:02:30","2026/01/01 04:30:00"
D,PREDISPATCH,REGION_PRICES,2,2026010101,1,NSW1,01,1,999,"2026/01/01 04:02:30","2026/01/01 04:30:00"
D,PREDISPATCH,REGION_PRICES,2,2026010101,1,NSW1,02,0,60,"2026/01/01 04:02:30","2026/01/01 05:00:00"
D,PREDISPATCH,REGION_PRICES,2,2026010101,1,VIC1,01,0,70,"2026/01/01 04:02:30","2026/01/01 04:30:00"
C,"END OF REPORT",7
"""


def test_intervention_filter_synthetic(tmp_path):
    f = tmp_path / "pd.csv"
    f.write_text(SYNTH_PD)
    df = load_table(f, PD_SPEC, "NSW1")
    assert len(df) == 2
    assert 999 not in df["rrp"].tolist()


MULTI = """C,header
I,P5MIN,REGIONSOLUTION,9,RUN_DATETIME,INTERVENTION,INTERVAL_DATETIME,REGIONID,RRP,LASTCHANGED
D,P5MIN,REGIONSOLUTION,9,"2026/01/01 04:05:00",0,"2026/01/01 04:10:00",NSW1,80,"2026/01/01 04:05:40"
D,P5MIN,REGIONSOLUTION,9,"2026/01/01 04:05:00",0,"2026/01/01 04:15:00",NSW1,81,"2026/01/01 04:05:40"
I,DISPATCH,PRICE,5,SETTLEMENTDATE,RUNNO,REGIONID,INTERVENTION,RRP,LASTCHANGED
D,DISPATCH,PRICE,5,"2026/01/01 04:05:00",1,NSW1,0,77,"2026/01/01 04:05:30"
C,"END OF REPORT",6
"""


def test_generic_parser_multi_table_and_stub_specs(tmp_path):
    f = tmp_path / "multi.csv"
    f.write_text(MULTI)
    tables = read_cid_csv(f)
    assert set(tables) == {P5MIN_SPEC.key, DISPATCH_SPEC.key}
    with pytest.warns(UserWarning, match="not been verified"):
        p5 = load_table(f, P5MIN_SPEC, "NSW1")
    assert p5["rrp"].tolist() == [80, 81]
    assert p5["run_id"].iloc[0] == "2026/01/01 04:05:00"
    dp = load_table(f, DISPATCH_SPEC, "NSW1")   # verified spec: no warning
    assert dp["run_id"].isna().all() and dp["rrp"].iloc[0] == 77


def test_pd_spec_does_not_warn(pd_path):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        load_table(pd_path, PD_SPEC, "SA1")
