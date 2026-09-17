import tempfile
from pathlib import Path

import pandas as pd
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.core.management import call_command
from django.urls import reverse

from observatory.models import Dataset, Station
from observatory.services import analytics, storage, units
from observatory.services.ingest import ingest_file


# 32F = 0C, 50F = 10C, 68F = 20C, 86F = 30C — exact conversions keep the assertions readable.
FAHRENHEIT = [32.0, 50.0, 68.0, 86.0]
CELSIUS = [0.0, 10.0, 20.0, 30.0]

TEMPERATURE_CSV = "station,time,measurement,value,unit,latitude,longitude\n" + "".join(
    f"S1,2026-02-10T{i:02d}:00:00,F,{v},F,53.3,-6.2\n" for i, v in enumerate(FAHRENHEIT)
)


class ConversionTests(TestCase):
    def test_fahrenheit_is_recognised_by_unit(self):
        for unit in ("F", "f", "°F", "degF", "Fahrenheit"):
            self.assertTrue(units.is_fahrenheit(unit), unit)

    def test_celsius_and_other_units_are_left_alone(self):
        for unit in ("°C", "C", "ug/m3", "%", "µg/m³"):
            self.assertFalse(units.is_fahrenheit(unit), unit)

    def test_a_reading_with_no_unit_falls_back_to_the_channel_name(self):
        self.assertTrue(units.is_fahrenheit("", "F"))
        self.assertFalse(units.is_fahrenheit("", "PM2.5"))

    def test_converting_twice_does_not_happen(self):
        """The unit is relabelled on conversion, so a second pass is a no-op."""
        frame = pd.DataFrame({"value": FAHRENHEIT, "unit": ["F"] * 4})

        once = units.convert(frame)
        twice = units.convert(once)

        self.assertEqual(once["value"].tolist(), CELSIUS)
        self.assertEqual(twice["value"].tolist(), CELSIUS)
        self.assertEqual(once["unit"].unique().tolist(), ["°C"])

    def test_the_source_frame_is_not_modified(self):
        frame = pd.DataFrame({"value": FAHRENHEIT, "unit": ["F"] * 4})

        units.convert(frame)

        self.assertEqual(frame["value"].tolist(), FAHRENHEIT)
        self.assertEqual(frame["unit"].unique().tolist(), ["F"])

    def test_particulate_readings_are_untouched(self):
        frame = pd.DataFrame({"value": [5.0, 7.0], "unit": ["ug/m3", "ug/m3"]})

        self.assertEqual(units.convert(frame)["value"].tolist(), [5.0, 7.0])


class StoredDataIsUntouchedTests(TestCase):
    def test_the_csv_on_disk_keeps_the_sensor_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "t.csv"
                upload.write_text(TEMPERATURE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")

                stored = storage.read_series("S1", "F")

                self.assertEqual(stored["value"].tolist(), FAHRENHEIT)
                self.assertEqual(stored["unit"].unique().tolist(), ["F"])


class AnalysisInCelsiusTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))

    def _ingest(self, tmp):
        upload = storage.sub_dir("raw", "uploads") / "t.csv"
        upload.write_text(TEMPERATURE_CSV, encoding="utf-8")
        ingest_file(upload, source="upload")

    def test_the_graph_and_statistics_are_in_celsius(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                self._ingest(tmp)

                data = self.client.get(reverse("api_analysis", args=["S1"]),
                                       {"measurement": "F", "aggregation": "raw"}).json()

                self.assertEqual(data["unit"], "°C")
                self.assertEqual(data["series"]["mean"], CELSIUS)
                self.assertEqual(data["stats"]["min"], 0.0)
                self.assertEqual(data["stats"]["max"], 30.0)
                self.assertEqual(data["stats"]["mean"], 15.0)
                self.assertEqual(data["stats"]["median"], 15.0)
                self.assertEqual(data["stats"]["latest"], 30.0)

    def test_the_spread_is_scaled_but_not_shifted(self):
        """A standard deviation must take the 5/9 scale without the 32 degree offset."""
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                self._ingest(tmp)

                stats = self.client.get(reverse("api_analysis", args=["S1"]),
                                        {"measurement": "F"}).json()["stats"]

                fahrenheit_std = pd.Series(FAHRENHEIT).std(ddof=1)
                self.assertAlmostEqual(stats["std"], fahrenheit_std * 5 / 9, places=3)
                self.assertNotAlmostEqual(stats["std"], fahrenheit_std, places=3)

    def test_the_map_popup_reports_celsius(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                self._ingest(tmp)

                feature = self.client.get(reverse("api_stations"), {"measurement": "F"}).json()["features"][0]

                self.assertEqual(feature["properties"]["unit"], "°C")
                self.assertEqual(feature["properties"]["snapshot"]["latest"], 30.0)
                self.assertEqual(feature["properties"]["snapshot"]["mean"], 15.0)

    def test_the_station_detail_labels_the_channel_in_celsius(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                self._ingest(tmp)

                detail = self.client.get(reverse("api_station", args=["S1"])).json()
                temperature = next(m for m in detail["measurements"] if m["key"] == "F")

                self.assertEqual(temperature["unit"], "°C")
                self.assertEqual(temperature["label"], "Temperature")

    def test_particulate_statistics_are_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "pm.csv"
                upload.write_text("station,time,measurement,value,unit\n"
                                  "S1,2026-02-10T00:00:00,PM2.5,5,ug/m3\n"
                                  "S1,2026-02-10T01:00:00,PM2.5,7,ug/m3\n", encoding="utf-8")
                ingest_file(upload, source="upload")

                stats = self.client.get(reverse("api_analysis", args=["S1"]),
                                        {"measurement": "PM2.5"}).json()

                self.assertEqual(stats["unit"], "ug/m3")
                self.assertEqual(stats["stats"]["mean"], 6.0)


class ExportedResultsTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))

    def test_the_csv_export_is_in_celsius(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "t.csv"
                upload.write_text(TEMPERATURE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")

                response = self.client.get(reverse("api_export", args=["S1"]), {"measurement": "F"})
                body = response.content.decode()

                self.assertIn("°C", body)
                self.assertNotIn(",F\r\n", body)
                for celsius in ("0.0", "10.0", "20.0", "30.0"):
                    self.assertIn(celsius, body)

    def test_the_weekly_dataset_file_is_in_celsius(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "t.csv"
                upload.write_text(TEMPERATURE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")

                dataset = Dataset.objects.get(station=Station.objects.get(code="S1"))
                rows = pd.read_csv(storage.sub_dir("datasets") / dataset.filename)

                self.assertEqual(sorted(rows["value"].tolist()), CELSIUS)
                self.assertEqual(rows["unit"].unique().tolist(), ["°C"])


class RebuildDatasetsCommandTests(TestCase):
    """Files written before the change still hold Fahrenheit until they are rebuilt."""

    def test_rebuilding_restates_an_older_dataset_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                upload = storage.sub_dir("raw", "uploads") / "t.csv"
                upload.write_text(TEMPERATURE_CSV, encoding="utf-8")
                ingest_file(upload, source="upload")
                dataset = Dataset.objects.get()
                path = storage.sub_dir("datasets") / dataset.filename
                # Stand in for a file generated before temperatures were converted.
                stale = pd.read_csv(path).assign(value=FAHRENHEIT, unit="F")
                stale.to_csv(path, index=False)

                call_command("rebuild_datasets")

                rows = pd.read_csv(storage.sub_dir("datasets") / Dataset.objects.get().filename)
                self.assertEqual(sorted(rows["value"].tolist()), CELSIUS)
                self.assertEqual(rows["unit"].unique().tolist(), ["°C"])
                # The readings themselves are still exactly what the sensor sent.
                self.assertEqual(storage.read_series("S1", "F")["value"].tolist(), FAHRENHEIT)

    def test_rebuilding_a_station_with_no_readings_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(OBSERVATORY_DATA_DIR=Path(tmp)):
                Station.objects.create(code="EMPTY", name="Empty site")

                call_command("rebuild_datasets")

                self.assertFalse(Dataset.objects.exists())


class MeasurementLabelTests(TestCase):
    """The channel is stored as "F"; nothing shown to a person may read as Fahrenheit."""

    def test_neither_page_labels_the_channel_by_its_storage_key(self):
        for script in ("map.js",):
            source = (Path("observatory/static/observatory/js") / script).read_text(encoding="utf-8")
            self.assertIn("fmt.unit(m.unit)", source, script)
            self.assertNotIn("${m.label} (${m.key})", source, script)
