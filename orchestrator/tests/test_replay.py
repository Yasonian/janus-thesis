import json
from datetime import datetime, timedelta

import pytest

from orchestrator.inputs.replay import main, replay


def test_replay_steps_5_min(make_layer):
    start, end = datetime(2026, 1, 1, 4, 5), datetime(2026, 1, 1, 6, 0)
    snaps = list(replay(make_layer(departure="07:00"), start, end))
    assert len(snaps) == 24
    assert [s.t for s in snaps] == [start + timedelta(minutes=5 * k) for k in range(24)]
    assert all(s.forecast[-1].interval_end == datetime(2026, 1, 1, 7, 0) for s in snaps)
    json.dumps(snaps[0].to_dict())


def test_off_grid_t_rejected(make_layer):
    with pytest.raises(ValueError, match="grid"):
        make_layer().get_snapshot(datetime(2026, 1, 1, 4, 7))


def test_cli(tmp_path, pd_path, monkeypatch, capsys):
    monkeypatch.setenv("JANUS_AEMO_DATA_DIR", str(pd_path.parent))
    out = tmp_path / "snaps.jsonl"
    main(["--start", "2026-01-01T04:05", "--end", "2026-01-01T05:00",
          "--departure", "2026-01-02T08:00", "--out", str(out)])
    lines = out.read_text().splitlines()
    assert len(lines) == 12
    d = json.loads(lines[0])
    assert d["runs"]["PD"]["run_id"] == "2026010101"
    assert "horizon_gap" in d["active_flags"] and "p5min_unavailable" in d["active_flags"]
    assert "12 snapshots" in capsys.readouterr().out
