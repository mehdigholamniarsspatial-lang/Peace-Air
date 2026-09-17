import os
import sys

from django.apps import AppConfig


class ObservatoryConfig(AppConfig):
    name = 'observatory'

    def ready(self):
        is_runserver = "runserver" in sys.argv
        is_worker_process = os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv
        if is_runserver and is_worker_process:
            from .services.scheduler import start_embedded_scheduler
            start_embedded_scheduler()
