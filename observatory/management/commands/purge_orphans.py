"""Remove storage the Data manager no longer lists."""
from django.core.management.base import BaseCommand

from observatory.services import cleanup


class Command(BaseCommand):
    help = "List, and optionally remove, data on disk that the Data manager does not list."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually delete. Without this the command only reports.")
        parser.add_argument("--include-stations", action="store_true",
                            help="Also delete stations whose readings no dataset represents. Destroys readings.")

    def handle(self, *args, **options):
        survey = cleanup.find_orphans()
        if not survey.all:
            self.stdout.write(self.style.SUCCESS("Nothing orphaned: everything stored is listed in the Data manager."))
            return

        for orphan in survey.stations:
            self.stdout.write(self.style.WARNING(
                f"  station  {orphan.label}  {orphan.readings:,} readings  {orphan.size / 1e6:.1f} MB"))
        for orphan in survey.files:
            self.stdout.write(f"  {orphan.kind:14} {orphan.label}  {orphan.size / 1e6:.1f} MB")
        self.stdout.write(f"\n{len(survey.all)} orphaned items, {survey.size / 1e6:.1f} MB.")

        if not options["apply"]:
            self.stdout.write(self.style.NOTICE("Nothing deleted. Re-run with --apply to remove them."))
            return
        if survey.stations and not options["include_stations"]:
            self.stdout.write(self.style.NOTICE(
                "Stations above are kept; add --include-stations to delete their readings too."))

        report = cleanup.purge(include_stations=options["include_stations"], survey=survey)
        self.stdout.write(self.style.SUCCESS(" ".join([report.describe(), *report.notes])))
