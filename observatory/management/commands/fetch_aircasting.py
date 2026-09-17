from django.core.management.base import BaseCommand

from ...models import AirCastingDevice
from ...services.sync import run_sync


class Command(BaseCommand):
    help = "Download new recordings for registered AirCasting devices and import them."

    def add_arguments(self, parser):
        parser.add_argument("--add-device", help="Register a device first, e.g. AIRBEAM3:B0B21C7627C4")
        parser.add_argument("--session", action="append", default=[], help="Known session id (repeatable)")

    def handle(self, *args, **opts):
        if opts["add_device"]:
            device, created = AirCastingDevice.objects.get_or_create(device_id=opts["add_device"])
            if opts["session"]:
                device.known_session_ids = ",".join(opts["session"])
                device.save()
            self.stdout.write(f"{'Registered' if created else 'Using'} {device.device_id}")
        run = run_sync()
        if run is None:
            self.stdout.write(self.style.WARNING("A sync is already running."))
            return
        self.stdout.write(f"{run.status}: read {run.rows_read}, stored {run.rows_stored}, duplicates {run.rows_duplicate}")
        if run.message:
            self.stdout.write(run.message)
