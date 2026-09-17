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
from . import storage
from .ingest import refresh_station_summary


@dataclass
class DeletionReport:
    """What a delete actually removed, in the words the interface shows."""
    datasets: int = 0
    readings: int = 0
    stations: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def merge(self, other: "DeletionReport") -> "DeletionReport":
        self.datasets += other.datasets
        self.readings += other.readings
        self.stations += [c for c in other.stations if c not in self.stations]
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
        return "Deleted " + ", ".join(parts) + "."


def log_deletion(report: DeletionReport, filename: str) -> None:
    """Keep a permanent, visible record of the removal in Update reports."""
    ImportRun.objects.create(
        source="deletion", status="complete", filename=filename,
        finished_at=timezone.now(), rows_deleted=report.readings,
        stations=sorted(report.stations),
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
