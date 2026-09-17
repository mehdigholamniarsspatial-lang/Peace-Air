"""Small in-process scheduler for local runserver use."""
import logging
import threading

from django.db import close_old_connections
from django.utils import timezone

from ..models import ScheduleConfig
from .sync import close_interrupted_runs, run_sync

log = logging.getLogger("observatory.scheduler")
_started = False
_guard = threading.Lock()


def _worker():
    # A download killed by a restart leaves a row saying "Running" for ever; settle it first.
    try:
        close_old_connections()
        close_interrupted_runs()
    except Exception:  # noqa: BLE001
        log.exception("Could not reconcile interrupted downloads")
    while True:
        close_old_connections()
        try:
            schedule = ScheduleConfig.load()
            due = schedule.enabled and (
                schedule.last_run is None
                or (timezone.now() - schedule.last_run).total_seconds() >= schedule.interval_minutes * 60
            )
            if due:
                run_sync()
        except Exception:  # noqa: BLE001
            log.exception("Automatic AirCasting update failed")
        finally:
            close_old_connections()
        threading.Event().wait(30)


def start_embedded_scheduler():
    global _started
    with _guard:
        if _started:
            return
        _started = True
        threading.Thread(target=_worker, daemon=True, name="observatory-scheduler").start()
