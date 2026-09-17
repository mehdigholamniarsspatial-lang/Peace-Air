import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from observatory.models import AirCastingDevice, DeviceAlias, ImportRun, Station
from observatory.services import sync


class DownloadPeriodTests(TestCase):
    def test_config_uses_the_configured_start_and_end(self):
        device = AirCastingDevice(device_id="AIRBEAM3:B0B21C7627C4",
                                  download_from=date(2026, 2, 1), download_until=date(2026, 3, 1))

        config = sync.build_config(device)

        self.assertEqual(config.discovery_start, "2026-02-01")
        self.assertEqual(config.download_from, "2026-02-01")
        self.assertEqual(config.download_until, "2026-03-01")

    def test_an_empty_end_date_means_now(self):
        device = AirCastingDevice(device_id="AIRBEAM3:B0B21C7627C4", download_from=date(2026, 2, 1))

        config = sync.build_config(device)

        self.assertEqual(config.download_until, (datetime.now() + timedelta(days=1)).date().isoformat())

    def test_later_syncs_narrow_discovery_too(self):
        """Re-searching every year since the start date on each tick is what made syncs overrun."""
        device = AirCastingDevice(device_id="AIRBEAM3:B0B21C7627C4", download_from=date(2020, 1, 1),
                                  last_success=timezone.now())

        incremental = sync.build_config(device)
        overlap = timedelta(days=1 + __import__("django").conf.settings.AIRCASTING_SYNC_OVERLAP_DAYS)

        self.assertNotEqual(incremental.discovery_start, "2020-01-01")
        self.assertGreaterEqual(incremental.discovery_start, (timezone.now() - overlap).date().isoformat())
        self.assertEqual(incremental.discovery_start, incremental.download_from)

    def test_a_first_full_download_ignores_the_last_success(self):
        device = AirCastingDevice(device_id="AIRBEAM3:B0B21C7627C4", download_from=date(2026, 2, 1),
                                  last_success=timezone.now())

        self.assertEqual(sync.build_config(device, full=True).discovery_start, "2026-02-01")


class InterruptedRunTests(TestCase):
    def test_a_run_left_running_by_a_restart_is_settled(self):
        stale = ImportRun.objects.create(source="aircasting", status="running", filename="AirCasting sync")
        ImportRun.objects.filter(pk=stale.pk).update(
            started_at=timezone.now() - sync.ABANDONED_AFTER - timedelta(minutes=1))

        self.assertEqual(sync.close_interrupted_runs(), 1)

        stale.refresh_from_db()
        self.assertEqual(stale.status, "failed")
        self.assertIsNotNone(stale.finished_at)
        self.assertIn("Interrupted", stale.message)

    def test_a_run_stamped_with_a_finish_time_but_still_running_is_settled(self):
        """The ``finally`` stamped a finish time while an error escaped before the status."""
        stale = ImportRun.objects.create(source="aircasting", status="running",
                                         finished_at=timezone.now())

        self.assertEqual(sync.close_interrupted_runs(), 1)

        stale.refresh_from_db()
        self.assertEqual(stale.status, "failed")

    def test_a_download_that_started_moments_ago_is_left_running(self):
        live = ImportRun.objects.create(source="aircasting", status="running")

        self.assertEqual(sync.close_interrupted_runs(), 0)

        live.refresh_from_db()
        self.assertEqual(live.status, "running")

    def test_finished_runs_are_left_alone(self):
        done = ImportRun.objects.create(source="aircasting", status="complete", finished_at=timezone.now())

        sync.close_interrupted_runs()

        done.refresh_from_db()
        self.assertEqual(done.status, "complete")


class AddDeviceStartsDownloadTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))

    def _post(self, **extra):
        data = {"device_id": "AIRBEAM3:B0B21C7627C4", "label": "", "known_session_ids": "",
                "project_tags": "", "download_from": "2026-02-01", "download_until": "",
                "station": "", "active": "on"}
        data.update(extra)
        return self.client.post(reverse("device_add"), data)

    def test_adding_a_sensor_starts_its_download(self):
        with patch("observatory.views.download_device_in_background", return_value=True) as started:
            response = self._post(download_until="2026-03-01")

        self.assertRedirects(response, reverse("data"))
        device = AirCastingDevice.objects.get(device_id="AIRBEAM3:B0B21C7627C4")
        self.assertEqual(device.download_from, date(2026, 2, 1))
        self.assertEqual(device.download_until, date(2026, 3, 1))
        started.assert_called_once()
        self.assertEqual(started.call_args.args[0].pk, device.pk)
        message = str(list(response.wsgi_request._messages)[0])
        self.assertIn("started downloading", message)
        self.assertIn("2026-02-01", message)
        self.assertIn("2026-03-01", message)

    def test_without_an_end_date_the_message_says_now(self):
        with patch("observatory.views.download_device_in_background", return_value=True):
            response = self._post()

        self.assertIsNone(AirCastingDevice.objects.get().download_until)
        self.assertIn("to now", str(list(response.wsgi_request._messages)[0]))

    def test_the_end_date_must_not_precede_the_start(self):
        with patch("observatory.views.download_device_in_background") as started:
            response = self._post(download_until="2026-01-01")

        self.assertRedirects(response, reverse("data"))
        self.assertFalse(AirCastingDevice.objects.exists())
        started.assert_not_called()
        self.assertIn("on or after the start date", str(list(response.wsgi_request._messages)[0]))

    def test_a_busy_downloader_queues_the_sensor_instead(self):
        with patch("observatory.views.download_device_in_background", return_value=False):
            response = self._post()

        self.assertTrue(AirCastingDevice.objects.exists())
        self.assertIn("already running", str(list(response.wsgi_request._messages)[0]))


class FinishedSensorTests(TestCase):
    """A closed date range that is fully downloaded is finished, not broken."""

    def test_a_fully_downloaded_closed_range_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                device = AirCastingDevice.objects.create(
                    device_id="AIRBEAM3:B0B21C7627C4", download_from=date(2026, 9, 11),
                    download_until=date(2026, 9, 13), last_success=timezone.now(),
                    last_error="something old")

                with patch("observatory.services.sync.AirCastingDownloader") as downloader:
                    run = sync.run_sync([device])
                    downloader.assert_not_called()

                device.refresh_from_db()
                self.assertNotEqual(run.status, "failed")
                self.assertIn("already downloaded up to 2026-09-13", run.message)
                self.assertEqual(device.last_error, "")
                self.assertEqual(device.status, "ready")


class QuietSensorTests(TestCase):
    def _no_recordings(self, device):
        with patch("observatory.services.sync.AirCastingDownloader") as downloader:
            downloader.return_value.run.return_value = (None, {"discovery_errors": []})
            return sync.run_sync([device])

    def test_an_empty_first_download_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4")

                run = self._no_recordings(device)

                device.refresh_from_db()
                self.assertEqual(run.status, "failed")
                self.assertEqual(device.status, "failed")

    def test_a_sensor_that_has_worked_before_stays_ready_when_nothing_is_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4",
                                                         last_success=timezone.now(),
                                                         last_error="an error from before")

                run = self._no_recordings(device)

                device.refresh_from_db()
                self.assertNotEqual(run.status, "failed")
                self.assertEqual(device.last_error, "")
                self.assertEqual(device.status, "ready")
                self.assertIn("No recordings found", run.message)

    def test_a_broken_search_still_fails_even_after_earlier_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4",
                                                         last_success=timezone.now())

                with patch("observatory.services.sync.AirCastingDownloader") as downloader:
                    downloader.return_value.run.return_value = (
                        None, {"discovery_errors": [{"error": "HTTP 503"}]})
                    run = sync.run_sync([device])

                device.refresh_from_db()
                self.assertEqual(run.status, "failed")
                self.assertIn("HTTP 503", device.last_error)


class SensorListTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))

    def test_the_data_manager_lists_registered_sensors(self):
        station = Station.objects.create(code="AB3-7627C4", name="AirBeam3 7627C4", reading_count=21720)
        AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4", label="Office window",
                                        station=station, download_from=date(2026, 2, 1))

        page = self.client.get(reverse("data"))
        rows = self.client.get(reverse("api_devices")).json()["results"]

        self.assertContains(page, 'id="sensor-rows"')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_id"], "AIRBEAM3:B0B21C7627C4")
        self.assertEqual(rows[0]["station"], "AB3-7627C4")
        self.assertEqual(rows[0]["readings"], 21720)
        self.assertEqual(rows[0]["download_from"], "2026-02-01")
        self.assertIsNone(rows[0]["download_until"])
        self.assertEqual(rows[0]["status"], "new")

    def test_readings_are_found_through_the_source_identity(self):
        """Imports match a sensor by device_group, so the FK is usually empty."""
        station = Station.objects.create(code="AB3-7627C4", name="AirBeam3 7627C4", reading_count=81050)
        DeviceAlias.objects.create(key="AirBeam3:b0b21c7627c4", station=station)
        AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4")

        row = self.client.get(reverse("api_devices")).json()["results"][0]

        self.assertEqual(row["station"], "AB3-7627C4")
        self.assertEqual(row["readings"], 81050)

    def test_status_reflects_the_last_attempt(self):
        device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4")
        self.assertEqual(device.status, "new")

        device.last_attempt = timezone.now()
        self.assertEqual(device.status, "waiting")

        device.last_error = "HTTP 404 at /api/v3/sessions"
        self.assertEqual(device.status, "failed")

        device.last_error = ""
        device.last_success = timezone.now()
        self.assertEqual(device.status, "ready")

        device.active = False
        self.assertEqual(device.status, "paused")

    def test_a_failed_download_is_recorded_against_the_sensor(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4")

                with patch("observatory.services.sync.AirCastingDownloader", side_effect=RuntimeError("HTTP 403")):
                    run = sync.run_sync([device])

                device.refresh_from_db()
                self.assertEqual(run.status, "failed")
                self.assertIn("HTTP 403", device.last_error)
                self.assertEqual(device.status, "failed")
                self.assertIsNotNone(device.last_attempt)
                self.assertContains(self.client.get(reverse("data")), "HTTP 403")

    def test_a_sensor_with_no_recordings_explains_the_period_it_searched(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4",
                                                         download_from=date(2026, 2, 1),
                                                         download_until=date(2026, 3, 1))

                with patch("observatory.services.sync.AirCastingDownloader") as downloader:
                    downloader.return_value.run.return_value = (None, {"discovery_errors": []})
                    sync.run_sync([device])

                device.refresh_from_db()
                self.assertIn("2026-02-01", device.last_error)
                self.assertIn("2026-03-01", device.last_error)

    def test_a_failed_search_window_reports_the_underlying_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4")

                with patch("observatory.services.sync.AirCastingDownloader") as downloader:
                    downloader.return_value.run.return_value = (
                        None, {"discovery_errors": [{"error": "HTTP 429 at /api/v3/sessions"}]})
                    sync.run_sync([device])

                device.refresh_from_db()
                self.assertIn("HTTP 429", device.last_error)
