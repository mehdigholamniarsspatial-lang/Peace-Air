"""Remove stored readings taken outside the region the observatory covers."""
from django.core.management.base import BaseCommand

from observatory.services import region
from observatory.services.deletion import find_outside_region, purge_outside_region


class Command(BaseCommand):
    help = ("List, and optionally remove, stored readings whose coordinates fall outside the "
            "region (by default the island of Ireland). Imports are filtered already; this is "
            "for readings stored before that, or after the boundary changed.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually delete. Without this the command only reports.")

    def handle(self, *args, **options):
        if not region.enabled():
            self.stdout.write(self.style.NOTICE(
                "OBSERVATORY_RESTRICT_TO_REGION is off, so nothing is treated as outside the region."))
            return

        survey = find_outside_region()
        if not survey.anything:
            self.stdout.write(self.style.SUCCESS(survey.describe()))
            return

        for code, count in sorted(survey.by_station.items()):
            self.stdout.write(self.style.WARNING(f"  {code:<12} {count:,} readings outside {region.name()}"))
        for code in survey.misplaced:
            self.stdout.write(self.style.WARNING(f"  {code:<12} its own marker is outside {region.name()}"))
        self.stdout.write("\n" + survey.describe())

        if not options["apply"]:
            # Readings cannot be brought back, so the count is shown before anything goes.
            self.stdout.write(self.style.NOTICE("Nothing deleted. Re-run with --apply to remove them."))
            return

        report = purge_outside_region()
        self.stdout.write(self.style.SUCCESS(" ".join([report.describe(), *report.notes])))
