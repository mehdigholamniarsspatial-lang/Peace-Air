from django.core.management.base import BaseCommand

from ...services import demo
from ...services.ingest import ingest_file


class Command(BaseCommand):
    help = "Create a synthetic 12-station Irish network and import it through the normal pipeline."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14)
        parser.add_argument("--minutes", type=int, default=10, help="Reading interval")
        parser.add_argument("--seed", type=int, default=7)

    def handle(self, *args, **opts):
        for path in demo.build(days=opts["days"], minutes=opts["minutes"], seed=opts["seed"]):
            run = ingest_file(path, source="demo")
            self.stdout.write(f"{path.name}: {run.status}, {run.rows_stored} stored, {run.rows_duplicate} duplicates")
        self.stdout.write(self.style.SUCCESS("Demo network ready."))
