"""Run the AirCasting downloader for registered devices and import the result."""
from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from ..aircasting.client import AirCastingDownloader, DownloadConfig
from ..models import AirCastingDevice, ImportRun, ScheduleConfig
from . import storage
from .ingest import ingest_file

log = logging.getLogger("observatory.sync")
_running = threading.Lock()


def _as_date(value):
    """Dates can arrive as strings from a field default or a fixture; normalise them."""
    return date.fromisoformat(value) if isinstance(value, str) else value


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def build_config(device: AirCastingDevice, full: bool = False) -> DownloadConfig:
    """Describe one download.

    A first run covers the whole configured period. Later runs restart a little before
    the last success so late uploads are caught — and, crucially, *discovery* uses the
    same narrowed start. Searching every year since the configured start date on every
    scheduled tick is what made routine syncs take longer than the interval between them.
    """
    start = _as_date(device.download_from)
    if device.last_success and not full:
        start = max(start, (device.last_success - timedelta(days=settings.AIRCASTING_SYNC_OVERLAP_DAYS)).date())
    folder = storage.sub_dir("raw", "aircasting", device.device_id.replace(":", "_"))
    return DownloadConfig(
        device_id=device.device_id,
        known_session_ids=device.session_ids(),
        project_tags=device.tags(),
        discovery_start=_iso(start),
        download_from=_iso(start),
        download_until=_iso(_as_date(device.download_until)) if device.download_until else None,
        time_convention=settings.AIRCASTING_TIME_CONVENTION,
        timezone_name=settings.AIRCASTING_TIMEZONE,
        output_root=folder,
        base_url=settings.AIRCASTING_BASE_URL,
    )


# A download this old cannot still be in progress, even a first full one.
ABANDONED_AFTER = timedelta(hours=6)


def close_interrupted_runs() -> int:
    """Mark syncs that never finished as failed.

    ``run_sync`` holds an in-process lock, so a server restart during a download leaves a
    row saying "Running" that nothing will ever complete — the Data manager then polls it
    for ever and the status card never settles.

    Two shapes of wreckage show up: the ``finally`` never ran, leaving no finish time at
    all; or it ran and stamped one while an error escaped before the status was updated.
    Both are dead, so a finish time is a symptom rather than a sign of life.
    """
    running = ImportRun.objects.filter(source="aircasting", status="running")
    stale = [run for run in running
             if run.finished_at is not None or run.started_at < timezone.now() - ABANDONED_AFTER]
    count = 0
    for run in stale:
        run.status = "failed"
        run.finished_at = timezone.now()
        run.message = "\n".join(filter(None, [run.message, "Interrupted: the server stopped before this download finished."]))
        run.save(update_fields=["status", "finished_at", "message"])
        count += 1
    return count


def run_sync(devices: list[AirCastingDevice] | None = None, full: bool = False) -> ImportRun | None:
    """Download and import the given devices (all active ones by default).

    Returns None if a sync is already running.
    """
    if not _running.acquire(blocking=False):
        return None
    close_interrupted_runs()
    selected = list(devices) if devices is not None else list(AirCastingDevice.objects.filter(active=True))
    label = selected[0].device_id if len(selected) == 1 else "AirCasting sync"
    run = ImportRun.objects.create(source="aircasting", filename=label)
    try:
        if not selected:
            run.status, run.message = "failed", "No AirCasting sensors registered. Add one in the data manager."
        notes, failures = [], 0
        for device in selected:
            config = build_config(device, full=full)
            # A sensor with a closed end date that has already been downloaded has nothing
            # left to fetch. That is finished, not broken, so it must not read as a failure.
            if config.download_until <= config.download_from:
                notes.append(f"{device}: already downloaded up to {device.download_until}; nothing new to fetch.")
                if device.last_error:
                    device.last_error = ""
                    device.save(update_fields=["last_error"])
                continue
            device.last_attempt = timezone.now()
            device.save(update_fields=["last_attempt"])
            try:
                export_dir, manifest = AirCastingDownloader(config).run()
            except Exception as error:  # noqa: BLE001
                notes.append(f"{device}: {error}")
                _record_failure(device, str(error))
                failures += 1
                continue
            if export_dir is None:
                # Say *why* the search came back empty: "3 windows failed" on its own sent
                # people looking for missing recordings when the real fault was local.
                window_errors = manifest.get("discovery_errors", [])
                reason = f"No recordings found between {config.download_from} and {config.download_until}."
                if window_errors:
                    reason += (f" {len(window_errors)} of the search windows failed, the first with: "
                               f"{window_errors[0].get('error', 'unknown error')}")
                notes.append(f"{device}: {reason}")
                # A sensor that has downloaded before and simply has nothing new is quiet,
                # not broken; flagging it red every few minutes would train people to
                # ignore the status. Only an empty *first* download is a real failure.
                if device.last_success and not window_errors:
                    if device.last_error:
                        device.last_error = ""
                        device.save(update_fields=["last_error"])
                    continue
                _record_failure(device, reason)
                failures += 1
                continue
            ingest_file(export_dir / "all_recordings.csv", force_station=device.station, run=run)
            if manifest["status"] == "partial_or_inconsistent":
                reason = f"Some download windows failed; see {export_dir / 'manifest.json'}"
                notes.append(f"{device}: {reason}")
                _record_failure(device, reason)
                failures += 1
            else:
                device.last_success = timezone.now()
                device.last_error = ""
                device.save(update_fields=["last_success", "last_error"])
        if selected:
            run.message = "\n".join(filter(None, [run.message, *notes]))
            if run.status != "failed":
                run.status = "partial" if notes else "complete"
            if failures and not run.rows_read:
                run.status = "failed"
    finally:
        run.finished_at = timezone.now()
        run.save()
        schedule = ScheduleConfig.load()
        schedule.last_run = run.started_at
        schedule.save(update_fields=["last_run"])
        _running.release()
    return run


def _record_failure(device: AirCastingDevice, message: str) -> None:
    device.last_error = message[:2000]
    device.save(update_fields=["last_error"])


def _in_background(target) -> bool:
    if _running.locked():
        return False

    def wrapper():
        close_old_connections()
        try:
            target()
        finally:
            close_old_connections()

    threading.Thread(target=wrapper, daemon=True, name="aircasting-sync").start()
    return True


def run_sync_in_background() -> bool:
    return _in_background(run_sync)


def download_device_in_background(device: AirCastingDevice) -> bool:
    """Start the first download for one newly registered sensor, over its whole period."""
    device_id = device.pk

    def target():
        fresh = AirCastingDevice.objects.filter(pk=device_id).first()
        if fresh:
            run_sync([fresh], full=True)

    return _in_background(target)
