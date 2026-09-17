import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from observatory.models import Dataset, DeviceAlias, Station
from observatory.services import analytics
from observatory.services.ingest import ingest_file, measurement_key

SAMPLE = Path(__file__).resolve().parents[2] / "sample_data" / "airbeam3_sample.csv"


class TempDataMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(OBSERVATORY_DATA_DIR=Path(self.tmp))
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, frame):
        path = Path(self.tmp) / name
        frame.to_csv(path, index=False)
        return path


class IngestTests(TempDataMixin, TestCase):
    def test_airbeam_sample_import_and_reimport_is_idempotent(self):
        run = ingest_file(SAMPLE)
        self.assertEqual(run.rows_rejected, 0)
        self.assertGreater(run.rows_stored, 0)
        station = Station.objects.get()
        self.assertEqual(station.code, "AB3-7627C4")
        self.assertTrue(station.is_indoor)
        self.assertFalse(station.has_location)  # coordinates withheld indoors
        self.assertEqual({m["key"] for m in station.measurements}, {"PM1", "PM2.5", "PM10", "RH", "F"})
        again = ingest_file(SAMPLE)
        self.assertEqual(again.rows_stored, 0)
        self.assertEqual(again.rows_duplicate, run.rows_read)
        self.assertEqual(Dataset.objects.count(), 1)

    def _simple(self, station, lat, lon, start="2026-09-01", n=48):
        times = pd.date_range(start, periods=n, freq="30min")
        return pd.DataFrame({"station": station, "time": times.strftime("%Y-%m-%dT%H:%M:%S"),
                             "measurement": "PM2.5", "unit": "µg/m³", "value": np.linspace(2, 20, n),
                             "latitude": lat, "longitude": lon})

    def test_same_coordinates_become_one_station(self):
        ingest_file(self.write("a.csv", self._simple("GW-014", 53.27430, -9.05140)))
        ingest_file(self.write("b.csv", self._simple("OTHER-DEVICE", 53.274301, -9.051399, start="2026-09-02")))
        self.assertEqual(Station.objects.count(), 1)
        self.assertEqual(DeviceAlias.objects.count(), 2)
        self.assertEqual(Station.objects.get().reading_count, 96)

    def test_manual_location_is_not_overwritten(self):
        ingest_file(self.write("a.csv", self._simple("S1", 53.0, -8.0)))
        s = Station.objects.get()
        s.latitude, s.longitude, s.location_source = 52.0, -7.0, "manual"
        s.save()
        ingest_file(self.write("b.csv", self._simple("S1", 53.0, -8.0, start="2026-09-05")))
        s.refresh_from_db()
        self.assertEqual((s.latitude, s.location_source), (52.0, "manual"))

    def test_bad_rows_rejected_and_unknown_layout_fails(self):
        frame = self._simple("S1", 53.0, -8.0, n=4).astype(str)
        frame.loc[1, "value"] = "n/a"
        frame.loc[2, "time"] = "not a time"
        run = ingest_file(self.write("bad.csv", frame))
        self.assertEqual((run.rows_stored, run.rows_rejected), (2, 2))
        failed = ingest_file(self.write("x.csv", pd.DataFrame({"foo": [1]})))
        self.assertEqual(failed.status, "failed")

    def test_measurement_key(self):
        self.assertEqual(measurement_key("AirBeam3-PM2.5"), "PM2.5")
        self.assertEqual(measurement_key("NO2"), "NO2")


class AnalyticsTests(TempDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_user("viewer"))
        rng = np.random.default_rng(1)
        times = pd.date_range("2026-09-01", periods=6 * 24 * 7, freq="10min")
        values = np.round(rng.lognormal(2.2, 0.4, len(times)), 2)
        frame = pd.DataFrame({"station": "T1", "time": times.strftime("%Y-%m-%dT%H:%M:%S"), "measurement": "PM2.5",
                              "value": values, "latitude": 53, "longitude": -8})
        ingest_file(self.write("t.csv", frame))
        self.values = values

    def test_statistics_match_numpy(self):
        out = analytics.analyse(analytics.Query("T1", "PM2.5", aggregation="hourly", ci=95))
        s = out["stats"]
        self.assertEqual(s["count"], len(self.values))
        self.assertAlmostEqual(s["mean"], float(np.mean(self.values)), places=3)
        self.assertAlmostEqual(s["median"], float(np.median(self.values)), places=3)
        self.assertAlmostEqual(s["std"], float(np.std(self.values, ddof=1)), places=3)
        self.assertEqual(len(out["series"]["t"]), 24 * 7)
        self.assertEqual(sum(out["histogram"]["counts"]), len(self.values))
        # each hourly CI contains its mean, and 99% is wider than 90%
        ser = out["series"]
        self.assertTrue(all(lo <= m <= hi for lo, m, hi in zip(ser["lower"], ser["mean"], ser["upper"])))
        w90 = analytics.analyse(analytics.Query("T1", "PM2.5", ci=90))["stats"]["mean_ci"]
        w99 = analytics.analyse(analytics.Query("T1", "PM2.5", ci=99))["stats"]["mean_ci"]
        self.assertLess(w90[1] - w90[0], w99[1] - w99[0])

    def test_observation_range_keeps_central_share(self):
        out = analytics.analyse(analytics.Query("T1", "PM2.5", obs_range="central90"))
        lo, hi = np.quantile(self.values, [0.05, 0.95])
        kept = ((self.values >= lo) & (self.values <= hi)).sum()
        self.assertEqual(out["stats"]["count"], kept)
        self.assertGreaterEqual(out["stats"]["min"], round(lo, 4) - 1e-6)
        self.assertAlmostEqual(kept / len(self.values), 0.90, delta=0.01)

    def test_date_window_end_is_inclusive_day(self):
        q = analytics.Query.from_request("T1", {"measurement": "PM2.5", "start": "2026-09-02", "end": "2026-09-02"})
        self.assertEqual(analytics.analyse(q)["stats"]["count"], 144)

    def test_api_geojson_one_feature_per_station(self):
        r = self.client.get("/api/stations/?measurement=PM2.5")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["features"]), 1)
        self.assertEqual(self.client.get("/api/stations/T1/analysis/?ci=42").status_code, 400)
