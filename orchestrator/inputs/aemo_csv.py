"""Generic reader for AEMO MMSDM "C/I/D" CSV files, plus per-table specs.

File format (verified on PUBLIC_ARCHIVE#PREDISPATCHPRICE#ALL#FILE01#202601010000.CSV):

    C,...                      comment rows (first and last row) -> skipped
    I,<report>,<subtype>,<ver>,COL_A,COL_B,...   header row for one table
    D,<report>,<subtype>,<ver>,val_a,val_b,...   data rows for that table

A file may in principle hold several I/D blocks (different tables); each I row
starts a new table keyed by (report, subtype). All values are read as strings
and converted by the table spec.

Timestamps in MMSDM are NEM time (AEST, UTC+10, no DST) and are returned as
naive ``datetime64`` values in that timezone. Interval timestamps are
interval-ENDING.
"""
from __future__ import annotations

import csv
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple

import pandas as pd

AEMO_TIME_FORMAT = "%Y/%m/%d %H:%M:%S"

TableKey = Tuple[str, str]  # (report, subtype), e.g. ("PREDISPATCH", "REGION_PRICES")


@dataclass(frozen=True)
class AemoTable:
    key: TableKey
    version: str
    frame: pd.DataFrame  # raw string columns, exactly as in the I row


def read_cid_csv(
    path: Path | str,
    tables: Optional[Iterable[TableKey]] = None,
    filters: Optional[Mapping[str, str]] = None,
) -> Dict[TableKey, AemoTable]:
    """Read an AEMO C/I/D CSV into one raw string DataFrame per table.

    ``tables`` restricts which (report, subtype) blocks are kept.
    ``filters`` keeps only D rows whose column equals the given string value
    (applied while reading, e.g. ``{"REGIONID": "NSW1"}``, to save memory).
    """
    wanted = set(tables) if tables is not None else None
    filters = dict(filters or {})
    headers: Dict[TableKey, Tuple[str, list]] = {}
    rows: Dict[TableKey, list] = {}
    filter_idx: Dict[TableKey, list] = {}

    with open(path, newline="") as fh:
        for rec in csv.reader(fh):
            if not rec:
                continue
            kind = rec[0]
            if kind == "C":
                continue
            key = (rec[1], rec[2])
            if wanted is not None and key not in wanted:
                continue
            if kind == "I":
                cols = rec[4:]
                headers[key] = (rec[3], cols)
                rows.setdefault(key, [])
                missing = [c for c in filters if c not in cols]
                if missing:
                    raise KeyError(f"filter columns {missing} not in table {key}")
                filter_idx[key] = [(cols.index(c), v) for c, v in filters.items()]
            elif kind == "D":
                if key not in headers:
                    raise ValueError(f"D row for {key} before its I row")
                vals = rec[4:]
                if all(vals[i] == v for i, v in filter_idx[key]):
                    rows[key].append(vals)
            else:
                raise ValueError(f"unknown AEMO row type {kind!r}")

    out = {}
    for key, (version, cols) in headers.items():
        out[key] = AemoTable(key, version, pd.DataFrame(rows[key], columns=cols, dtype=str))
    return out


# --------------------------------------------------------------------------- #
# Table specs: map each table's columns onto one canonical price frame.
#
# Canonical columns:
#   run_id     str             forecast run identifier (None for actuals)
#   published  datetime64      availability time (LASTCHANGED)
#   target     datetime64      interval-ending target time
#   region     str
#   rrp        float           $/MWh
# --------------------------------------------------------------------------- #

CANONICAL_COLUMNS = ["run_id", "published", "target", "region", "rrp"]


@dataclass(frozen=True)
class TableSpec:
    name: str                 # "PD" / "P5MIN" / "DISPATCH"
    key: TableKey
    run_id_col: Optional[str]
    published_col: str
    target_col: str
    interval_minutes: int
    region_col: str = "REGIONID"
    rrp_col: str = "RRP"
    intervention_col: Optional[str] = "INTERVENTION"
    verified: bool = False    # True only once tested against a real file


PD_SPEC = TableSpec(
    name="PD",
    key=("PREDISPATCH", "REGION_PRICES"),
    run_id_col="PREDISPATCHSEQNO",
    published_col="LASTCHANGED",
    target_col="DATETIME",       # PERIODID is steps-ahead, NOT a clock time
    interval_minutes=30,
    verified=True,
)

# STUB: column names from the MMSDM data model; not yet checked against a file.
P5MIN_SPEC = TableSpec(
    name="P5MIN",
    key=("P5MIN", "REGIONSOLUTION"),
    run_id_col="RUN_DATETIME",
    published_col="LASTCHANGED",
    target_col="INTERVAL_DATETIME",
    interval_minutes=5,
)

# Actual dispatch prices (not a forecast). Verified on
# PUBLIC_ARCHIVE#DISPATCHPRICE#FILE01#202601010000.CSV. LASTCHANGED is ~9 s after
# the interval STARTS; prices revised for manifestly incorrect inputs
# (MII_STATUS = MII_PRICE_ACCEPTED) carry the later revision time instead.
DISPATCH_SPEC = TableSpec(
    name="DISPATCH",
    key=("DISPATCH", "PRICE"),
    run_id_col=None,
    published_col="LASTCHANGED",
    target_col="SETTLEMENTDATE",
    interval_minutes=5,
    verified=True,
)

SPECS = {s.name: s for s in (PD_SPEC, P5MIN_SPEC, DISPATCH_SPEC)}


def normalise(table: AemoTable, spec: TableSpec, intervention: str = "0") -> pd.DataFrame:
    """Convert a raw table to the canonical frame, keeping INTERVENTION == 0 only."""
    raw = table.frame
    if spec.intervention_col is not None:
        raw = raw[raw[spec.intervention_col] == intervention]
    out = pd.DataFrame(
        {
            "run_id": raw[spec.run_id_col] if spec.run_id_col else None,
            "published": pd.to_datetime(raw[spec.published_col], format=AEMO_TIME_FORMAT),
            "target": pd.to_datetime(raw[spec.target_col], format=AEMO_TIME_FORMAT),
            "region": raw[spec.region_col],
            "rrp": pd.to_numeric(raw[spec.rrp_col], errors="coerce"),
        }
    )
    return out.sort_values(["published", "target"]).reset_index(drop=True)


def load_table(path: Path | str, spec: TableSpec, region: str) -> pd.DataFrame:
    """Read one AEMO file and return the canonical frame for ``spec`` and ``region``."""
    if not spec.verified:
        warnings.warn(
            f"{spec.name} table spec has not been verified against a real AEMO file",
            stacklevel=2,
        )
    tables = read_cid_csv(path, tables=[spec.key], filters={spec.region_col: region})
    if spec.key not in tables:
        raise ValueError(f"{path}: no {spec.key} table found")
    return normalise(tables[spec.key], spec)


def load_tables(paths: Iterable[Path | str], spec: TableSpec, region: str) -> pd.DataFrame:
    """Load and concatenate several files (e.g. consecutive monthly archives)."""
    frames = [load_table(p, spec, region) for p in paths]
    if not frames:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(["run_id", "published", "target"]).sort_values(
        ["published", "target"], ignore_index=True
    )
