"""Reconcile what is on disk with what the Data manager lists.

The Data manager is the catalogue of everything this platform holds. Anything under
``OBSERVATORY_DATA_DIR`` that no longer has an entry there is dead weight: it cannot be
downloaded, it is invisible to every page, and a later import that reuses the same
station code would silently pick the old readings back up.

:func:`find_orphans` reports that leftover storage without touching it; :func:`purge`
removes it. Orphans come in two kinds, kept apart deliberately:

* **files** – stray directories, generated CSVs and cached downloads with no database
  row behind them. Removing these loses nothing the interface could reach, so deletions
  sweep them automatically.
* **stations** – a station whose readings are stored but which has no dataset in the
  Data manager. Removing one destroys real readings, so it only happens when somebody
  asks for it explicitly.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from django.db.models import Count

from ..models import AirCastingDevice, Dataset, Station
from . import storage


@dataclass
class Orphan:
    kind: str           # station | station_files | dataset_file | dataset_row | raw_device | raw_source | temp_file
    label: str          # what to show a person
    detail: str = ""
    path: Path | None = None
    readings: int = 0
    size: int = 0

    @property
    def destroys_readings(self) -> bool:
        return self.kind == "station"

    def as_json(self) -> dict:
        return {"kind": self.kind, "label": self.label, "detail": self.detail,
                "readings": self.readings, "size": self.size,
                "destroys_readings": self.destroys_readings}


@dataclass
class Survey:
    stations: list[Orphan] = field(default_factory=list)
    files: list[Orphan] = field(default_factory=list)

    @property
    def all(self) -> list[Orphan]:
        return self.stations + self.files

    @property
    def size(self) -> int:
        return sum(o.size for o in self.all)

    @property
    def readings(self) -> int:
        return sum(o.readings for o in self.all)

    def as_json(self) -> dict:
        return {"stations": [o.as_json() for o in self.stations], "files": [o.as_json() for o in self.files],
                "total": len(self.all), "size": self.size, "readings": self.readings}


def _tree_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _row_count(path: Path) -> int:
    """Data rows in a CSV, without parsing it."""
    with path.open("rb") as fh:
        return max(0, sum(1 for _ in fh) - 1)


def _referenced_sources() -> set[str]:
    """Every ``source_file`` name still backing a stored reading."""
    names: set[str] = set()
    for path in storage.sub_dir("stations").glob("*/*.csv"):
        try:
            column = pd.read_csv(path, usecols=["source_file"], dtype=str, keep_default_na=False)
        except (ValueError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue  # a file we cannot read is handled as a stray directory instead
        names.update(value for value in column["source_file"].unique() if value)
    return names


def find_orphans() -> Survey:
    """List everything on disk or in the catalogue that the Data manager no longer covers."""
    survey = Survey()

    # 1. Stations holding readings that no dataset represents.
    for station in Station.objects.annotate(n=Count("datasets")).filter(n=0):
        files = storage.list_measurement_files(station.code)
        survey.stations.append(Orphan(
            kind="station",
            label=f"{station.name} · {station.code}",
            detail="No dataset in the Data manager, but readings are still stored.",
            readings=sum(_row_count(path) for path in files),
            size=sum(path.stat().st_size for path in files),
        ))

    # 2. Reading directories with no station row.
    known_slugs = {storage.measurement_slug(code) for code in Station.objects.values_list("code", flat=True)}
    for folder in sorted(storage.sub_dir("stations").iterdir()):
        if folder.is_dir() and folder.name not in known_slugs:
            survey.files.append(Orphan(
                kind="station_files", label=f"stations/{folder.name}",
                detail="Reading files for a station that no longer exists.",
                path=folder, size=_tree_size(folder),
                readings=sum(_row_count(p) for p in folder.glob("*.csv")),
            ))

    # 3. Generated dataset files with no catalogue entry, and entries with no file.
    dataset_dir = storage.sub_dir("datasets")
    catalogued = set(Dataset.objects.values_list("filename", flat=True))
    for path in sorted(dataset_dir.glob("*.csv")):
        if path.name not in catalogued:
            survey.files.append(Orphan(
                kind="dataset_file", label=f"datasets/{path.name}",
                detail="Downloadable file that is not listed in the Data manager.",
                path=path, size=path.stat().st_size))
    for dataset in Dataset.objects.select_related("station"):
        if not (dataset_dir / dataset.filename).exists():
            survey.files.append(Orphan(
                kind="dataset_row", label=dataset.filename,
                detail="Listed in the Data manager, but the file is missing.",
                path=None))

    # 4. Cached AirCasting downloads for sensors that are no longer registered.
    registered = {device.device_id.replace(":", "_") for device in AirCastingDevice.objects.all()}
    for folder in sorted(storage.sub_dir("raw", "aircasting").iterdir()):
        if folder.is_dir() and folder.name not in registered:
            survey.files.append(Orphan(
                kind="raw_device", label=f"raw/aircasting/{folder.name}",
                detail="Downloads cached for a sensor that is not registered in the data manager.",
                path=folder, size=_tree_size(folder)))

    # 5. Uploaded and demo source files that no stored reading still comes from.
    referenced = _referenced_sources()
    for name in ("uploads", "demo"):
        for path in sorted(storage.sub_dir("raw", name).glob("*")):
            if path.is_file() and path.name not in referenced:
                survey.files.append(Orphan(
                    kind="raw_source", label=f"raw/{name}/{path.name}",
                    detail="Source file whose readings are no longer stored.",
                    path=path, size=path.stat().st_size))

    # 6. Half-written files left by an interrupted import.
    for path in sorted(storage.data_dir().rglob("*.tmp")):
        survey.files.append(Orphan(
            kind="temp_file", label=str(path.relative_to(storage.data_dir())).replace("\\", "/"),
            detail="Left behind by an interrupted write.", path=path, size=path.stat().st_size))

    return survey


def _remove(orphan: Orphan) -> None:
    """Delete one orphan, refusing anything that resolves outside the data directory."""
    if orphan.kind == "dataset_row":
        Dataset.objects.filter(filename=orphan.label).delete()
        return
    if orphan.path is None:
        return
    root = storage.data_dir().resolve()
    target = orphan.path.resolve()
    if root not in target.parents:
        raise ValueError(f"Refusing to delete outside the data directory: {target}")
    if target.is_dir():
        storage.forget_cached(target)
        shutil.rmtree(target)
    else:
        storage.forget_cached(target)
        target.unlink(missing_ok=True)


def purge(include_stations: bool = False, survey: Survey | None = None):
    """Remove orphaned storage. Returns a ``DeletionReport`` describing what went.

    ``include_stations`` also deletes stations that hold readings no dataset represents;
    without it only unreachable files are swept.
    """
    from .deletion import DeletionReport, delete_station_and_data  # circular at module level

    survey = survey or find_orphans()
    report = DeletionReport()
    if include_stations:
        for orphan in survey.stations:
            code = orphan.label.rsplit("·", 1)[-1].strip()
            station = Station.objects.filter(code=code).first()
            if station:
                report.merge(delete_station_and_data(station, log=False, sweep=False))
        # Deleting those stations frees their source files, so look again.
        survey = find_orphans()
    for orphan in survey.files:
        _remove(orphan)
    if survey.files:
        report.notes.append(
            f"Removed {len(survey.files)} orphaned file{'' if len(survey.files) == 1 else 's'} "
            f"({survey.size / 1e6:.1f} MB) that the Data manager did not list."
        )
    return report
