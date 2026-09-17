"""Mobile recordings: the per-session export, and the track it draws on the map.

A fixed station is a point — one coordinate repeated on every reading. A mobile session
is a path, and these tests pin down the three things that have to hold for one to arrive
intact: the export's own shape is understood, the sensor behind it is recognised as one
already known, and the readings keep the coordinate each was taken at.
"""
import shutil
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd
from django.test import TestCase, override_settings
from django.urls import reverse

from observatory.aircasting.client import AirCastingDownloader, encoded_ms
from observatory.api import _has_track
from observatory.models import AirCastingDevice, DeviceAlias, Station
from observatory.services import analytics
from observatory.services.ingest import ingest_file, read_session_export
from observatory.services.sync import run_sync
from observatory.tests.test_pipeline import TempDataMixin

SAMPLE = Path(__file__).resolve().parents[2] / "sample_data" / "airbeam3_sample.csv"

HEADER = "\n".join([
    ",,,,,Sensor_Package_Name,Sensor_Package_Name",
    ",,,,,AirBeam3:b0b21c7627c4,AirBeam3:b0b21c7627c4",
    ",,,,,Sensor_Name,Sensor_Name",
    ",,,,,AirBeam3-PM2.5,AirBeam3-F",
    ",,,,,Measurement_Type,Measurement_Type",
    ",,,,,Particulate Matter,Temperature",
    ",,,,,Measurement_Units,Measurement_Units",
    ",,,,,micrograms per cubic meter,degrees Fahrenheit",
    "ObjectID,Session_Name,Timestamp,Latitude,Longitude,1:Measurement_Value,2:Measurement_Value",
])
# Five fixes walking east, with PM2.5 crossing the 12 µg/m³ band edge and 32 °F = 0 °C.
PM = [2.0, 8.0, 20.0, 60.0, 3.0]


def session_csv(name="Walk 1", start_second=9, lat=53.2800, lon=-9.0500):
    rows = [
        f"{i + 1},{name},2026-09-12T15:15:{start_second + i:02d}.000,"
        f"{lat + i * 0.001:.4f},{lon + i * 0.001:.4f},{PM[i]},32.0"
        for i in range(len(PM))
    ]
    return HEADER + "\n" + "\n".join(rows) + "\n"


class SessionExportTests(TempDataMixin, TestCase):
    def write_session(self, filename="Walk_1_1973333__20260917-2645435-5zf3xb.csv", **kwargs):
        path = Path(self.tmp) / filename
        path.write_text(session_csv(**kwargs), encoding="utf-8")
        return path

    def test_metadata_block_becomes_the_long_layout(self):
        frame = read_session_export(self.write_session())
        self.assertEqual(len(frame), 10)  # five fixes × two channels
        self.assertEqual(set(frame["sensor_name"]), {"AirBeam3-PM2.5", "AirBeam3-F"})
        self.assertEqual(set(frame["device_group"]), {"AirBeam3:b0b21c7627c4"})
        # The export spells its units out; storage and display use symbols.
        self.assertEqual(set(frame.loc[frame["sensor_name"] == "AirBeam3-PM2.5", "unit"]), {"µg/m³"})
        self.assertEqual(set(frame.loc[frame["sensor_name"] == "AirBeam3-F", "unit"]), {"F"})
        self.assertEqual(set(frame["coordinate_source"]), {"measurement"})

    def test_an_ordinary_csv_is_left_to_the_ordinary_reader(self):
        self.assertIsNone(read_session_export(SAMPLE))

    def test_session_id_is_taken_from_the_download_name(self):
        frame = read_session_export(self.write_session())
        self.assertEqual(set(frame["session_id"]), {"1973333"})

    def test_a_renamed_file_falls_back_to_the_session_name(self):
        frame = read_session_export(self.write_session(filename="my-walk.csv"))
        self.assertEqual(set(frame["session_id"]), {"Walk 1"})

    def test_import_keeps_a_coordinate_for_every_reading(self):
        run = ingest_file(self.write_session())
        self.assertEqual(run.status, "complete")
        self.assertEqual(run.rows_stored, 10)
        station = Station.objects.get()
        self.assertEqual(station.code, "AB3-7627C4")
        self.assertEqual(station.location_source, "measurement_median")
        pm = next(m for m in station.measurements if m["key"] == "PM2.5")
        self.assertEqual(pm["positions"], len(PM))
        self.assertTrue(_has_track(station))

    def test_re_importing_the_same_session_stores_nothing_new(self):
        first = ingest_file(self.write_session())
        again = ingest_file(self.write_session())
        self.assertEqual(again.rows_stored, 0)
        self.assertEqual(again.rows_duplicate, first.rows_stored)

    def test_both_spellings_of_one_sensor_are_the_same_station(self):
        """The downloader writes ``query:AirBeam3-b0b21c7627c4;…`` and the session export
        ``AirBeam3:b0b21c7627c4``. Coordinates cannot reconcile them — the fixed session
        is indoor and has none — so the hardware address has to."""
        ingest_file(SAMPLE)
        ingest_file(self.write_session())
        self.assertEqual(Station.objects.count(), 1)
        self.assertEqual(DeviceAlias.objects.count(), 2)
        station = Station.objects.get()
        self.assertEqual(station.code, "AB3-7627C4")
        self.assertTrue(station.has_location)   # placed by the mobile readings
        self.assertTrue(station.is_indoor)      # and still the sensor that recorded indoors

    def test_temperature_is_converted_on_the_way_out(self):
        ingest_file(self.write_session())
        track = analytics.track(analytics.Query(station="AB3-7627C4", measurement="F"))
        self.assertEqual(track["unit"], "°C")
        self.assertEqual(track["sessions"][0]["v"], [0.0] * len(PM))


class TrackTests(TempDataMixin, TestCase):
    def write_session(self, filename, **kwargs):
        path = Path(self.tmp) / filename
        path.write_text(session_csv(**kwargs), encoding="utf-8")
        return path

    def track(self, **kwargs):
        return analytics.track(analytics.Query(station="AB3-7627C4", measurement="PM2.5"), **kwargs)

    def test_the_path_comes_back_in_time_order_with_its_readings(self):
        ingest_file(self.write_session("Walk_1_1973333__x.csv"))
        track = self.track()
        self.assertEqual(track["points"], len(PM))
        self.assertEqual(track["returned"], len(PM))
        session = track["sessions"][0]
        self.assertEqual(session["id"], "1973333")
        self.assertEqual(session["count"], len(PM))
        self.assertEqual(session["v"], PM)
        self.assertEqual(session["t"], sorted(session["t"]))
        self.assertEqual(len(session["lat"]), len(session["lon"]))
        self.assertEqual(session["lat"][0], 53.28)
        self.assertEqual(track["bounds"], [[53.28, -9.05], [53.284, -9.046]])

    def test_each_recording_is_its_own_path(self):
        ingest_file(self.write_session("Walk_1_1973333__x.csv"))
        ingest_file(self.write_session("Walk_2_1973334__x.csv", name="Walk 2", start_second=40, lat=53.30))
        track = self.track()
        self.assertEqual([s["id"] for s in track["sessions"]], ["1973333", "1973334"])
        self.assertEqual(track["points"], 2 * len(PM))

    def test_thresholds_follow_the_published_scale_for_the_sensor(self):
        ingest_file(self.write_session("Walk_1_1973333__x.csv"))
        thresholds = self.track()["thresholds"]
        self.assertEqual(thresholds["source"], "aircasting")
        self.assertEqual([thresholds["low"], thresholds["middle"], thresholds["high"]], [12.0, 35.0, 55.0])

    def test_thresholds_fall_back_to_the_readings_themselves(self):
        """A measurement with no published scale still needs a legend, and one with four
        different numbers on it: the quartiles are forced apart where they coincide."""
        rows = pd.DataFrame({"station": "S1", "time": pd.date_range("2026-09-01", periods=8, freq="min"),
                             "measurement": "NO2", "unit": "ppb", "value": [7.0] * 8,
                             "latitude": [53.0 + i * 0.001 for i in range(8)], "longitude": -9.0})
        path = Path(self.tmp) / "no2.csv"
        rows.to_csv(path, index=False)
        ingest_file(path)
        thresholds = analytics.track(analytics.Query(station="S1", measurement="NO2"))["thresholds"]
        self.assertEqual(thresholds["source"], "readings")
        edges = [thresholds["min"], thresholds["low"], thresholds["middle"], thresholds["high"], thresholds["max"]]
        self.assertEqual(edges, sorted(set(edges)))

    def test_a_long_recording_is_thinned_but_keeps_its_ends(self):
        ingest_file(self.write_session("Walk_1_1973333__x.csv"))
        track = self.track(max_points=2)
        session = track["sessions"][0]
        self.assertEqual(track["points"], len(PM))
        self.assertLess(track["returned"], len(PM))
        self.assertEqual(session["v"][0], PM[0])
        self.assertEqual(session["v"][-1], PM[-1])
        self.assertEqual(session["count"], len(PM))   # the count is of what was recorded

    def test_a_fixed_station_has_no_track_to_draw(self):
        times = pd.date_range("2026-09-01", periods=12, freq="10min")
        rows = pd.DataFrame({"station": "GW-014", "time": times, "measurement": "PM2.5", "unit": "µg/m³",
                             "value": 5.0, "latitude": 53.2743, "longitude": -9.0514})
        path = Path(self.tmp) / "fixed.csv"
        rows.to_csv(path, index=False)
        ingest_file(path)
        station = Station.objects.get(code="GW-014")
        self.assertEqual(station.measurements[0]["positions"], 1)
        self.assertFalse(_has_track(station))


class TrackApiTests(TempDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        path = Path(self.tmp) / "Walk_1_1973333__x.csv"
        path.write_text(session_csv(), encoding="utf-8")
        ingest_file(path)

    def url(self, code="AB3-7627C4"):
        return reverse("api_track", args=[code])

    def test_the_track_is_public_and_carries_the_path(self):
        response = self.client.get(self.url(), {"measurement": "PM2.5"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["measurement"], "PM2.5")
        self.assertEqual(data["unit"], "µg/m³")
        self.assertEqual(data["sessions"][0]["v"], PM)

    def test_the_window_filters_the_path(self):
        empty = self.client.get(self.url(), {"measurement": "PM2.5", "start": "2026-09-13"}).json()
        self.assertEqual(empty["points"], 0)
        self.assertEqual(empty["sessions"], [])
        self.assertIsNone(empty["bounds"])
        # An end date is inclusive, so the day of the recording still returns it.
        same_day = self.client.get(self.url(), {"measurement": "PM2.5", "start": "2026-09-12",
                                                "end": "2026-09-12"}).json()
        self.assertEqual(same_day["points"], len(PM))

    def test_a_measurement_with_no_readings_answers_empty_rather_than_failing(self):
        data = self.client.get(self.url(), {"measurement": "NO2"}).json()
        self.assertEqual(data["points"], 0)
        self.assertEqual(data["sessions"], [])

    def test_an_unknown_station_is_not_found(self):
        self.assertEqual(self.client.get(self.url("NOPE")).status_code, 404)

    def test_the_station_list_says_which_stations_moved(self):
        data = self.client.get(reverse("api_stations")).json()
        properties = data["features"][0]["properties"]
        self.assertEqual(properties["code"], "AB3-7627C4")
        self.assertTrue(properties["has_track"])


class AutomaticSyncTests(TestCase):
    """The scheduled AirCasting sync, offline: does a mobile recording arrive as a track?

    The question this answers is not whether a downloaded file can be imported by hand,
    but whether the path that runs unattended — discover, retrieve, export, import —
    carries a coordinate per reading all the way to the map. A fake API stands in for
    aircasting.org; everything between it and the track is the real code.
    """
    PATH = [(53.2820, -9.0450, 2.0), (53.2818, -9.0462, 9.0), (53.2815, -9.0471, 41.0),
            (53.2810, -9.0483, 18.0), (53.2806, -9.0495, 4.0)]
    SESSION_ID, STREAM_ID = 1973333, 2924457

    class FakeClient:
        """Discovery returns one mobile session; retrieval returns its measurements."""

        def __init__(self, outer):
            self.outer = outer
            self.calls = []

        def get(self, path, params=None, token=None, refresh=None):
            self.calls.append({"path": path})
            if path == "/api/v3/sessions":
                if params.get("sensor_package_name") != "AirBeam3:b0b21c7627c4":
                    return {"sessions": []}
                return {"sessions": [{"id": self.outer.SESSION_ID, "type": "MobileSession",
                                      "start_datetime": "2026-09-12T15:15:09",
                                      "streams": [{"id": self.outer.STREAM_ID, "sensor_name": "AirBeam3-PM2.5"}]}]}
            if path == f"/api/mobile/sessions2/{self.outer.SESSION_ID}.json":
                readings = [{"time": encoded_ms(f"2026-09-12T15:15:{9 + i:02d}"), "value": value,
                             "latitude": lat, "longitude": lon}
                            for i, (lat, lon, value) in enumerate(self.outer.PATH)]
                return {"streams": {"AirBeam3-PM2.5": {
                    "id": self.outer.STREAM_ID, "sensor_package_name": "AirBeam3:b0b21c7627c4",
                    "sensor_name": "AirBeam3-PM2.5", "measurement_type": "Particulate Matter",
                    "unit_symbol": "µg/m³", "measurements_count": len(readings), "measurements": readings}}}
            raise AssertionError(path)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(OBSERVATORY_DATA_DIR=Path(self.tmp))
        self.override.enable()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self.override.disable)

    def test_a_scheduled_sync_lands_a_mobile_recording_as_a_track(self):
        device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4",
                                                 download_from=date(2026, 9, 1),
                                                 download_until=date(2026, 9, 30))
        fake = self.FakeClient(self)
        with mock.patch("observatory.services.sync.AirCastingDownloader",
                        lambda cfg: AirCastingDownloader(cfg, client=fake)):
            run = run_sync([device])

        self.assertEqual(run.status, "complete", run.message)
        self.assertEqual(run.rows_stored, len(self.PATH))

        station = Station.objects.get()
        self.assertEqual(station.code, "AB3-7627C4")
        self.assertEqual(station.location_source, "measurement_median")
        self.assertEqual(station.measurements[0]["positions"], len(self.PATH))
        self.assertTrue(_has_track(station))

        track = analytics.track(analytics.Query(station=station.code, measurement="PM2.5"))
        self.assertEqual(track["points"], len(self.PATH))
        session = track["sessions"][0]
        self.assertEqual(session["id"], str(self.SESSION_ID))
        self.assertEqual(session["lat"], [lat for lat, _, _ in self.PATH])
        self.assertEqual(session["lon"], [lon for _, lon, _ in self.PATH])
        self.assertEqual(session["v"], [value for _, _, value in self.PATH])

    def test_syncing_again_does_not_draw_the_same_path_twice(self):
        """A recording already downloaded must not accumulate on every tick.

        The second sync is asked for the whole period rather than the incremental window,
        because an incremental one restarts near the last success and would simply find
        nothing to fetch — which would prove the de-duplication nothing.
        """
        device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4",
                                                 download_from=date(2026, 9, 1),
                                                 download_until=date(2026, 9, 30))
        fake = self.FakeClient(self)
        patched = mock.patch("observatory.services.sync.AirCastingDownloader",
                             lambda cfg: AirCastingDownloader(cfg, client=fake))
        with patched:
            run_sync([device])
            again = run_sync([AirCastingDevice.objects.get(pk=device.pk)], full=True)
        self.assertEqual(again.rows_stored, 0)
        self.assertEqual(again.rows_duplicate, len(self.PATH))
        self.assertEqual(Station.objects.get().reading_count, len(self.PATH))
        track = analytics.track(analytics.Query(station="AB3-7627C4", measurement="PM2.5"))
        self.assertEqual(len(track["sessions"]), 1)
        self.assertEqual(track["points"], len(self.PATH))

    def test_an_incremental_sync_leaves_an_older_recording_alone(self):
        """Routine ticks restart a couple of days before the last success, so a recording
        from before that window is neither refetched nor lost."""
        device = AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4",
                                                 download_from=date(2026, 9, 1),
                                                 download_until=date(2026, 9, 30))
        fake = self.FakeClient(self)
        patched = mock.patch("observatory.services.sync.AirCastingDownloader",
                             lambda cfg: AirCastingDownloader(cfg, client=fake))
        with patched:
            run_sync([device])
            run_sync([AirCastingDevice.objects.get(pk=device.pk)])
        self.assertEqual(Station.objects.get().reading_count, len(self.PATH))
        self.assertTrue(_has_track(Station.objects.get()))
