"""Statistics for the map explorer and station analysis pages.

Two different statistical controls are exposed, because they answer different questions:

* **Observation range** (``range=central90|central95``) filters the *readings*: only
  values between the lower and upper percentiles of the selected window are kept
  (e.g. central 95 % keeps P2.5–P97.5). Everything downstream — series, histogram and
  statistics — uses the filtered readings. The CSV files are never modified.
* **Mean confidence interval** (``ci=90|95|99``) describes uncertainty of each
  aggregated mean: mean ± t(n-1) · s / √n, drawn as the band around the line.

:func:`track` answers a different question again: *where* the sensor was. A mobile
recording stores a coordinate with every reading, so its readings are a path rather than
a point, and the map explorer draws it coloured by the value measured along the way.
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
TRACK_MAX_POINTS = 4000

# The bands a track is coloured by, matching the scales aircasting.org shows for the same
# sensors, so a recording looks the same here as it does on the map it was downloaded from.
# Temperature is given in Celsius because that is what this platform displays.
TRACK_THRESHOLDS = {
    "PM1": {"min": 0, "low": 12, "middle": 35, "high": 55, "max": 150},
    "PM2.5": {"min": 0, "low": 12, "middle": 35, "high": 55, "max": 150},
    "PM10": {"min": 0, "low": 20, "middle": 50, "high": 100, "max": 200},
    "F": {"min": 0, "low": 8, "middle": 16, "high": 24, "max": 35},
    "C": {"min": 0, "low": 8, "middle": 16, "high": 24, "max": 35},
    "RH": {"min": 0, "low": 25, "middle": 50, "high": 75, "max": 100},
}


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


def load_window(q: Query, columns: tuple[str, ...] = ("time", "value", "unit")) -> pd.DataFrame:
    """Readings for the query, in display units.

    Every figure this module produces — series, histogram, statistics, the map snapshot,
    the track and the CSV export — comes through here, so converting once is enough to
    put temperatures in Celsius everywhere. ``columns`` chooses what to carry along:
    the track needs each reading’s own coordinates, the charts do not.
    """
    df = storage.read_series(q.station, q.measurement)
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if q.start:
        mask &= df["time"] >= pd.Timestamp(q.start)
    if q.end:
        mask &= df["time"] < pd.Timestamp(q.end)
    keep = [c for c in columns if c in df.columns]
    window = df.loc[mask & df["value"].notna(), keep]
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


def _derived_thresholds(values: pd.Series) -> dict:
    """Colour bands for a measurement with no published scale: the readings’ own quartiles.

    The edges are forced apart where the quartiles coincide — a recording that barely
    moves would otherwise produce a legend with four identical numbers on it.
    """
    lo, hi = float(values.min()), float(values.max())
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return {"min": 0.0, "low": 1.0, "middle": 2.0, "high": 3.0, "max": 4.0}
    step = (hi - lo) / 4 or max(abs(lo) * 0.05, 0.5)
    edges = [lo, *values.quantile([0.25, 0.5, 0.75]).tolist(), hi]
    for i in range(1, len(edges)):
        edges[i] = max(edges[i], edges[i - 1] + step / 8)
    keys = ("min", "low", "middle", "high", "max")
    return {key: round(float(edge), 3) for key, edge in zip(keys, edges)}


def thresholds_for(measurement: str, values: pd.Series) -> dict:
    preset = TRACK_THRESHOLDS.get(measurement)
    if preset:
        return {**{k: float(v) for k, v in preset.items()}, "source": "aircasting"}
    if values.empty:
        return {"min": 0.0, "low": 1.0, "middle": 2.0, "high": 3.0, "max": 4.0, "source": "none"}
    return {**_derived_thresholds(values), "source": "readings"}


def _bounds(frame: pd.DataFrame) -> list[list[float]]:
    return [[float(frame["latitude"].min()), float(frame["longitude"].min())],
            [float(frame["latitude"].max()), float(frame["longitude"].max())]]


def track(q: Query, max_points: int = TRACK_MAX_POINTS) -> dict:
    """Where the sensor was, reading by reading, for each recording in the window.

    A fixed station answers with no sessions: its readings share one location, which the
    map already shows as a marker. A mobile recording answers with the path it travelled,
    as parallel arrays — latitudes, longitudes, times and values — because a list of
    objects would triple the size of a payload that is mostly numbers.

    Long recordings are thinned to ``max_points`` with one stride across the whole
    window, so sessions keep their relative density and the first and last fix of each
    are always kept: a track that stopped early must not appear to run on.
    """
    columns = ("time", "value", "unit", "latitude", "longitude", "session_id")
    window = load_window(q, columns=columns)
    unit = ""
    if not window.empty and "unit" in window:
        seen = window["unit"].replace("", np.nan).dropna()
        unit = seen.iat[-1] if len(seen) else ""
    empty = {"station": q.station, "measurement": q.measurement, "unit": unit, "sessions": [],
             "points": 0, "returned": 0, "bounds": None,
             "thresholds": thresholds_for(q.measurement, pd.Series(dtype=float))}
    if window.empty or not {"latitude", "longitude"} <= set(window.columns):
        return empty
    located = window.dropna(subset=["latitude", "longitude"]).sort_values("time", kind="stable")
    if located.empty:
        return empty
    ids = located["session_id"].fillna("").astype(str) if "session_id" in located else pd.Series("", index=located.index)
    stride = max(1, math.ceil(len(located) / max_points))
    sessions = []
    for session_id, rows in located.groupby(ids, sort=False):
        thinned = rows.iloc[::stride]
        if len(thinned) < len(rows) and thinned.index[-1] != rows.index[-1]:
            thinned = pd.concat([thinned, rows.iloc[[-1]]])
        sessions.append({
            "id": session_id or "unknown",
            "count": int(len(rows)),
            "start": rows["time"].iat[0].isoformat(),
            "end": rows["time"].iat[-1].isoformat(),
            "bounds": _bounds(rows),
            "t": to_ms(thinned["time"]),
            "lat": [round(float(v), 6) for v in thinned["latitude"]],
            "lon": [round(float(v), 6) for v in thinned["longitude"]],
            "v": [_clean(v) for v in thinned["value"]],
        })
    sessions.sort(key=lambda s: s["start"])
    return {
        "station": q.station, "measurement": q.measurement, "unit": unit,
        "thresholds": thresholds_for(q.measurement, located["value"]),
        "points": int(len(located)),
        "returned": sum(len(s["t"]) for s in sessions),
        "bounds": _bounds(located),
        "sessions": sessions,
    }


def station_snapshot(code: str, measurement: str, start: datetime | None, end: datetime | None) -> dict:
    """Small summary used for map popups."""
    q = Query(station=code, measurement=measurement, start=start, end=end)
    window = load_window(q)
    if window.empty:
        return {"count": 0, "latest": None, "latest_time": None, "mean": None}
    return {"count": int(len(window)), "latest": _clean(window["value"].iat[-1]),
            "latest_time": window["time"].iat[-1].isoformat(), "mean": _clean(window["value"].mean())}
