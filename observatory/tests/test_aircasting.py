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


class MobileClient:
    """A sensor whose recording the package search misses, so only its number finds it.

    The fixed endpoint refuses the id, as it does for any mobile recording; the mobile one
    answers. Exactly the case a session number pasted from an aircasting.org map link
    produces.
    """
    PATH = [(53.2820, -9.0450, 2.0), (53.2817, -9.0462, 14.0), (53.2813, -9.0475, 38.0)]
    SESSION_ID, STREAM_ID = 1973333, 2924457

    def __init__(self, fixed_answers=False):
        self.fixed_answers = fixed_answers
        self.calls = []

    def stream(self, with_measurements):
        stream = {"id": self.STREAM_ID, "sensor_package_name": "AirBeam3:b0b21c7627c4",
                  "sensor_name": "AirBeam3-PM2.5", "measurement_type": "Particulate Matter",
                  "unit_symbol": "µg/m³"}
        if with_measurements:
            readings = [{"time": encoded_ms(f"2026-09-12T15:15:{9 + i:02d}"), "value": value,
                         "latitude": lat, "longitude": lon}
                        for i, (lat, lon, value) in enumerate(self.PATH)]
            stream |= {"measurements": readings, "measurements_count": len(readings)}
        return stream

    def get(self, path, params=None, token=None, refresh=None):
        self.calls.append(path)
        if path == "/api/v3/sessions":
            return {"sessions": []}                      # the search never finds it
        if path.startswith("/api/fixed/sessions/"):
            if self.fixed_answers:
                return {"title": "Roof", "is_indoor": False, "latitude": 53.27, "longitude": -9.05,
                        "streams": [{"stream_id": 77, "sensor_name": "AirBeam3-PM2.5"}]}
            raise RuntimeError("404 Not Found")
        if path == f"/api/mobile/sessions2/{self.SESSION_ID}.json":
            return {"title": "120926 2", "start_time_local": "2026-09-12T15:15:09",
                    "streams": {"AirBeam3-PM2.5": self.stream(with_measurements=True)}}
        raise AssertionError(path)


class KnownSessionTests(TestCase):
    """A session named by number alone: the platform must not assume it was a fixed one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.override = override_settings(OBSERVATORY_DATA_DIR=self.tmp / "data")
        self.override.enable()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self.override.disable)

    def config(self, **kwargs):
        return DownloadConfig(device_id="AIRBEAM3:B0B21C7627C4", discovery_start="2026-09-01",
                              download_from="2026-09-01", download_until="2026-09-30",
                              known_session_ids=[MobileClient.SESSION_ID],
                              output_root=self.tmp / "out", **kwargs)

    def test_a_mobile_session_number_is_resolved_and_downloaded(self):
        client = MobileClient()
        export_dir, manifest = AirCastingDownloader(self.config(), client=client).run()
        self.assertEqual(manifest["discovery_errors"], [])
        self.assertEqual(manifest["sessions_discovered"], 1)
        run = ingest_file(export_dir / "all_recordings.csv")
        self.assertEqual(run.rows_stored, len(MobileClient.PATH))
        station = Station.objects.get()
        self.assertEqual(station.code, "AB3-7627C4")
        # The whole point: every reading kept the place it was taken, so there is a track.
        self.assertEqual(station.location_source, "measurement_median")
        self.assertEqual(station.measurements[0]["positions"], len(MobileClient.PATH))

    def test_the_fixed_endpoint_is_still_asked_first(self):
        client = MobileClient(fixed_answers=True)
        downloader = AirCastingDownloader(self.config(), client=client)
        session, notes = downloader.lookup_session(MobileClient.SESSION_ID)
        self.assertEqual(session["type"], "FixedSession")
        self.assertEqual(notes, [])
        self.assertNotIn(f"/api/mobile/sessions2/{MobileClient.SESSION_ID}.json", client.calls)

    def test_an_empty_fixed_reply_falls_through_to_mobile(self):
        """The fixed endpoint answering 200 with nothing in it is a miss, not a session
        with no streams — otherwise a mobile recording is silently downloaded as empty."""
        class Empty(MobileClient):
            def get(self, path, params=None, token=None, refresh=None):
                if path.startswith("/api/fixed/sessions/"):
                    self.calls.append(path)
                    return {"title": "", "streams": []}
                return super().get(path, params, token, refresh)

        session, _ = AirCastingDownloader(self.config(), client=Empty()).lookup_session(MobileClient.SESSION_ID)
        self.assertEqual(session["type"], "MobileSession")
        self.assertEqual(session["start_datetime"], "2026-09-12T15:15:09")
        self.assertEqual(session["streams"], [{"id": MobileClient.STREAM_ID, "sensor_name": "AirBeam3-PM2.5"}])

    def test_both_kinds_are_named_when_neither_answers(self):
        class Nothing(MobileClient):
            def get(self, path, params=None, token=None, refresh=None):
                if path == "/api/v3/sessions":
                    return {"sessions": []}
                raise RuntimeError("410 Gone")

        downloader = AirCastingDownloader(self.config(), client=Nothing())
        session, notes = downloader.lookup_session(MobileClient.SESSION_ID)
        self.assertIsNone(session)
        self.assertEqual(len(notes), 2)
        self.assertTrue(notes[0].startswith("FixedSession:"), notes)
        self.assertTrue(notes[1].startswith("MobileSession:"), notes)
        _found, errors = downloader.add_known_sessions([], [])
        self.assertIn("MobileSession", errors[0]["error"])

    def test_test_access_says_which_kind_the_session_is(self):
        results = AirCastingDownloader(self.config(), client=MobileClient()).test_access()
        session_result = next(r for r in results if r["query"] == f"session {MobileClient.SESSION_ID}")
        self.assertTrue(session_result["ok"])
        self.assertIn("mobile", session_result["result"])
        self.assertIn("1 streams", session_result["result"])
        self.assertIn(f"AirBeam3-PM2.5={MobileClient.STREAM_ID}", session_result["detail"])
