"""Read sensor CSV files, normalise them and write them into station storage.

Three input layouts are accepted:

* **AirCasting export** – the 18 columns written by the notebook / downloader
  (``device_group, session_id, stream_id, sensor_name, source_time, value, latitude, ...``).
* **AirCasting session export** – the per-session CSV the aircasting.org map offers for a
  mobile recording: one row per GPS fix, one column per measured channel, under a block
  of paired metadata rows. :func:`read_session_export` folds it back into the layout
  above, so nothing downstream has to know this shape exists.
* **Simple long format** – ``station, time, measurement, value`` plus optional
  ``station_name, unit, latitude, longitude, region``. Useful for other sensor networks.
"""
from __future__ import annotations

import csv
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
from . import region, storage, units

log = logging.getLogger("observatory.ingest")

AIRCASTING_REQUIRED = {"sensor_name", "value"}
SIMPLE_REQUIRED = {"station", "time", "measurement", "value"}

MEASUREMENT_LABELS = {"F": "Temperature", "C": "Temperature", "RH": "Humidity"}

# The session export spells its units out in words; everything else here uses symbols.
SPELLED_UNITS = {
    "micrograms per cubic meter": "µg/m³", "micrograms per cubic metre": "µg/m³",
    "degrees fahrenheit": "F", "degrees celsius": "°C", "degrees centigrade": "°C",
    "percent": "%", "parts per billion": "ppb", "parts per million": "ppm", "decibels": "dB",
}
# How far into a file to look for the session export’s real header row.
SESSION_HEADER_SCAN = 40


class IngestError(ValueError):
    """The file cannot be imported; the message explains how to fix it."""


@dataclass
class IngestResult:
    rows_read: int = 0
    rows_stored: int = 0
    rows_duplicate: int = 0
    rows_rejected: int = 0
    rows_outside_region: int = 0
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


def normalise_unit(unit: str) -> str:
    """``micrograms per cubic meter`` -> ``µg/m³``; a symbol is returned unchanged."""
    return SPELLED_UNITS.get(str(unit or "").strip().lower(), str(unit or "").strip())


# ------------------------------------------------- AirCasting per-session export (a track)
def _sniff_session_export(path: Path) -> tuple[int, list[list[str]]] | None:
    """Find the real header of a session export, and the metadata rows above it.

    The file opens with paired rows — a label repeated across the measurement columns,
    then that label’s value for each of them — and only afterwards the header the
    readings sit under::

        ,,,,,Sensor_Name,Sensor_Name,Sensor_Name
        ,,,,,AirBeam3-PM1,AirBeam3-PM2.5,AirBeam3-RH
        ObjectID,Session_Name,Timestamp,Latitude,Longitude,1:Measurement_Value,...

    Returns ``None`` for anything that is not this layout, so the caller can fall back
    to reading the file as an ordinary CSV.
    """
    preamble: list[list[str]] = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for index, row in enumerate(csv.reader(fh)):
                if index >= SESSION_HEADER_SCAN:
                    return None
                cells = [cell.strip() for cell in row]
                if cells and cells[0].lower() == "objectid" and any(
                        cell.lower().endswith("measurement_value") for cell in cells):
                    return index, preamble
                preamble.append(cells)
    except (OSError, csv.Error, UnicodeDecodeError):
        return None
    return None


def _metadata_by_column(rows: list[list[str]]) -> dict[str, dict[int, str]]:
    """``{"sensor_name": {5: "AirBeam3-F", 6: "AirBeam3-PM1", ...}, ...}``.

    A label row carries one word repeated over the measurement columns, so a row with
    exactly one distinct value names the row beneath it. Anything else is skipped rather
    than guessed at.
    """
    found: dict[str, dict[int, str]] = {}
    index = 0
    while index + 1 < len(rows):
        labels = {cell for cell in rows[index] if cell}
        if len(labels) == 1:
            found[labels.pop().lower()] = {i: v for i, v in enumerate(rows[index + 1]) if v}
            index += 2
        else:
            index += 1
    return found


def _session_id(path: Path, names: pd.Series) -> pd.Series:
    """The AirCasting session number, taken from the download’s file name.

    The export is named ``<session name>_<session id>__<export stamp>.csv`` and holds the
    number nowhere inside, so a renamed file falls back to the session name. Either way
    the value only has to be stable and unique per recording: it is two thirds of the key
    that stops a re-import counting the same readings twice.
    """
    match = re.match(r"^(?P<name>.+?)_(?P<id>\d{4,})__", path.stem)
    if match:
        return pd.Series(match.group("id"), index=names.index)
    return names.astype(str).str.strip().replace("", "session")


def read_session_export(path: Path) -> pd.DataFrame | None:
    """Read a per-session export into the long AirCasting layout, or ``None`` if it is not one.

    Every reading keeps the coordinates of its own GPS fix — that is the point of a
    mobile recording, and what the map explorer draws as the sensor’s track.
    """
    sniffed = _sniff_session_export(path)
    if sniffed is None:
        return None
    header_index, preamble = sniffed
    meta = _metadata_by_column(preamble)
    wide = pd.read_csv(path, skiprows=header_index, dtype=str, keep_default_na=False,
                       encoding="utf-8-sig", low_memory=False)
    wide.columns = [str(column).strip() for column in wide.columns]
    columns = {column.lower(): column for column in wide.columns}
    value_columns = [c for c in wide.columns if c.lower().endswith("measurement_value")]
    missing = {"timestamp"} - set(columns)
    if missing or not value_columns:
        raise IngestError("This looks like an AirCasting session export but has no Timestamp "
                          "or Measurement_Value columns.")
    blank = pd.Series("", index=wide.index)
    names = wide[columns["session_name"]] if "session_name" in columns else blank
    session_id = _session_id(path, names)
    frames = []
    for column in value_columns:
        at = wide.columns.get_loc(column)
        sensor = meta.get("sensor_name", {}).get(at) or column
        frames.append(pd.DataFrame({
            "device_group": meta.get("sensor_package_name", {}).get(at, ""),
            "session_id": session_id,
            # The export carries no stream ids; the session number identifies the recording.
            "stream_id": "",
            "sensor_name": sensor,
            "measurement_type": meta.get("measurement_type", {}).get(at, ""),
            "unit": normalise_unit(meta.get("measurement_units", {}).get(at, "")),
            "source_time": wide[columns["timestamp"]],
            "value": wide[column],
            "latitude": wide[columns["latitude"]] if "latitude" in columns else blank,
            "longitude": wide[columns["longitude"]] if "longitude" in columns else blank,
            # Each row is where the sensor was at that second, not a session-wide location.
            "coordinate_source": "measurement",
            "time_status": "source_clock_unverified",
        }))
    return pd.concat(frames, ignore_index=True)


def read_source_csv(path: Path) -> pd.DataFrame:
    """Read a sensor file into one of the layouts :func:`normalise` understands."""
    session = read_session_export(path)
    if session is not None:
        return session
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig", low_memory=False)


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


def normalise(df: pd.DataFrame, source_file: str) -> tuple[pd.DataFrame, int, int, str]:
    """Return (tidy frame, rejected rows, rows outside the region, detected layout)."""
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
    # A reading whose fix lies outside the region is not stored at all. It is counted
    # apart from the malformed rows because it is a different thing to tell someone: the
    # file was readable, the sensor simply was not where this platform covers.
    beyond = valid & region.outside(tidy["latitude"], tidy["longitude"])
    return tidy[valid & ~beyond].copy(), int((~valid).sum()), int(beyond.sum()), layout


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


def _station_for_device(key: str, code: str) -> Station | None:
    """The station already standing for this sensor, matched on its hardware address.

    One sensor is spelled several ways across AirCasting's own exports — the downloader
    writes ``query:AirBeam3-b0b21c7627c4;…`` where a session export writes
    ``AirBeam3:b0b21c7627c4`` — and coordinates cannot reconcile them, because a fixed
    indoor session has none and a mobile one is a track rather than a point. The MAC
    address in the key can: it names the hardware, so both spellings belong to the same
    station instead of standing up a second copy of one sensor.
    """
    if not re.search(r"[0-9A-Fa-f]{12}", key):
        return None
    return Station.objects.filter(code=code).first()


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
        code, name = _derive_code(key)
        existing = (Station.objects.filter(code=hint).first() if hint else None) or _station_for_device(key, code)
        if existing:
            station = existing
        else:
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


def _distinct_positions(df: pd.DataFrame) -> int:
    if not {"latitude", "longitude"} <= set(df.columns):
        return 0
    located = df[["latitude", "longitude"]].dropna()
    if located.empty:
        return 0
    return int(len(located.round(settings.OBSERVATORY_COORD_PRECISION).drop_duplicates()))


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
            # Distinct places these readings were taken, which is what separates a track
            # from a point: a fixed station repeats one coordinate on every row, however
            # many rows it has, while a mobile recording has a new fix every second.
            "positions": _distinct_positions(df),
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


def refresh_station_location(station: Station) -> None:
    """Re-derive a station's marker from the readings it still holds.

    Needed after readings are removed rather than added: a station placed at the median
    of a track has to move when part of that track goes, and one whose every located
    reading has gone belongs nowhere on the map. A location entered by hand is left
    alone, as it is everywhere else.
    """
    if station.location_source == "manual":
        return
    frames = [storage.read_series(station.code, m["file"]) for m in station.measurements]
    rows = [frame for frame in frames if not frame.empty]
    if rows:
        latitude, longitude, source, _indoor = _location(pd.concat(rows, ignore_index=True))
    else:
        latitude, longitude, source = None, None, "none"
    station.latitude, station.longitude, station.location_source = latitude, longitude, source
    station.save(update_fields=["latitude", "longitude", "location_source"])


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
    tidy, rejected, beyond, layout = normalise(raw, source_file)
    result.rows_rejected = rejected + beyond
    result.rows_outside_region = beyond
    if rejected:
        result.warnings.append(f"{rejected} rows skipped: missing time, value or measurement.")
    if beyond:
        result.warnings.append(f"{beyond} readings were taken outside {region.name()} and were not stored.")
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
        raw = read_source_csv(path)
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
