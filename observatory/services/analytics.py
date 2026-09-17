"""Statistics for the map explorer and station analysis pages.

Two different statistical controls are exposed, because they answer different questions:

* **Observation range** (``range=central90|central95``) filters the *readings*: only
  values between the lower and upper percentiles of the selected window are kept
  (e.g. central 95 % keeps P2.5–P97.5). Everything downstream — series, histogram and
  statistics — uses the filtered readings. The CSV files are never modified.
* **Mean confidence interval** (``ci=90|95|99``) describes uncertainty of each
  aggregated mean: mean ± t(n-1) · s / √n, drawn as the band around the line.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import storage, units

try:  # SciPy gives exact t quantiles; the fallback below is accurate to ~1e-3.
    from scipy import stats as _scipy_stats
except ImportError:  # pragma: no cover
    _scipy_stats = None

AGGREGATIONS = {"raw": None, "10min": "10min", "hourly": "1h", "6h": "6h", "daily": "1D"}
CI_LEVELS = {90: 0.90, 95: 0.95, 99: 0.99}
RANGES = {"all": None, "central90": 0.90, "central95": 0.95}
MAX_POINTS = 5000


def to_ms(values) -> list[int]:
    """Epoch milliseconds, independent of pandas' datetime resolution (ns/us/s)."""
    idx = pd.DatetimeIndex(values)
    return ((idx - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)).astype("int64").tolist()


def t_critical(level: float, df: int) -> float:
    if df < 1:
        return float("nan")
    if _scipy_stats is not None:
        return float(_scipy_stats.t.ppf(0.5 + level / 2, df))
    # Cornish–Fisher expansion of the t quantile from the normal quantile.
    from statistics import NormalDist
    z = NormalDist().inv_cdf(0.5 + level / 2)
    g1 = (z ** 3 + z) / 4
    g2 = (5 * z ** 5 + 16 * z ** 3 + 3 * z) / 96
    g3 = (3 * z ** 7 + 19 * z ** 5 + 17 * z ** 3 - 15 * z) / 384
    return z + g1 / df + g2 / df ** 2 + g3 / df ** 3


@dataclass
class Query:
    station: str
    measurement: str
    start: datetime | None = None
    end: datetime | None = None  # exclusive
    aggregation: str = "hourly"
    ci: int = 95
    obs_range: str = "all"

    @classmethod
    def from_request(cls, code: str, params) -> "Query":
        def parse(value, end=False):
            if not value:
                return None
            ts = datetime.fromisoformat(value)
            # A bare date as the end bound means "through the end of that day".
            if end and len(value) == 10:
                ts += timedelta(days=1)
            return ts

        aggregation = params.get("aggregation", "hourly")
        ci = int(params.get("ci", 95)) if str(params.get("ci", "95")).isdigit() else 95
        obs_range = params.get("range", "all")
        if aggregation not in AGGREGATIONS:
            raise ValueError(f"aggregation must be one of {', '.join(AGGREGATIONS)}")
        if ci not in CI_LEVELS:
            raise ValueError("ci must be 90, 95 or 99")
        if obs_range not in RANGES:
            raise ValueError("range must be all, central90 or central95")
        return cls(station=code, measurement=params.get("measurement", "PM2.5"),
                   start=parse(params.get("start")), end=parse(params.get("end"), end=True),
                   aggregation=aggregation, ci=ci, obs_range=obs_range)


def _clean(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) or math.isinf(x) else round(x, 4)


def load_window(q: Query) -> pd.DataFrame:
    """Readings for the query, in display units.

    Every figure this module produces — series, histogram, statistics, the map snapshot
    and the CSV export — comes through here, so converting once is enough to put
    temperatures in Celsius everywhere.
    """
    df = storage.read_series(q.station, q.measurement)
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if q.start:
        mask &= df["time"] >= pd.Timestamp(q.start)
    if q.end:
        mask &= df["time"] < pd.Timestamp(q.end)
    window = df.loc[mask & df["value"].notna(), ["time", "value", "unit"]]
    return units.convert(window, q.measurement)


def apply_observation_range(df: pd.DataFrame, obs_range: str) -> tuple[pd.DataFrame, dict]:
    share = RANGES[obs_range]
    info = {"mode": obs_range, "lower": None, "upper": None, "removed": 0}
    if share is None or df.empty:
        return df, info
    tail = (1 - share) / 2
    lower, upper = df["value"].quantile([tail, 1 - tail]).tolist()
    kept = df[(df["value"] >= lower) & (df["value"] <= upper)]
    info.update(lower=_clean(lower), upper=_clean(upper), removed=int(len(df) - len(kept)))
    return kept, info


def aggregate(df: pd.DataFrame, aggregation: str, ci: int) -> dict:
    level = CI_LEVELS[ci]
    if df.empty:
        return {"t": [], "mean": [], "lower": [], "upper": [], "n": [], "has_ci": False, "downsampled": False}
    rule = AGGREGATIONS[aggregation]
    if rule is None:
        step = max(1, math.ceil(len(df) / MAX_POINTS))
        sub = df.iloc[::step]
        values = sub["value"].round(4).tolist()
        return {"t": to_ms(sub["time"]), "mean": values,
                "lower": [None] * len(values), "upper": [None] * len(values),
                "n": [1] * len(values), "has_ci": False, "downsampled": step > 1}
    g = df.set_index("time")["value"].resample(rule)
    frame = pd.DataFrame({"mean": g.mean(), "std": g.std(ddof=1), "n": g.count()})
    frame = frame[frame["n"] > 0]
    tcrit = frame["n"].map(lambda n: t_critical(level, int(n) - 1))
    half = tcrit * frame["std"] / np.sqrt(frame["n"])
    lower, upper = frame["mean"] - half, frame["mean"] + half
    return {
        # Plot each mean at the middle of its interval (a daily mean sits at noon, not midnight).
        "t": to_ms(frame.index + pd.Timedelta(rule) / 2),
        "mean": [_clean(v) for v in frame["mean"]],
        "lower": [_clean(v) for v in lower],
        "upper": [_clean(v) for v in upper],
        "n": frame["n"].astype(int).tolist(),
        "has_ci": True,
        "downsampled": False,
    }


def histogram(values: pd.Series) -> dict:
    v = values.dropna().to_numpy()
    if v.size == 0:
        return {"edges": [], "counts": [], "bin_width": None}
    lo, hi = float(v.min()), float(v.max())
    if lo == hi:
        return {"edges": [lo - 0.5, lo + 0.5], "counts": [int(v.size)], "bin_width": 1.0}
    iqr = float(np.subtract(*np.percentile(v, [75, 25])))
    fd = 2 * iqr / (v.size ** (1 / 3)) if iqr > 0 else 0
    bins = int(np.clip(math.ceil((hi - lo) / fd) if fd else 20, 8, 60))
    # Round the bin width to a readable step (0.5, 1, 2, 2.5, 5 × 10^k).
    raw = (hi - lo) / bins
    mag = 10 ** math.floor(math.log10(raw))
    width = min((s * mag for s in (0.5, 1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    start = math.floor(lo / width) * width
    edges = np.arange(start, hi + width, width)
    if edges[-1] < hi:
        edges = np.append(edges, edges[-1] + width)
    counts, edges = np.histogram(v, bins=edges)
    return {"edges": [round(float(e), 4) for e in edges], "counts": counts.astype(int).tolist(), "bin_width": round(width, 6)}


def describe(df: pd.DataFrame, ci: int) -> dict:
    if df.empty:
        return {"count": 0}
    v = df["value"]
    n = int(v.size)
    mean, std = float(v.mean()), float(v.std(ddof=1)) if n > 1 else float("nan")
    half = t_critical(CI_LEVELS[ci], n - 1) * std / math.sqrt(n) if n > 1 else float("nan")
    q = v.quantile([0.05, 0.25, 0.5, 0.75, 0.95]).tolist()
    last = df.iloc[-1]
    return {
        "count": n, "mean": _clean(mean), "median": _clean(q[2]), "min": _clean(v.min()), "max": _clean(v.max()),
        "std": _clean(std), "p05": _clean(q[0]), "p25": _clean(q[1]), "p75": _clean(q[3]), "p95": _clean(q[4]),
        "mean_ci": [_clean(mean - half), _clean(mean + half)], "ci_level": ci,
        "skewness": _clean(v.skew()) if n > 2 else None,
        "first_time": df["time"].iloc[0].isoformat(), "last_time": last["time"].isoformat(),
        "latest": _clean(last["value"]),
    }


def analyse(q: Query) -> dict:
    window = load_window(q)
    unit = window["unit"].replace("", np.nan).dropna()
    filtered, range_info = apply_observation_range(window, q.obs_range)
    return {
        "station": q.station,
        "measurement": q.measurement,
        "unit": unit.iat[-1] if len(unit) else "",
        "query": {"start": q.start.isoformat() if q.start else None, "end": q.end.isoformat() if q.end else None,
                  "aggregation": q.aggregation, "ci": q.ci, "range": q.obs_range},
        "observation_range": range_info,
        "series": aggregate(filtered, q.aggregation, q.ci),
        "histogram": histogram(filtered["value"]) if len(filtered) else histogram(pd.Series(dtype=float)),
        "stats": describe(filtered, q.ci),
    }


def filtered_rows(q: Query) -> pd.DataFrame:
    window = load_window(q)
    filtered, _ = apply_observation_range(window, q.obs_range)
    return filtered


def extent(code: str, measurement: str) -> dict:
    df = storage.read_series(code, measurement)
    if df.empty:
        return {"min": None, "max": None}
    return {"min": df["time"].iat[0].isoformat(), "max": df["time"].iat[-1].isoformat()}


def station_snapshot(code: str, measurement: str, start: datetime | None, end: datetime | None) -> dict:
    """Small summary used for map popups."""
    q = Query(station=code, measurement=measurement, start=start, end=end)
    window = load_window(q)
    if window.empty:
        return {"count": 0, "latest": None, "latest_time": None, "mean": None}
    return {"count": int(len(window)), "latest": _clean(window["value"].iat[-1]),
            "latest_time": window["time"].iat[-1].isoformat(), "mean": _clean(window["value"].mean())}
