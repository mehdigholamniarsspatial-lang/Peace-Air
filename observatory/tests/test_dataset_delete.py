import tempfile
from pathlib import Path

import pandas as pd
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.urls import reverse

from observatory.models import AirCastingDevice, Dataset, DeviceAlias, ImportRun, Station
from observatory.services import storage
from observatory.services.deletion import delete_station_and_data
from observatory.services.ingest import ingest_file


SIMPLE_CSV = "station,time,measurement,value,unit,latitude,longitude\n" + "".join(
    f"S1,2026-02-{10 + i // 4:02d}T{i % 4:02d}:00:00,PM2.5,{5 + i},ug/m3,53.3,-6.2\n" for i in range(8)
)


class DatasetDeleteTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))

    def test_delete_selected_removes_record_and_generated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                station = Station.objects.create(code="AB3-7627C4", name="AirBeam3 7627C4")
                dataset = Dataset.objects.create(
                    station=station,
                    period_start="2026-02-09",
                    period_end="2026-02-15",
                    filename="selected.csv",
                )
                path = Path(tmp) / "datasets" / dataset.filename
                path.parent.mkdir(parents=True)
                path.write_text("time,value\n", encoding="utf-8")
                series = Path(tmp) / "stations" / "AB3_7627C4" / "PM2_5.csv"
                series.parent.mkdir(parents=True)
                rows = pd.DataFrame([
                    {"time": "2026-02-10T12:00:00", "value": 5, "sensor_name": "AirBeam3-PM2.5"},
                    {"time": "2026-02-20T12:00:00", "value": 7, "sensor_name": "AirBeam3-PM2.5"},
                ]).reindex(columns=storage.STATION_COLUMNS, fill_value="")
                rows.to_csv(series, index=False)

                response = self.client.post(
                    reverse("api_datasets_delete"),
                    data={"ids": [dataset.pk]},
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["deleted"], 1)
                self.assertEqual(body["readings"], 1)
                self.assertEqual(body["stations_removed"], [])
                self.assertFalse(Dataset.objects.filter(pk=dataset.pk).exists())
                self.assertFalse(path.exists())
                self.assertTrue(Station.objects.filter(pk=station.pk).exists())
                station.refresh_from_db()
                self.assertEqual(station.reading_count, 1)
                self.assertEqual(len(pd.read_csv(series)), 1)

    def test_delete_requires_a_selection(self):
        response = self.client.post(
            reverse("api_datasets_delete"), data={"ids": []}, content_type="application/json"
        )

        self.assertEqual(response.status_code, 400)

    def test_delete_of_already_deleted_datasets_says_so(self):
        response = self.client.post(
            reverse("api_datasets_delete"), data={"ids": [9876]}, content_type="application/json"
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("already been deleted", response.json()["error"])

    def test_deleting_the_last_dataset_removes_the_empty_station(self):
        """An emptied station used to stay on the map with 0 readings and no explanation."""
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "20260210T000000_a.csv"
                upload.write_text(SIMPLE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")
                station = Station.objects.get(code="S1")
                self.assertEqual(station.reading_count, 8)

                response = self.client.post(
                    reverse("api_datasets_delete"),
                    data={"ids": list(Dataset.objects.values_list("pk", flat=True))},
                    content_type="application/json",
                )

                body = response.json()
                self.assertEqual(body["readings"], 8)
                self.assertEqual(body["stations_removed"], ["S1"])
                self.assertFalse(Station.objects.exists())
                self.assertFalse(DeviceAlias.objects.exists())
                self.assertFalse(Dataset.objects.exists())
                self.assertEqual(self.client.get(reverse("api_stations")).json()["features"], [])
                self.assertEqual(self.client.get(reverse("api_summary")).json()["stored_readings"], 0)

    def test_deletion_is_recorded_in_update_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                station = Station.objects.create(code="S1", name="Site one", reading_count=0)

                delete_station_and_data(station)

                run = ImportRun.objects.get(source="deletion")
                self.assertEqual(run.status, "complete")
                self.assertIn("S1", run.filename)
                self.assertIn("Deleted", run.message)
                self.assertContains(self.client.get(reverse("reports")), "Deletion")

    def test_deleting_a_station_drops_it_from_earlier_import_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                station = Station.objects.create(code="S1", name="Site one")
                run = ImportRun.objects.create(source="upload", status="complete", stations=["S1", "S2"])

                delete_station_and_data(station)

                run.refresh_from_db()
                self.assertEqual(run.stations, ["S2"])


class DataManagerAdminTests(TestCase):
    """Sensor and station management moved out of Settings and into the data manager."""

    def test_the_data_manager_offers_delete_controls_to_a_superuser(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))
        station = Station.objects.create(code="S1", name="Site one", reading_count=8)
        AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4", station=station)

        response = self.client.get(reverse("data"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("station_delete", args=["S1"]))
        self.assertContains(response, reverse("device_add"))
        self.assertContains(response, "Delete station")
        self.assertContains(response, "This cannot be undone.")


class StationDeleteViewTests(TestCase):
    def test_superuser_can_delete_a_station_and_its_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))
                upload = storage.sub_dir("raw", "uploads") / "a.csv"
                upload.write_text(SIMPLE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")
                station = Station.objects.get(code="S1")
                dataset_files = [storage.sub_dir("datasets") / d.filename for d in station.datasets.all()]
                series_files = storage.list_measurement_files("S1")
                self.assertTrue(series_files)

                response = self.client.post(reverse("station_delete", args=["S1"]))

                self.assertRedirects(response, reverse("data"))
                self.assertFalse(Station.objects.exists())
                self.assertFalse(any(path.exists() for path in series_files))
                self.assertFalse(any(path.exists() for path in dataset_files))
                message = str(list(response.wsgi_request._messages)[0])
                self.assertIn("8 readings", message)
                self.assertIn("S1", message)

    def test_signed_in_non_superuser_cannot_delete_a_station(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                self.client.force_login(get_user_model().objects.create_user("viewer", password="test"))
                Station.objects.create(code="S1", name="Site one")

                response = self.client.post(reverse("station_delete", args=["S1"]))

                self.assertEqual(response.status_code, 302)
                self.assertTrue(Station.objects.filter(code="S1").exists())


class DeviceDeleteTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))

    def test_device_delete_removes_station_datasets_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                station = Station.objects.create(code="AB3-7627C4", name="AirBeam3 7627C4")
                device = AirCastingDevice.objects.create(
                    device_id="AIRBEAM3:B0B21C7627C4", station=station
                )
                dataset = Dataset.objects.create(
                    station=station, period_start="2026-02-09", period_end="2026-02-15",
                    filename="device.csv",
                )
                dataset_file = Path(tmp) / "datasets" / dataset.filename
                dataset_file.parent.mkdir(parents=True)
                dataset_file.write_text("time,value\n", encoding="utf-8")
                station_file = Path(tmp) / "stations" / "AB3_7627C4" / "PM2_5.csv"
                station_file.parent.mkdir(parents=True)
                station_file.write_text("time,value\n", encoding="utf-8")
                raw_file = Path(tmp) / "raw" / "aircasting" / "AIRBEAM3_B0B21C7627C4" / "raw.json"
                raw_file.parent.mkdir(parents=True)
                raw_file.write_text("{}", encoding="utf-8")

                response = self.client.post(reverse("device_delete", args=[device.pk]))

                self.assertRedirects(response, reverse("data"))
                self.assertFalse(AirCastingDevice.objects.filter(pk=device.pk).exists())
                self.assertFalse(Station.objects.filter(pk=station.pk).exists())
                self.assertFalse(dataset_file.exists())
                self.assertFalse(station_file.parent.exists())
                self.assertFalse(raw_file.parent.exists())

    def test_device_delete_also_removes_stations_keyed_by_session_id(self):
        """Exports without a ``device_group`` column key the station as ``session:<id>``."""
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                station = Station.objects.create(code="AB3-7627C4", name="AirBeam3 7627C4")
                DeviceAlias.objects.create(key="session:21374", station=station)
                storage.merge_series(station.code, "PM2.5", pd.DataFrame([{
                    "time": pd.Timestamp("2026-02-10T00:00:00"), "value": 5.0,
                    "sensor_name": "AirBeam3-PM2.5", "session_id": "21374", "stream_id": "1",
                }]))
                device = AirCastingDevice.objects.create(
                    device_id="AIRBEAM3:B0B21C7627C4", known_session_ids="21374"
                )

                self.client.post(reverse("device_delete", args=[device.pk]))

                self.assertFalse(Station.objects.filter(pk=station.pk).exists())
                self.assertEqual(storage.list_measurement_files("AB3-7627C4"), [])

    def test_device_delete_says_when_no_readings_were_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4")

                response = self.client.post(reverse("device_delete", args=[device.pk]))

                message = str(list(response.wsgi_request._messages)[0])
                self.assertIn("No stored readings were linked", message)


class AdminDeleteTests(TestCase):
    """Deleting from /admin/ must clean up files too, or a re-import resurrects the data."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))

    def test_admin_station_delete_removes_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "a.csv"
                upload.write_text(SIMPLE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")
                station = Station.objects.get(code="S1")
                dataset_files = [storage.sub_dir("datasets") / d.filename for d in station.datasets.all()]
                series_files = storage.list_measurement_files("S1")
                self.assertTrue(series_files)

                response = self.client.post(
                    reverse("admin:observatory_station_delete", args=[station.pk]), {"post": "yes"}
                )

                self.assertEqual(response.status_code, 302)
                self.assertFalse(Station.objects.exists())
                self.assertFalse(any(path.exists() for path in series_files))
                self.assertFalse(any(path.exists() for path in dataset_files))

    def test_admin_device_delete_removes_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4")
                raw_dir = storage.sub_dir("raw", "aircasting", "AIRBEAM3_B0B21C7627C4")
                (raw_dir / "cached.json").write_text("{}", encoding="utf-8")

                self.client.post(
                    reverse("admin:observatory_aircastingdevice_delete", args=[device.pk]), {"post": "yes"}
                )

                self.assertFalse(AirCastingDevice.objects.exists())
                self.assertFalse(raw_dir.exists())
