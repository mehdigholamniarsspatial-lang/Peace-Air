"""Structured CSV storage for sensor readings.

Layout under ``OBSERVATORY_DATA_DIR``::

    raw/uploads/<timestamp>_<original>.csv     every uploaded file, untouched
    raw/aircasting/<device>/export_*/          notebook-style exports + manifest + cache
    stations/<CODE>/<MEASUREMENT>.csv          one tidy time series per station & measurement
    datasets/<Station>_<YYYY-MM-DD>.csv        weekly downloadable datasets (long format)

Station files are the single source of truth for analysis. Each is sorted by time and
de-duplicated on (time, session_id, stream_id), so re-importing a file is safe.
"""
from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path

import pandas as pd
from django.conf import settings

STATION_COLUMNS = ["time", "value", "unit", "measurement_type", "sensor_name", "session_id", "stream_id",
                   "time_status", "latitude", "longitude", "coordinate_source", "source_file"]
DEDUP_KEYS = ["time", "session_id", "stream_id"]

_lock = threading.RLock()
_cache: dict[Path, tuple[float, pd.DataFrame]] = {}


def data_dir() -> Path:
    path = Path(settings.OBSERVATORY_DATA_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def sub_dir(*parts: str) -> Path:
    path = data_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def measurement_slug(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_") or "value"


def station_dir(code: str, create: bool = False) -> Path:
    """Where a station's series live. Reading never creates the folder: an empty one
    left behind by a lookup would look like storage for a station that no longer exists."""
    path = data_dir() / "stations" / measurement_slug(code)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def series_path(code: str, measurement: str, create: bool = False) -> Path:
    return station_dir(code, create) / f"{measurement_slug(measurement)}.csv"


def read_series(code: str, measurement: str) -> pd.DataFrame:
    """Read one station/measurement series, cached until the file changes."""
    path = series_path(code, measurement)
    if not path.exists():
        return pd.DataFrame(columns=STATION_COLUMNS).assign(time=pd.to_datetime([]), value=pd.Series(dtype=float))
    mtime = path.stat().st_mtime
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    df = pd.read_csv(path, dtype={"session_id": str, "stream_id": str, "unit": str}, keep_default_na=False,
                     na_values={"value": [""], "latitude": [""], "longitude": [""]})
    df["time"] = pd.to_datetime(df["time"], format="ISO8601")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    with _lock:
        _cache[path] = (mtime, df)
    return df


def merge_series(code: str, measurement: str, new_rows: pd.DataFrame) -> tuple[int, int, pd.DataFrame]:
    """Append rows to a station series. Returns (stored, duplicates, merged frame)."""
    new_rows = new_rows.reindex(columns=STATION_COLUMNS)
    path = series_path(code, measurement, create=True)
    with _lock:
        existing = read_series(code, measurement)
        before = len(existing)
        combined = pd.concat([existing, new_rows], ignore_index=True) if before else new_rows.copy()
        combined["session_id"] = combined["session_id"].fillna("").astype(str)
        combined["stream_id"] = combined["stream_id"].fillna("").astype(str)
        combined = combined.drop_duplicates(subset=DEDUP_KEYS, keep="last").sort_values("time", kind="stable")
        stored = len(combined) - before
        duplicates = len(new_rows) - stored
        out = combined.copy()
        out["time"] = out["time"].dt.strftime("%Y-%m-%dT%H:%M:%S")
        tmp = path.with_suffix(".csv.tmp")
        out.to_csv(tmp, index=False, columns=STATION_COLUMNS)
        tmp.replace(path)
        _cache.pop(path, None)
    return stored, duplicates, combined


def list_measurement_files(code: str) -> list[Path]:
    folder = station_dir(code)
    return sorted(folder.glob("*.csv")) if folder.is_dir() else []


def replace_series_file(path: Path, frame: pd.DataFrame) -> None:
    """Atomically replace or remove a station series and invalidate its cache."""
    path = Path(path)
    with _lock:
        if frame.empty:
            path.unlink(missing_ok=True)
        else:
            out = frame.reindex(columns=STATION_COLUMNS).copy()
            out["time"] = pd.to_datetime(out["time"]).dt.strftime("%Y-%m-%dT%H:%M:%S")
            temporary = path.with_suffix(path.suffix + ".tmp")
            out.to_csv(temporary, index=False, columns=STATION_COLUMNS)
            temporary.replace(path)
        _cache.pop(path, None)


def forget_cached(path: Path) -> None:
    """Drop cached frames for a file, or for everything inside a directory."""
    path = Path(path).resolve()
    with _lock:
        for cached in list(_cache):
            resolved = cached.resolve()
            if resolved == path or path in resolved.parents:
                _cache.pop(cached, None)


def remove_station_files(code: str) -> None:
    """Remove a station's normalized storage directory, constrained to data/stations."""
    root = sub_dir("stations").resolve()
    target = station_dir(code).resolve()
    if target.parent != root:
        raise ValueError("Unsafe station storage path")
    with _lock:
        forget_cached(target)
        if target.exists():
            shutil.rmtree(target)
