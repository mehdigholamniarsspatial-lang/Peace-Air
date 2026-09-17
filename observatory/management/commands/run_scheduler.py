import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from ...models import ScheduleConfig
from ...services.sync import close_interrupted_runs, run_sync


class Command(BaseCommand):
    help = ("Long-running worker that performs scheduled AirCasting imports using the interval set on the "
            "Data manager page. Run it alongside the web server (or use cron with fetch_aircasting).")

    def handle(self, *args, **opts):
        settled = close_interrupted_runs()
        if settled:
            self.stdout.write(f"Marked {settled} interrupted download(s) as failed.")
        self.stdout.write("Scheduler started. Press Ctrl+C to stop.")
        while True:
            schedule = ScheduleConfig.load()
            due = schedule.enabled and (schedule.last_run is None or
                                        (timezone.now() - schedule.last_run).total_seconds() >= schedule.interval_minutes * 60)
            if due:
                run = run_sync()
                if run:
                    self.stdout.write(f"{timezone.now():%H:%M} sync {run.status}: {run.rows_stored} new readings")
            time.sleep(30)
