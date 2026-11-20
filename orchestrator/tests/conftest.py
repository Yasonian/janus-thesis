import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from orchestrator.inputs.aemo_csv import PD_SPEC, load_table
from orchestrator.inputs.config import load_config
from orchestrator.inputs.snapshot import InputLayer
from orchestrator.inputs.sources import ArchivePriceSource

PD_FILE = "PUBLIC_ARCHIVE#PREDISPATCHPRICE#ALL#FILE01#202601010000.CSV"


def data_dir() -> Path:
    return Path(os.environ.get("JANUS_AEMO_DATA_DIR", "~/everest-workspace/data/aemo")).expanduser()


@pytest.fixture(scope="session")
def pd_path() -> Path:
    p = data_dir() / PD_FILE
    if not p.exists():
        pytest.skip(f"real PD file not found: {p} (set JANUS_AEMO_DATA_DIR)")
    return p


@pytest.fixture(scope="session")
def pd_frame(pd_path) -> pd.DataFrame:
    return load_table(pd_path, PD_SPEC, "NSW1")


@pytest.fixture(scope="session")
def prices(pd_frame) -> ArchivePriceSource:
    return ArchivePriceSource(pd_frame)


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def make_layer(config, prices):
    def _make(prices_=None, departure=None, start=datetime(2026, 1, 1, 4, 5), soc_source=None,
              config_=None, **user_kw):
        cfg = config_ or config
        user = cfg.user.with_overrides(departure_time=departure, **user_kw)
        return InputLayer(cfg, prices_ or prices, soc_source, user, session_start=start)
    return _make


def p5min_frame(published: datetime, first_target: datetime, rrp=100.0, n=12, run_id=None):
    """Synthetic P5MIN run in canonical form (no real P5MIN file yet)."""
    targets = [first_target + timedelta(minutes=5 * k) for k in range(n)]
    return pd.DataFrame({
        "run_id": run_id or published.strftime("%Y/%m/%d %H:%M:00"),
        "published": [published] * n,
        "target": targets,
        "region": "NSW1",
        "rrp": [rrp] * n if not isinstance(rrp, list) else rrp,
    })


DISPATCH_FILE = "PUBLIC_ARCHIVE#DISPATCHPRICE#FILE01#202601010000.CSV"


@pytest.fixture(scope="session")
def dispatch_path() -> Path:
    p = data_dir() / DISPATCH_FILE
    if not p.exists():
        pytest.skip(f"real DISPATCHPRICE file not found: {p} (set JANUS_AEMO_DATA_DIR)")
    return p


@pytest.fixture(scope="session")
def dispatch_frame(dispatch_path) -> pd.DataFrame:
    from orchestrator.inputs.aemo_csv import DISPATCH_SPEC
    return load_table(dispatch_path, DISPATCH_SPEC, "NSW1")
