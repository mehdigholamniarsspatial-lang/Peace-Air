"""Read sensor CSV files, normalise them and write them into station storage.

Two input layouts are accepted:

* **AirCasting export** – the 18 columns written by the notebook / downloader
  (``device_group, session_id, stream_id, sensor_name, source_time, value, latitude, ...``).
* **Simple long format** – ``station, time, measurement, value`` plus optional
  ``station_name, unit, latitude, longitude, region``. Useful for other sensor networks.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Dataset, DeviceAlias, ImportRun, Station
from . import storage, units

log = logging.getLogger("observatory.ingest")

AIRCASTING_REQUIRED = {"sensor_name", "value"}
SIMPLE_REQUIRED = {"station", "time", "measurement", "value"}

MEASUREMENT_LABELS = {"F": "Temperature", "C": "Temperature", "RH": "Humidity"}


class IngestError(ValueError):
    """The file cannot be imported; the message explains how to fix it."""


@dataclass
class IngestResult:
    rows_read: int = 0
    rows_stored: int = 0
    rows_duplicate: int = 0
    rows_rejected: int = 0
    stations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- parsing
def measurement_key(sensor_name: str) -> str:
    """``AirBeam3-PM2.5`` -> ``PM2.5``; anything else is kept as written."""
    name = str(sensor_name).strip()
    if re.match(r"(?i)^airbeam\w*-", name):
        return name.split("-", 1)[1]
    return name


def measurement_label(key: str, measurement_type: str = "") -> str:
    return MEASUREMENT_LABELS.get(key, key if key.upper().startswith("PM") else (measurement_type or key))


def _parse_times(df: pd.DataFrame) -> pd.Series:
    """Source clock first (the notebook's default convention), then UTC, then epoch ms."""
    times = pd.to_datetime(df.get("source_time", pd.Series("", index=df.index)), errors="coerce", format="ISO8601")
    if "time_utc" in df:
        utc = pd.to_datetime(df["time_utc"], errors="coerce", utc=True, format="ISO8601")
        utc = utc.dt.tz_convert(settings.TIME_ZONE).dt.tz_localize(None)
        times = times.fillna(utc)
    if "raw_time" in df:
        raw = pd.to_numeric(df["raw_time"], errors="coerce")
        times = times.fillna(pd.to_datetime(raw, unit="ms", errors="coerce"))
    if "time" in df:
        parsed = pd.to_datetime(df["time"], errors="coerce", format="ISO8601")
        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_convert(settings.TIME_ZONE).dt.tz_localize(None)
        times = times.fillna(parsed)
    return times.dt.floor("s")


def normalise(df: pd.DataFrame, source_file: str) -> tuple[pd.DataFrame, int, str]:
    """Return (tidy frame, rejected row count, detected layout)."""
    df.columns = [c.strip().lower() for c in df.columns]
    cols = set(df.columns)
    if AIRCASTING_REQUIRED <= cols and ({"source_time", "time_utc", "raw_time"} & cols):
        layout = "aircasting"
        key = df.get("device_group", pd.Series("", index=df.index)).astype(str).str.strip()
        fallback = "session:" + df.get("session_id", pd.Series("unknown", index=df.index)).astype(str)
        tidy = pd.DataFrame({
            "source_key": key.where(key != "", fallback),
            "station_hint": "",
            "name_hint": "",
            "region_hint": "",
            "measurement": df["sensor_name"].map(measurement_key),
            "sensor_name": df["sensor_name"],
            "measurement_type": df.get("measurement_type", ""),
            "unit": df.get("unit", ""),
            "session_id": df.get("session_id", ""),
            "stream_id": df.get("stream_id", ""),
            "time_status": df.get("time_status", ""),
            "coordinate_source": df.get("coordinate_source", ""),
        })
    elif SIMPLE_REQUIRED <= cols:
        layout = "simple"
        tidy = pd.DataFrame({
            "source_key": "station:" + df["station"].astype(str).str.strip(),
            "station_hint": df["station"].astype(str).str.strip(),
            "name_hint": df.get("station_name", ""),
            "region_hint": df.get("region", ""),
            "measurement": df["measurement"].astype(str).str.strip(),
            "sensor_name": df["measurement"],
            "measurement_type": df.get("measurement_type", ""),
            "unit": df.get("unit", ""),
            "session_id": "",
            "stream_id": "",
            "time_status": "as_supplied",
            "coordinate_source": "file",
        })
    else:
        raise IngestError(
            "Columns not recognised. Use an AirCasting export (sensor_name, value, source_time …) "
            "or the simple layout: station, time, measurement, value [, unit, latitude, longitude]."
        )
    tidy["time"] = _parse_times(df)
    tidy["value"] = pd.to_numeric(df["value"], errors="coerce")
    tidy["latitude"] = pd.to_numeric(df.get("latitude", np.nan), errors="coerce")
    tidy["longitude"] = pd.to_numeric(df.get("longitude", np.nan), errors="coerce")
    bad_coords = ~(tidy["latitude"].between(-90, 90) & tidy["longitude"].between(-180, 180))
    tidy.loc[bad_coords, ["latitude", "longitude"]] = np.nan
    tidy["source_file"] = source_file
    for col in ("name_hint", "region_hint", "measurement_type", "unit", "session_id", "stream_id", "time_status", "coordinate_source"):
        tidy[col] = tidy[col].fillna("").astype(str)
    valid = tidy["time"].notna() & np.isfinite(tidy["value"]) & (tidy["measurement"] != "")
    return tidy[valid].copy(), int((~valid).sum()), layout


# --------------------------------------------------------------------------- stations
def _derive_code(key: str) -> tuple[str, str]:
    mac = re.search(r"([0-9A-Fa-f]{12})", key)
    model = re.search(r"(?i)airbeam(\d?)", key)
    if mac:
        prefix = f"AB{model.group(1)}" if model else "DEV"
        suffix = mac.group(1)[-6:].upper()
        return f"{prefix}-{suffix}", f"{'AirBeam' + model.group(1) if model else 'Device'} {suffix}"
    clean = re.sub(r"^(station|session|query):", "", key)
    return re.sub(r"[^A-Za-z0-9]+", "-", clean).strip("-").upper()[:24] or "STATION", clean[:120]


def _unique_code(code: str) -> str:
    candidate, n = code, 2
    while Station.objects.filter(code=candidate).exists():
        candidate, n = f"{code}-{n}", n + 1
    return candidate


def _location(rows: pd.DataFrame) -> tuple[float | None, float | None, str, bool]:
    indoor = rows["coordinate_source"].str.contains("indoor").any()
    located = rows.dropna(subset=["latitude", "longitude"])
    if located.empty:
        return None, None, "none", bool(indoor)
    precision = settings.OBSERVATORY_COORD_PRECISION
    if (located["coordinate_source"] == "measurement").all():
        return float(located["latitude"].median()), float(located["longitude"].median()), "measurement_median", bool(indoor)
    pairs = located[["latitude", "longitude"]].round(precision)
    lat, lon = pairs.value_counts().idxmax()
    source = "fixed_session" if (located["coordinate_source"] == "fixed_session").any() else "file"
    return float(lat), float(lon), source, bool(indoor)


def _same_point(station: Station, lat: float, lon: float) -> bool:
    p = settings.OBSERVATORY_COORD_PRECISION
    return round(station.latitude, p) == round(lat, p) and round(station.longitude, p) == round(lon, p)


def resolve_station(key: str, rows: pd.DataFrame, force_station: Station | None = None) -> Station:
    lat, lon, source, indoor = _location(rows)
    alias = DeviceAlias.objects.select_related("station").filter(key=key).first()
    station = force_station or (alias.station if alias else None)
    if station is None and lat is not None:
        # Spatial aggregation: an unseen device at an existing station's coordinates joins that station.
        station = next((s for s in Station.objects.exclude(latitude=None) if _same_point(s, lat, lon)), None)
    if station is None:
        hint = rows["station_hint"].iloc[0]
        existing = Station.objects.filter(code=hint).first() if hint else None
        if existing:
            station = existing
        else:
            code, name = _derive_code(key)
            station = Station.objects.create(
                code=_unique_code(hint[:32] if hint else code),
                name=(rows["name_hint"].iloc[0] or (hint if hint else name))[:120],
                region=rows["region_hint"].iloc[0] or settings.OBSERVATORY_DEFAULT_REGION,
            )
    if not alias:
        DeviceAlias.objects.get_or_create(key=key, defaults={"station": station})
    if lat is not None and station.location_source != "manual":
        station.latitude, station.longitude, station.location_source = lat, lon, source
    station.is_indoor = station.is_indoor or indoor
    station.save()
    return station


def refresh_station_summary(station: Station) -> None:
    measurements, total, first, last = [], 0, None, None
    for path in storage.list_measurement_files(station.code):
        df = storage.read_series(station.code, path.stem)
        if df.empty:
            continue
        key = df["sensor_name"].map(measurement_key).mode().iat[0] if "sensor_name" in df else path.stem
        seen_units = df["unit"].replace("", np.nan).dropna()
        mtype = df["measurement_type"].replace("", np.nan).dropna()
        measurements.append({
            # The sensor's own unit is kept here; display converts it (see services/units.py).
            "key": key, "file": path.stem, "unit": seen_units.iat[-1] if len(seen_units) else "",
            "type": mtype.iat[-1] if len(mtype) else "", "label": measurement_label(key, mtype.iat[-1] if len(mtype) else ""),
            "count": int(len(df)),
        })
        total += len(df)
        first = min(first, df["time"].iat[0]) if first is not None else df["time"].iat[0]
        last = max(last, df["time"].iat[-1]) if last is not None else df["time"].iat[-1]
    order = {"PM2.5": 0, "PM10": 1, "PM1": 2}
    station.measurements = sorted(measurements, key=lambda m: (order.get(m["key"], 9), m["key"]))
    station.reading_count = total
    station.first_reading = first.to_pydatetime() if first is not None else None
    station.last_reading = last.to_pydatetime() if last is not None else None
    station.save()


# --------------------------------------------------------------------------- datasets
def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _file_label(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "station"


def rebuild_datasets(station: Station, weeks: set[date]) -> None:
    folder = storage.sub_dir("datasets")
    frames = []
    for m in station.measurements:
        df = storage.read_series(station.code, m["file"])
        if not df.empty:
            # Downloadable datasets carry the same units the dashboard shows.
            frames.append(units.convert(df, m["key"]).assign(measurement=m["key"]))
    allrows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["time", "measurement", "value"])
    for monday in sorted(weeks):
        start, end = pd.Timestamp(monday), pd.Timestamp(monday) + pd.Timedelta(days=7)
        week = allrows[(allrows["time"] >= start) & (allrows["time"] < end)] if len(allrows) else allrows
        existing = Dataset.objects.filter(station=station, period_start=monday).first()
        if week.empty:
            if existing:
                (folder / existing.filename).unlink(missing_ok=True)
                existing.delete()
            continue
        period_end = monday + timedelta(days=6)
        filename = f"{_file_label(station.name)}_{station.code}_{period_end.isoformat()}.csv"
        out = week.sort_values(["time", "measurement"]).assign(
            station_code=station.code, station_name=station.name, station_latitude=station.latitude,
            station_longitude=station.longitude, time=lambda d: d["time"].dt.strftime("%Y-%m-%dT%H:%M:%S"))
        cols = ["station_code", "station_name", "station_latitude", "station_longitude", "time", "measurement",
                "value", "unit", "measurement_type", "session_id", "stream_id", "time_status", "source_file"]
        tmp = folder / (filename + ".tmp")
        out.to_csv(tmp, index=False, columns=cols)
        tmp.replace(folder / filename)
        if existing and existing.filename != filename:
            (folder / existing.filename).unlink(missing_ok=True)
        Dataset.objects.update_or_create(
            station=station, period_start=monday,
            defaults={"period_end": period_end, "filename": filename, "readings": len(out),
                      "measurements": sorted(out["measurement"].unique().tolist())})


# --------------------------------------------------------------------------- entry points
def ingest_frame(raw: pd.DataFrame, source_file: str, force_station: Station | None = None) -> IngestResult:
    result = IngestResult(rows_read=len(raw))
    tidy, rejected, layout = normalise(raw, source_file)
    result.rows_rejected = rejected
    if rejected:
        result.warnings.append(f"{rejected} rows skipped: missing time, value or measurement.")
    if tidy.empty:
        result.warnings.append("No valid readings found in this file.")
        return result
    for key, rows in tidy.groupby("source_key", sort=False):
        with transaction.atomic():
            station = resolve_station(key, rows, force_station)
            for measurement, mrows in rows.groupby("measurement", sort=False):
                stored, dup, _ = storage.merge_series(station.code, measurement, mrows)
                result.rows_stored += stored
                result.rows_duplicate += dup
            refresh_station_summary(station)
            weeks = {week_start(d) for d in rows["time"].dt.date.unique()}
            rebuild_datasets(station, weeks)
        if station.code not in result.stations:
            result.stations.append(station.code)
        if not station.has_location:
            result.warnings.append(f"{station.name} has no coordinates (indoor or withheld). Set its location in the data manager to show it on the map.")
    log.info("Ingested %s (%s layout): %s", source_file, layout, result)
    return result


def ingest_file(path: Path | str, source: str = "cli", force_station: Station | None = None,
                run: ImportRun | None = None) -> ImportRun:
    path = Path(path)
    run = run or ImportRun.objects.create(source=source, filename=path.name)
    try:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig", low_memory=False)
        result = ingest_frame(raw, path.name, force_station)
        run.rows_read += result.rows_read
        run.rows_stored += result.rows_stored
        run.rows_duplicate += result.rows_duplicate
        run.rows_rejected += result.rows_rejected
        run.stations = sorted(set(run.stations) | set(result.stations))
        run.message = "\n".join(filter(None, [run.message, *result.warnings]))
        run.status = "partial" if result.warnings else "complete"
    except (IngestError, pd.errors.ParserError, UnicodeDecodeError) as error:
        run.status, run.message = "failed", str(error)
    except Exception as error:  # noqa: BLE001 - keep the import log truthful
        log.exception("Import failed")
        run.status, run.message = "failed", f"Import failed: {error}"
    run.finished_at = timezone.now()
    run.save()
    return run


def save_upload(uploaded) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(uploaded.name).name)
    dest = storage.sub_dir("raw", "uploads") / f"{stamp}_{safe}"
    with dest.open("wb") as fh:
        for chunk in uploaded.chunks():
            fh.write(chunk)
    return dest
