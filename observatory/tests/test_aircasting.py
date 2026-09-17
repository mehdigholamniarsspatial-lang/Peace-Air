"""Offline check of the notebook port: a fake API feeds discovery → export → import."""
import shutil
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from observatory.aircasting.client import AirCastingDownloader, DownloadConfig, encoded_ms, package_variants
from observatory.models import Station
from observatory.services.ingest import ingest_file


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None, token=None, refresh=None):
        self.calls.append({"path": path})
        if path == "/api/v3/sessions":
            if params["sensor_package_name"] != "AirBeam3:b0b21c7627c4" or not params["start_datetime"].startswith("2026"):
                return {"sessions": []}
            return {"sessions": [{"id": 21374, "type": "FixedSession", "start_datetime": "2026-09-01T00:00:00",
                                  "streams": [{"id": 1, "sensor_name": "AirBeam3-PM2.5"}]}]}
        if path.endswith("/streams.json"):
            return {"title": "Roof", "is_indoor": False, "latitude": 53.2743, "longitude": -9.0514,
                    "streams": [{"stream_id": 1, "sensor_name": "AirBeam3-PM2.5", "measurement_type": "Particulate Matter", "unit_symbol": "µg/m³"}]}
        if path == "/api/v3/fixed_measurements":
            start, end = params["start_time"], params["end_time"]
            return [{"time": t, "value": 5.0 + i % 7} for i, t in enumerate(range(start, end, 600000))][:50]
        raise AssertionError(path)


class NotebookPortTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.override = override_settings(OBSERVATORY_DATA_DIR=self.tmp / "data")
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_variants(self):
        self.assertIn("AirBeam3:b0b21c7627c4", package_variants("AIRBEAM3:B0B21C7627C4"))
        self.assertIn("AirBeam3-B0B21C7627C4", package_variants("AIRBEAM3:B0B21C7627C4"))

    def test_download_export_and_import(self):
        cfg = DownloadConfig(device_id="AIRBEAM3:B0B21C7627C4", discovery_start="2026-01-01", download_from="2026-09-01",
                             download_until="2026-09-03", output_root=self.tmp / "out")
        downloader = AirCastingDownloader(cfg, client=FakeClient())
        export_dir, manifest = downloader.run()
        self.assertEqual(manifest["status"], "requests_completed_upstream_completeness_unverified")
        self.assertTrue((export_dir / "recordings.zip").exists())
        run = ingest_file(export_dir / "all_recordings.csv")
        self.assertEqual(run.status, "complete")
        station = Station.objects.get()
        self.assertEqual((station.code, station.location_source), ("AB3-7627C4", "fixed_session"))
        self.assertEqual(station.reading_count, 50)
        self.assertEqual(encoded_ms("2026-09-01T00:00:00"), 1788220800000)
