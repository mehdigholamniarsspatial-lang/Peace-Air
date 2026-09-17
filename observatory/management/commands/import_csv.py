from django.core.management.base import BaseCommand, CommandError

from ...models import Station
from ...services.ingest import ingest_file


class Command(BaseCommand):
    help = "Import one or more sensor CSV files (AirCasting export or simple long layout)."

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+")
        parser.add_argument("--station", help="Store all rows under this existing station code")

    def handle(self, *args, **opts):
        station = None
        if opts["station"]:
            station = Station.objects.filter(code=opts["station"]).first()
            if not station:
                raise CommandError(f"Station {opts['station']} does not exist")
        for path in opts["paths"]:
            run = ingest_file(path, source="cli", force_station=station)
            style = self.style.ERROR if run.status == "failed" else self.style.SUCCESS
            self.stdout.write(style(f"{path}: {run.status} — read {run.rows_read}, stored {run.rows_stored}, "
                                    f"duplicates {run.rows_duplicate}, rejected {run.rows_rejected}"))
            if run.message:
                self.stdout.write(run.message)
