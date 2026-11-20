"""Look up PD forecast-error quantiles by lead time and forecast price.

The table is built by ``python -m orchestrator.inputs.error_dist`` and stored in
``data/error_distributions.json`` as
``{lead_bin: {price_bin | "all": {q10, q25, q50, q75, q90, count, mean, std}}}``.

Groups below $300 store error = actual - forecast ($/MWh); groups at or above
$300 store ratio = actual / forecast. get_error_adjustment() always returns an
additive $/MWh adjustment, so a quantile forecast is
``forecast + get_error_adjustment(lead, forecast, q)`` either way.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Union

from .error_dist import ALL_PRICES, LEAD_BINS, PRICE_BINS, QUANTILES, bin_label

ORCHESTRATOR_DIR = Path(__file__).resolve().parent.parent

Quantile = Union[str, float]


def _quantile_key(q: Quantile) -> str:
    if isinstance(q, str):
        if q in QUANTILES:
            return q
    else:
        for k, v in QUANTILES.items():
            if abs(v - float(q)) < 1e-9:
                return k
    raise ValueError(f"quantile must be one of {list(QUANTILES)} or {list(QUANTILES.values())}, got {q!r}")


class ErrorLookup:
    def __init__(self, table: Dict, min_count: int = 100):
        self.table = table
        self.min_count = min_count

    @classmethod
    def from_file(cls, path: Union[str, Path], min_count: int = 100) -> "ErrorLookup":
        return cls(json.loads(Path(path).read_text()), min_count)

    def stats(self, lead_time_hours: float, forecast_price: float) -> Dict:
        """Stats dict used for these inputs. Out-of-range inputs clamp to the edge bins.

        Fallback order when a group has count < min_count:
          1. its lead-merged group (e.g. 300-1000 @ 0-12h), if that has >= min_count
          2. the lead bin's "all"-prices group
        """
        lb = bin_label(lead_time_hours, LEAD_BINS)
        pb = bin_label(forecast_price, PRICE_BINS)
        cell = self.table[lb][pb]
        if cell["count"] >= self.min_count:
            return cell
        merged = cell.get("merged")
        if merged is not None and merged["count"] >= self.min_count:
            return merged
        return self.table[lb][ALL_PRICES]

    def group(self, lead_time_hours: float, forecast_price: float) -> tuple:
        """(lead_bin, price_bin) of the group actually used, e.g. ("0-12h", "300-1000")."""
        lb = bin_label(lead_time_hours, LEAD_BINS)
        pb = bin_label(forecast_price, PRICE_BINS)
        s = self.stats(lead_time_hours, forecast_price)
        if s is self.table[lb][ALL_PRICES]:
            return lb, ALL_PRICES
        return s.get("lead_bin", lb), pb

    def get_error_adjustment(self, lead_time_hours: float, forecast_price: float,
                             quantile: Quantile) -> float:
        """Additive adjustment in $/MWh: adjusted = forecast + adjustment.

        Ratio groups (forecast >= $300) store actual/forecast; the adjustment is
        then forecast * (ratio_q - 1), i.e. adjusted = forecast * ratio_q.
        """
        s = self.stats(lead_time_hours, forecast_price)
        v = float(s[_quantile_key(quantile)])
        if s.get("kind", "absolute") == "ratio":
            return forecast_price * (v - 1.0)
        return v

    def adjusted_price(self, lead_time_hours: float, forecast_price: float, quantile: Quantile) -> float:
        return forecast_price + self.get_error_adjustment(lead_time_hours, forecast_price, quantile)


@lru_cache(maxsize=4)
def default_lookup(path: Optional[str] = None, min_count: Optional[int] = None) -> ErrorLookup:
    from .config import load_config
    cfg = load_config().raw.get("error_distributions", {})
    p = Path(path or cfg.get("path", "data/error_distributions.json"))
    if not p.is_absolute():
        p = ORCHESTRATOR_DIR / p
    return ErrorLookup.from_file(p, min_count if min_count is not None else cfg.get("min_count", 100))


def get_error_adjustment(lead_time_hours: float, forecast_price: float, quantile: Quantile) -> float:
    """Error quantile ($/MWh, actual - forecast) for this lead time and forecast price."""
    return default_lookup().get_error_adjustment(lead_time_hours, forecast_price, quantile)
