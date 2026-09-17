"""Regenerate the weekly dataset files from the stored readings."""
from django.core.management.base import BaseCommand

from observatory.models import Station
from observatory.services import storage
from observatory.services.ingest import rebuild_datasets, refresh_station_summary, week_start


class Command(BaseCommand):
    help = ("Rebuild every downloadable weekly dataset from the readings on disk. Use this after a change "
            "to how values are presented (temperatures now read in Celsius) so files written earlier match "
            "what the dashboard shows. Stored readings are not modified.")

    def add_arguments(self, parser):
        parser.add_argument("--station", help="Only this station code (default: all).")

    def handle(self, *args, **options):
        stations = Station.objects.all()
        if options["station"]:
            stations = stations.filter(code=options["station"])
            if not stations:
                self.stderr.write(self.style.ERROR(f"No station with code {options['station']}."))
                return

        for station in stations:
            refresh_station_summary(station)
            weeks = set()
            for path in storage.list_measurement_files(station.code):
                frame = storage.read_series(station.code, path.stem)
                weeks.update(week_start(day) for day in frame["time"].dt.date.unique())
            if not weeks:
                self.stdout.write(f"  {station.code}: no readings stored, nothing to build.")
                continue
            rebuild_datasets(station, weeks)
            self.stdout.write(self.style.SUCCESS(
                f"  {station.code}: rebuilt {station.datasets.count()} weekly dataset(s) from "
                f"{station.reading_count:,} readings."))
