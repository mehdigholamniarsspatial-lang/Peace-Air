"""Complete, filesystem-aware deletion operations.

Deleting from the interface has to remove three things that live in different places:
the database rows, the generated files under ``OBSERVATORY_DATA_DIR`` and, for a sensor,
the cached downloads it would otherwise be rebuilt from. Every entry point below returns
a :class:`DeletionReport` so the page that triggered it can say exactly what went, and
records the removal in the import log so Update reports keeps a permanent trace.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field

import pandas as pd
from django.db import transaction
from django.utils import timezone

from ..aircasting.client import package_variants
from ..models import AirCastingDevice, Dataset, DeviceAlias, ImportRun, Station
from . import region, storage
from .ingest import rebuild_datasets, refresh_station_location, refresh_station_summary, week_start


@dataclass
class DeletionReport:
    """What a delete actually removed, in the words the interface shows."""
    datasets: int = 0
    readings: int = 0
    stations: list[str] = field(default_factory=list)   # removed outright
    # Stations that survived but lost some readings. Kept apart from ``stations`` because
    # "deleted 1 station" and "removed some readings from 1 station" are very different
    # things to read after the fact, and only the first is a station gone from the map.
    changed: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def merge(self, other: "DeletionReport") -> "DeletionReport":
        self.datasets += other.datasets
        self.readings += other.readings
        self.stations += [c for c in other.stations if c not in self.stations]
        self.changed += [c for c in other.changed if c not in self.changed]
        self.devices += [d for d in other.devices if d not in self.devices]
        self.notes += [n for n in other.notes if n not in self.notes]
        return self

    def describe(self) -> str:
        parts = []
        if self.devices:
            parts.append(f"{len(self.devices)} sensor{'' if len(self.devices) == 1 else 's'} ({', '.join(self.devices)})")
        if self.stations:
            parts.append(f"{len(self.stations)} station{'' if len(self.stations) == 1 else 's'} ({', '.join(self.stations)})")
        if self.datasets:
            parts.append(f"{self.datasets} dataset{'' if self.datasets == 1 else 's'}")
        parts.append(f"{self.readings:,} readings")
        summary = "Deleted " + ", ".join(parts) + "."
        kept = [code for code in self.changed if code not in self.stations]
        if kept:
            summary += (f" {len(kept)} station{'' if len(kept) == 1 else 's'} kept, "
                        f"with some readings removed: {', '.join(sorted(kept))}.")
        return summary


def log_deletion(report: DeletionReport, filename: str) -> None:
    """Keep a permanent, visible record of the removal in Update reports."""
    ImportRun.objects.create(
        source="deletion", status="complete", filename=filename,
        finished_at=timezone.now(), rows_deleted=report.readings,
        stations=sorted(set(report.stations) | set(report.changed)),
        message="\n".join([report.describe(), *report.notes]),
    )


def _sweep(report: "DeletionReport") -> None:
    """Remove the files a deletion has just orphaned (source CSVs, caches, strays).

    Only unreachable files go: stations that still hold readings are never touched here,
    because losing readings is something a person has to ask for.
    """
    from .cleanup import purge  # imported late: cleanup composes on top of this module

    report.merge(purge(include_stations=False))


def _forget_station(code: str) -> None:
    """Drop a deleted station's code from earlier import reports so nothing still lists it.

    SQLite has no JSON ``contains`` lookup, so the (short) log is filtered in Python.
    """
    for run in ImportRun.objects.exclude(stations=[]).only("stations"):
        remaining = [c for c in run.stations if c != code]
        if remaining != run.stations:
            run.stations = remaining
            run.save(update_fields=["stations"])


def _remove_dataset_file(dataset: Dataset) -> None:
    root = storage.sub_dir("datasets").resolve()
    path = (root / dataset.filename).resolve()
    if path.parent != root:
        raise ValueError("Unsafe dataset path")
    path.unlink(missing_ok=True)


def _remove_device_downloads(device_id: str) -> None:
    """Remove a sensor's cached AirCasting downloads so a later sync cannot restore it."""
    raw_root = storage.sub_dir("raw", "aircasting").resolve()
    raw_path = (raw_root / device_id.replace(":", "_")).resolve()
    if raw_path.parent != raw_root:
        raise ValueError("Unsafe device raw-data path")
    if raw_path.exists():
        shutil.rmtree(raw_path)


def _stored_readings(code: str) -> int:
    """Count the rows actually on disk, which ``Station.reading_count`` can lag behind."""
    return sum(len(storage.read_series(code, path.stem)) for path in storage.list_measurement_files(code))


@transaction.atomic
def delete_station_and_data(station: Station, log: bool = True, sweep: bool = True) -> DeletionReport:
    """Delete a station outright: datasets, generated files, source identities and readings."""
    report = DeletionReport(readings=_stored_readings(station.code), stations=[station.code])
    for dataset in list(station.datasets.all()):
        _remove_dataset_file(dataset)
        report.datasets += 1
    storage.remove_station_files(station.code)
    _forget_station(station.code)
    # Aliases cascade with the station; naming them keeps the message honest about what
    # will no longer be recognised on the next import.
    aliases = list(station.aliases.values_list("key", flat=True))
    if aliases:
        report.notes.append("Source identities removed: " + ", ".join(sorted(aliases)) + ".")
    station.delete()
    if sweep:
        _sweep(report)
    if log:
        log_deletion(report, f"Removed station {report.stations[0]}")
    return report


@transaction.atomic
def delete_dataset_and_readings(dataset: Dataset, log: bool = True) -> DeletionReport:
    """Delete a weekly dataset and its matching source readings.

    A station left with no readings at all is removed as well, so nothing keeps a marker
    on the map or a line in the station list after its data is gone.
    """
    station = dataset.station
    start = pd.Timestamp(dataset.period_start)
    end = pd.Timestamp(dataset.period_end) + pd.Timedelta(days=1)
    removed = 0
    for path in storage.list_measurement_files(station.code):
        frame = storage.read_series(station.code, path.stem)
        kept = frame[(frame["time"] < start) | (frame["time"] >= end)]
        removed += len(frame) - len(kept)
        storage.replace_series_file(path, kept)
    _remove_dataset_file(dataset)
    dataset.delete()
    refresh_station_summary(station)
    report = DeletionReport(datasets=1, readings=removed)
    if station.reading_count == 0:
        report.merge(delete_station_and_data(station, log=False, sweep=False))
        report.notes.append(f"{station.name} had no readings left and was removed.")
    if log:
        _sweep(report)
        log_deletion(report, f"Removed dataset {dataset.filename}")
    return report


def delete_datasets(datasets) -> DeletionReport:
    """Delete several datasets under one heading in the log (what the data manager sends)."""
    report = DeletionReport()
    names = []
    for dataset in datasets:
        names.append(dataset.filename)
        report.merge(delete_dataset_and_readings(dataset, log=False))
    if names:
        _sweep(report)
        log_deletion(report, names[0] if len(names) == 1 else f"{len(names)} datasets")
    return report


def device_stations(device: AirCastingDevice) -> set[Station]:
    """Every station this sensor's readings could have landed on.

    Imports key a station by ``device_group`` when the export has one and by
    ``session:<id>`` when it does not, so both spellings are matched here — missing the
    second used to leave a full station behind after the sensor was "removed".
    """
    stations = {device.station} if device.station_id else set()
    variants = [value.lower() for value in package_variants(device.device_id)]
    session_keys = {f"session:{sid}" for sid in device.session_ids()}
    for alias in DeviceAlias.objects.select_related("station"):
        key = alias.key.lower()
        if key in session_keys or any(value in key for value in variants):
            stations.add(alias.station)
    return stations


@transaction.atomic
def delete_device_and_data(device: AirCastingDevice) -> DeletionReport:
    """Delete a registered sensor and every station, file and download attributable to it."""
    report = DeletionReport(devices=[device.device_id])
    for station in device_stations(device):
        report.merge(delete_station_and_data(station, log=False, sweep=False))
    _remove_device_downloads(device.device_id)
    if not report.stations:
        report.notes.append("No stored readings were linked to this sensor.")
    device.delete()
    _sweep(report)
    log_deletion(report, f"Removed sensor {report.devices[0]}")
    return report


# --------------------------------------------------------------- readings outside the region
@dataclass
class OutsideRegionSurvey:
    """What is stored that the region no longer allows, before anything is removed."""
    readings: int = 0
    by_station: dict = field(default_factory=dict)      # station code -> readings outside
    # Stations whose marker was placed by hand outside the region. A marker *derived* from
    # readings can also land outside — strays drag the median out to sea — but that is a
    # symptom of those readings and rights itself when they go, so it is not listed here.
    misplaced: list[str] = field(default_factory=list)

    @property
    def anything(self) -> bool:
        return bool(self.readings or self.misplaced)

    def describe(self) -> str:
        if not self.anything:
            return f"Nothing stored falls outside {region.name()}."
        parts = []
        if self.readings:
            parts.append(f"{self.readings:,} reading{'' if self.readings == 1 else 's'} "
                         f"across {len(self.by_station)} station{'' if len(self.by_station) == 1 else 's'}")
        if self.misplaced:
            parts.append(f"{len(self.misplaced)} station marker{'' if len(self.misplaced) == 1 else 's'} "
                         f"({', '.join(self.misplaced)})")
        return f"Outside {region.name()}: " + " and ".join(parts) + "."


def find_outside_region() -> OutsideRegionSurvey:
    """Which stored readings were taken outside the region, without removing any.

    Readings with no coordinates are never counted: an indoor session has its position
    withheld by AirCasting, and "we do not know where this was" is not the same claim as
    "this was somewhere else".
    """
    survey = OutsideRegionSurvey()
    for station in Station.objects.all():
        count = 0
        for path in storage.list_measurement_files(station.code):
            frame = storage.read_series(station.code, path.stem)
            if not frame.empty:
                count += int(region.outside(frame["latitude"], frame["longitude"]).sum())
        if count:
            survey.by_station[station.code] = count
            survey.readings += count
        if (station.location_source == "manual" and station.has_location
                and not region.contains(station.latitude, station.longitude)):
            survey.misplaced.append(station.code)
    return survey


@transaction.atomic
def purge_outside_region(log: bool = True) -> DeletionReport:
    """Remove every stored reading taken outside the region, and re-place what is left.

    A station is not deleted for being partly outside: only the readings that were taken
    outside go, the marker is re-derived from those that remain, and the station itself is
    removed only when nothing at all is left of it. A marker someone placed by hand
    outside the region is cleared rather than moved, because guessing where they meant
    would be worse than saying the station has no location.
    """
    report = DeletionReport()
    for station in list(Station.objects.all()):
        weeks, removed = set(), 0
        for path in storage.list_measurement_files(station.code):
            frame = storage.read_series(station.code, path.stem)
            if frame.empty:
                continue
            beyond = region.outside(frame["latitude"], frame["longitude"])
            if not beyond.any():
                continue
            weeks |= {week_start(d) for d in frame.loc[beyond, "time"].dt.date.unique()}
            removed += int(beyond.sum())
            storage.replace_series_file(path, frame[~beyond])
        manual_outside = (station.location_source == "manual" and station.has_location
                          and not region.contains(station.latitude, station.longitude))
        if not (removed or manual_outside):
            continue
        report.readings += removed
        refresh_station_summary(station)
        if station.reading_count == 0:
            report.merge(delete_station_and_data(station, log=False, sweep=False))
            report.notes.append(f"{station.name} had no readings left inside {region.name()} and was removed.")
            continue
        if manual_outside:
            station.latitude, station.longitude, station.location_source = None, None, "none"
            station.save(update_fields=["latitude", "longitude", "location_source"])
            report.notes.append(f"{station.name} was placed by hand outside {region.name()}; "
                                "its location has been cleared.")
        else:
            refresh_station_location(station)
            # Re-deriving normally brings the marker back inside, because the readings that
            # dragged it out have gone. It can still land outside where a station's readings
            # straddle the region and their median falls in the sea between them; a marker
            # there would be a point on the map the project does not cover, so it goes.
            if station.has_location and not region.contains(station.latitude, station.longitude):
                station.latitude, station.longitude, station.location_source = None, None, "none"
                station.save(update_fields=["latitude", "longitude", "location_source"])
                report.notes.append(f"{station.name} has readings too far apart to place inside "
                                    f"{region.name()}; its location has been cleared.")
        rebuild_datasets(station, weeks)
        if station.code not in report.changed:
            report.changed.append(station.code)
    if log and (report.readings or report.notes):
        # Deliberately no orphan sweep. The other deletions here remove whole stations or
        # sensors and genuinely strand files; this one only drops rows from inside a
        # series, and sweeping would reach past what was asked for — cached downloads for
        # an unregistered sensor, say — on the back of a filtering change. The raw files
        # are also the only way back from this, since readings cannot be restored.
        log_deletion(report, f"Removed readings outside {region.name()}")
    return report
