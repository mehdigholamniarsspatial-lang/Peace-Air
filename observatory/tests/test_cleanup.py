import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from observatory.models import AirCastingDevice, Dataset, ImportRun, Station
from observatory.services import cleanup, storage
from observatory.services.ingest import ingest_file


SIMPLE_CSV = "station,time,measurement,value,unit,latitude,longitude\n" + "".join(
    f"S1,2026-02-{10 + i // 4:02d}T{i % 4:02d}:00:00,PM2.5,{5 + i},ug/m3,53.3,-6.2\n" for i in range(8)
)


class OrphanSurveyTests(TestCase):
    """Anything the Data manager does not list must be findable, and then removable."""

    def test_a_clean_store_reports_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "a.csv"
                upload.write_text(SIMPLE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")

                survey = cleanup.find_orphans()

                self.assertEqual(survey.all, [])

    def test_station_with_readings_but_no_dataset_is_an_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "a.csv"
                upload.write_text(SIMPLE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")
                # Exactly the state the live database was in: catalogue emptied, readings left.
                Dataset.objects.all().delete()

                survey = cleanup.find_orphans()

                self.assertEqual([o.kind for o in survey.stations], ["station"])
                self.assertEqual(survey.stations[0].readings, 8)
                self.assertTrue(survey.stations[0].destroys_readings)

    def test_purge_without_stations_keeps_readings(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "a.csv"
                upload.write_text(SIMPLE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")
                Dataset.objects.all().delete()

                cleanup.purge(include_stations=False)

                self.assertTrue(Station.objects.filter(code="S1").exists())
                self.assertTrue(storage.list_measurement_files("S1"))

    def test_purge_with_stations_leaves_nothing_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "a.csv"
                upload.write_text(SIMPLE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")
                Dataset.objects.all().delete()

                report = cleanup.purge(include_stations=True)

                self.assertEqual(report.readings, 8)
                self.assertFalse(Station.objects.exists())
                self.assertFalse(storage.list_measurement_files("S1"))
                self.assertFalse(upload.exists())
                self.assertEqual(cleanup.find_orphans().all, [])

    def test_stray_files_are_found_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                stray_dir = storage.sub_dir("stations", "GHOST")
                (stray_dir / "PM2_5.csv").write_text("time,value\n2026-02-10T00:00:00,5\n", encoding="utf-8")
                stray_dataset = storage.sub_dir("datasets") / "Ghost_GH-001_2026-02-15.csv"
                stray_dataset.write_text("time,value\n", encoding="utf-8")
                stray_raw = storage.sub_dir("raw", "aircasting", "AIRBEAM3_DEADBEEF")
                (stray_raw / "cached.json").write_text("{}", encoding="utf-8")
                leftover = storage.sub_dir("datasets") / "half_written.csv.tmp"
                leftover.write_text("x", encoding="utf-8")

                kinds = sorted(o.kind for o in cleanup.find_orphans().files)
                self.assertEqual(kinds, ["dataset_file", "raw_device", "station_files", "temp_file"])

                cleanup.purge(include_stations=False)

                self.assertFalse(stray_dir.exists())
                self.assertFalse(stray_dataset.exists())
                self.assertFalse(stray_raw.exists())
                self.assertFalse(leftover.exists())

    def test_catalogue_entry_without_a_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                station = Station.objects.create(code="S1", name="Site one")
                Dataset.objects.create(station=station, period_start="2026-02-09",
                                       period_end="2026-02-15", filename="gone.csv")

                self.assertIn("dataset_row", [o.kind for o in cleanup.find_orphans().files])
                cleanup.purge(include_stations=False)

                self.assertFalse(Dataset.objects.exists())

    def test_registered_sensor_downloads_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4")
                raw = storage.sub_dir("raw", "aircasting", "AIRBEAM3_B0B21C7627C4")
                (raw / "cached.json").write_text("{}", encoding="utf-8")

                cleanup.purge(include_stations=False)

                self.assertTrue(raw.exists())

    def test_purge_refuses_paths_outside_the_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp) / "data"):
                outside = Path(tmp) / "elsewhere.csv"
                outside.write_text("x", encoding="utf-8")

                with self.assertRaises(ValueError):
                    cleanup._remove(cleanup.Orphan(kind="raw_source", label="x", path=outside))

                self.assertTrue(outside.exists())


class DeletionSweepsOrphansTests(TestCase):
    def test_deleting_the_data_also_removes_the_source_file_it_came_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))
                upload = storage.sub_dir("raw", "uploads") / "20260210T000000_a.csv"
                upload.write_text(SIMPLE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")

                self.client.post(reverse("api_datasets_delete"),
                                 data={"ids": list(Dataset.objects.values_list("pk", flat=True))},
                                 content_type="application/json")

                self.assertFalse(Station.objects.exists())
                self.assertFalse(upload.exists())
                self.assertEqual(cleanup.find_orphans().all, [])


class OrphanApiTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))

    def test_preview_lists_orphans_and_purge_removes_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "a.csv"
                upload.write_text(SIMPLE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")
                Dataset.objects.all().delete()

                preview = self.client.get(reverse("api_orphans")).json()
                self.assertEqual(preview["readings"], 8)
                self.assertEqual(len(preview["stations"]), 1)
                self.assertTrue(preview["stations"][0]["destroys_readings"])
                self.assertTrue(Station.objects.exists())  # preview changes nothing

                response = self.client.post(reverse("api_orphans_purge"),
                                            data={"include_stations": True}, content_type="application/json")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["readings"], 8)
                self.assertFalse(Station.objects.exists())
                self.assertTrue(ImportRun.objects.filter(source="deletion", filename="Orphaned data").exists())

    def test_purge_with_nothing_to_do_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                response = self.client.post(reverse("api_orphans_purge"), data={}, content_type="application/json")

                self.assertEqual(response.status_code, 404)
                self.assertIn("nothing to remove", response.json()["error"].lower())

    def test_a_signed_in_non_superuser_cannot_purge(self):
        self.client.force_login(get_user_model().objects.create_user("viewer", password="test"))

        self.assertEqual(self.client.get(reverse("api_orphans")).status_code, 403)
        self.assertEqual(self.client.post(reverse("api_orphans_purge"), data={},
                                          content_type="application/json").status_code, 403)
