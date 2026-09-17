"""The area the observatory covers: what gets in, and what is removed.

Deleting a reading cannot be undone, so the boundary is checked against real places
rather than trusted. The list below is the specification: everywhere on the island of
Ireland stays, everywhere off it goes, and the awkward cases are the ones that matter —
Rathlin and Tory offshore, the Mull of Kintyre and the Isle of Man just across the water,
and (0, 0), which is what a sensor reports when its GPS has nothing to say.
"""
import json
from pathlib import Path

import pandas as pd
from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model

from observatory.forms import StationLocationForm
from observatory.models import Dataset, Station
from observatory.services import region, storage
from observatory.services.deletion import find_outside_region, purge_outside_region
from observatory.services.ingest import ingest_file
from observatory.tests.test_pipeline import TempDataMixin

INSIDE = {
    "Galway": (53.2707, -9.0568), "Dublin": (53.3498, -6.2603), "Cork": (51.8985, -8.4756),
    "Limerick": (52.6638, -8.6267), "Waterford": (52.2593, -7.1101), "Sligo": (54.2766, -8.4761),
    "Athlone": (53.4239, -7.9407), "Castlebar": (53.8550, -9.2988), "Tralee": (52.2713, -9.7026),
    "Letterkenny": (54.9558, -7.7342), "Dundalk": (54.0090, -6.4049),
    # Northern Ireland: PEACE-Air spans the border, so these are inside.
    "Belfast": (54.5973, -5.9301), "Derry/Londonderry": (54.9966, -7.3086), "Enniskillen": (54.3438, -7.6316),
    "Armagh": (54.3503, -6.6528), "Newry": (54.1753, -6.3402), "Coleraine": (55.1326, -6.6685),
    # Extremities and offshore islands.
    "Malin Head": (55.3800, -7.3700), "Mizen Head": (51.4500, -9.8200), "Carnsore Point": (52.1700, -6.3600),
    "Achill Island": (53.9600, -10.0000), "Inishmore": (53.1200, -9.7900), "Valentia": (51.9200, -10.3500),
    "Rathlin Island": (55.2900, -6.2200), "Tory Island": (55.2700, -8.2300),
    # Enclosed water: a sensor on a boat in a bay is not somewhere else.
    "Galway Bay": (53.1500, -9.3000), "Dublin Bay": (53.3400, -6.1200),
}
OUTSIDE = {
    "Null Island": (0.0, 0.0), "Mull of Kintyre": (55.3100, -5.8000), "Islay": (55.7700, -6.2000),
    "Isle of Man": (54.2400, -4.5500), "Holyhead": (53.3090, -4.6330), "St Davids": (51.8820, -5.2690),
    "Liverpool": (53.4084, -2.9916), "Glasgow": (55.8642, -4.2518), "Stranraer": (54.9020, -5.0270),
    "London": (51.5074, -0.1278), "Paris": (48.8566, 2.3522), "New York": (40.7128, -74.0060),
    "Atlantic off Donegal": (55.0000, -12.0000), "Celtic Sea": (50.5000, -8.0000),
    "North of Malin Head": (56.0000, -7.3700),
}


class BoundaryTests(TestCase):
    def test_the_island_of_ireland_is_inside(self):
        for place, (lat, lon) in INSIDE.items():
            with self.subTest(place=place):
                self.assertTrue(region.contains(lat, lon), f"{place} should be inside {region.name()}")

    def test_everywhere_else_is_outside(self):
        for place, (lat, lon) in OUTSIDE.items():
            with self.subTest(place=place):
                self.assertFalse(region.contains(lat, lon), f"{place} should be outside {region.name()}")

    def test_a_reading_with_no_coordinates_is_not_outside(self):
        """AirCasting withholds an indoor session's position. "Unknown" is not "elsewhere"."""
        latitudes = pd.Series([None, 53.27, 48.85])
        longitudes = pd.Series([None, -9.05, 2.35])
        self.assertEqual(region.outside(latitudes, longitudes).tolist(), [False, False, True])
        self.assertEqual(region.inside(latitudes, longitudes).tolist(), [False, True, False])

    @override_settings(OBSERVATORY_RESTRICT_TO_REGION=False)
    def test_the_restriction_can_be_switched_off(self):
        self.assertFalse(region.enabled())
        self.assertFalse(region.outside(pd.Series([48.85]), pd.Series([2.35])).iat[0])

    def test_a_supplied_geojson_replaces_the_built_in_boundary(self):
        import tempfile
        box = {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "boundary.geojson"
            path.write_text(json.dumps(box), encoding="utf-8")
            with override_settings(OBSERVATORY_REGION_GEOJSON=str(path), OBSERVATORY_REGION_NAME="Test box"):
                self.assertEqual(region.name(), "Test box")
                self.assertTrue(region.contains(0.5, 0.5))
                self.assertFalse(region.contains(53.27, -9.05))   # Galway is outside this one


def rows(*points, measurement="PM2.5", station="GW-014"):
    """A simple-layout frame, one reading per (lat, lon)."""
    times = pd.date_range("2026-09-01", periods=len(points), freq="min")
    return pd.DataFrame({"station": station, "time": times.strftime("%Y-%m-%dT%H:%M:%S"),
                         "measurement": measurement, "unit": "µg/m³", "value": 5.0,
                         "latitude": [p[0] for p in points], "longitude": [p[1] for p in points]})


class ImportFilterTests(TempDataMixin, TestCase):
    def ingest(self, frame, name="in.csv"):
        path = Path(self.tmp) / name
        frame.to_csv(path, index=False)
        return ingest_file(path)

    def test_readings_outside_ireland_are_not_stored(self):
        run = self.ingest(rows(INSIDE["Galway"], OUTSIDE["Paris"], INSIDE["Dublin"], OUTSIDE["Null Island"]))
        self.assertEqual(run.rows_read, 4)
        self.assertEqual(run.rows_stored, 2)
        self.assertEqual(run.rows_rejected, 2)
        stored = storage.read_series("GW-014", "PM2.5")
        self.assertEqual(len(stored), 2)
        self.assertTrue(region.inside(stored["latitude"], stored["longitude"]).all())

    def test_the_import_log_says_why_they_went(self):
        run = self.ingest(rows(INSIDE["Galway"], OUTSIDE["Paris"]))
        self.assertEqual(run.status, "partial")
        self.assertIn("outside Ireland", run.message)
        self.assertIn("1 readings", run.message)

    def test_a_file_entirely_outside_stores_nothing(self):
        run = self.ingest(rows(OUTSIDE["Paris"], OUTSIDE["Liverpool"]))
        self.assertEqual(run.rows_stored, 0)
        self.assertFalse(Station.objects.exists())
        self.assertIn("outside Ireland", run.message)

    def test_readings_without_coordinates_are_still_imported(self):
        """Indoor sessions have their position withheld; they are not outside anything."""
        frame = rows(INSIDE["Galway"], INSIDE["Galway"])
        frame.loc[:, ["latitude", "longitude"]] = None
        run = self.ingest(frame)
        self.assertEqual(run.rows_stored, 2)
        self.assertEqual(run.rows_rejected, 0)

    @override_settings(OBSERVATORY_RESTRICT_TO_REGION=False)
    def test_nothing_is_filtered_when_the_restriction_is_off(self):
        run = self.ingest(rows(INSIDE["Galway"], OUTSIDE["Paris"]))
        self.assertEqual(run.rows_stored, 2)


class PurgeOutsideRegionTests(TempDataMixin, TestCase):
    """Readings stored before the boundary existed, or after it changed."""

    def store(self, *points, station="GW-014"):
        """Write readings straight into storage, bypassing the import filter."""
        with override_settings(OBSERVATORY_RESTRICT_TO_REGION=False):
            path = Path(self.tmp) / f"{station}.csv"
            rows(*points, station=station).to_csv(path, index=False)
            ingest_file(path)
        return Station.objects.get(code=station)

    def test_the_survey_reports_without_removing_anything(self):
        station = self.store(INSIDE["Galway"], OUTSIDE["Paris"], OUTSIDE["London"])
        survey = find_outside_region()
        self.assertEqual(survey.readings, 2)
        self.assertEqual(survey.by_station, {station.code: 2})
        self.assertIn("2 readings", survey.describe())
        self.assertEqual(len(storage.read_series(station.code, "PM2.5")), 3)   # still all there

    def test_the_purge_removes_them_and_re_places_the_station(self):
        station = self.store(INSIDE["Galway"], OUTSIDE["Paris"])
        report = purge_outside_region()
        self.assertEqual(report.readings, 1)
        # The station lost a reading; it was not deleted, and the report must not say so.
        self.assertEqual(report.changed, [station.code])
        self.assertEqual(report.stations, [])
        self.assertIn("1 station kept, with some readings removed", report.describe())
        kept = storage.read_series(station.code, "PM2.5")
        self.assertEqual(len(kept), 1)
        station.refresh_from_db()
        self.assertEqual(station.reading_count, 1)
        # The marker was the median of both points, which was in the sea; it has to move.
        self.assertTrue(region.contains(station.latitude, station.longitude))
        self.assertFalse(find_outside_region().anything)

    def test_the_weekly_dataset_is_rebuilt_without_them(self):
        station = self.store(INSIDE["Galway"], OUTSIDE["Paris"])
        self.assertEqual(Dataset.objects.get(station=station).readings, 2)
        purge_outside_region()
        self.assertEqual(Dataset.objects.get(station=station).readings, 1)

    def test_a_station_with_nothing_left_inside_is_removed(self):
        self.store(OUTSIDE["Paris"], OUTSIDE["London"], station="AWAY")
        self.store(INSIDE["Galway"])
        report = purge_outside_region()
        self.assertEqual(report.readings, 2)
        self.assertFalse(Station.objects.filter(code="AWAY").exists())
        self.assertTrue(Station.objects.filter(code="GW-014").exists())
        self.assertIn("had no readings left inside Ireland", " ".join(report.notes))

    def test_a_marker_placed_by_hand_outside_the_region_is_cleared(self):
        station = self.store(INSIDE["Galway"])
        Station.objects.filter(pk=station.pk).update(
            latitude=OUTSIDE["Paris"][0], longitude=OUTSIDE["Paris"][1], location_source="manual")
        survey = find_outside_region()
        self.assertEqual(survey.readings, 0)
        self.assertEqual(survey.misplaced, [station.code])
        purge_outside_region()
        station.refresh_from_db()
        self.assertIsNone(station.latitude)
        self.assertEqual(station.location_source, "none")
        self.assertTrue(station.reading_count)   # its readings were inside and are untouched

    def test_the_removal_is_recorded_in_reports(self):
        from observatory.models import ImportRun
        self.store(INSIDE["Galway"], OUTSIDE["Paris"])
        purge_outside_region()
        run = ImportRun.objects.filter(source="deletion").first()
        self.assertIsNotNone(run)
        self.assertEqual(run.rows_deleted, 1)
        self.assertIn("outside Ireland", run.filename)

    def test_a_clean_store_is_left_alone(self):
        station = self.store(INSIDE["Galway"], INSIDE["Dublin"])
        before = station.reading_count
        report = purge_outside_region()
        self.assertEqual(report.readings, 0)
        self.assertEqual(report.stations, [])
        self.assertEqual(report.changed, [])
        station.refresh_from_db()
        self.assertEqual(station.reading_count, before)


class ManualLocationTests(TempDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.station = Station.objects.create(code="GW-014", name="Galway", region="Ireland")
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="test"))

    def post(self, latitude, longitude):
        return self.client.post(reverse("station_update", args=[self.station.code]),
                                {"name": "Galway", "region": "Ireland",
                                 "latitude": latitude, "longitude": longitude})

    def test_a_location_outside_ireland_is_refused(self):
        response = self.post(*OUTSIDE["Paris"])
        self.assertIn("outside Ireland", " ".join(str(m) for m in response.wsgi_request._messages))
        self.station.refresh_from_db()
        self.assertIsNone(self.station.latitude)

    def test_a_location_inside_ireland_is_saved(self):
        self.post(*INSIDE["Galway"])
        self.station.refresh_from_db()
        self.assertAlmostEqual(self.station.latitude, INSIDE["Galway"][0])
        self.assertEqual(self.station.location_source, "manual")

    def test_the_form_says_which_area_it_means(self):
        form = StationLocationForm({"name": "Galway", "region": "Ireland", "latitude": 48.85, "longitude": 2.35},
                                   instance=self.station)
        self.assertFalse(form.is_valid())
        self.assertIn("outside Ireland", " ".join(form.errors["__all__"]))


class OutsideRegionApiTests(TempDataMixin, TestCase):
    """The Data manager's control: check what is outside, then remove it."""

    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_superuser("admin", password="test")

    def store(self, *points, station="GW-014"):
        with override_settings(OBSERVATORY_RESTRICT_TO_REGION=False):
            path = Path(self.tmp) / f"{station}.csv"
            rows(*points, station=station).to_csv(path, index=False)
            ingest_file(path)
        return Station.objects.get(code=station)

    def test_both_endpoints_are_administrator_only(self):
        """They name stations and delete readings; hiding the button is not enough."""
        for url in (reverse("api_outside_region"), reverse("api_outside_region_purge")):
            with self.subTest(url=url):
                method = self.client.post if url.endswith("purge/") else self.client.get
                self.assertEqual(method(url).status_code, 403)

    def test_the_check_reports_without_removing_anything(self):
        station = self.store(INSIDE["Galway"], OUTSIDE["New York"], OUTSIDE["New York"])
        self.client.force_login(self.admin)
        data = self.client.get(reverse("api_outside_region")).json()
        self.assertEqual(data["region"], "Ireland")
        self.assertTrue(data["enabled"])
        self.assertEqual(data["readings"], 2)
        self.assertEqual(data["stations"], [{"code": station.code, "name": station.name, "readings": 2}])
        # The strays also drag this station's marker out of Ireland, since it sits at the
        # median of its readings. That is not listed separately: it rights itself when the
        # readings go, and counting it again would make the same problem look like two.
        self.assertEqual(data["misplaced"], [])
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(storage.read_series(station.code, "PM2.5")), 3)

    def test_the_marker_comes_back_inside_once_the_strays_are_gone(self):
        station = self.store(INSIDE["Galway"], OUTSIDE["New York"], OUTSIDE["New York"])
        self.client.force_login(self.admin)
        self.client.post(reverse("api_outside_region_purge"))
        station.refresh_from_db()
        self.assertTrue(region.contains(station.latitude, station.longitude))
        self.assertEqual(self.client.get(reverse("api_outside_region")).json()["total"], 0)

    def test_the_purge_removes_them_and_says_what_went(self):
        station = self.store(INSIDE["Galway"], OUTSIDE["New York"])
        self.client.force_login(self.admin)
        result = self.client.post(reverse("api_outside_region_purge")).json()
        self.assertEqual(result["readings"], 1)
        self.assertEqual(result["stations_changed"], [station.code])
        self.assertEqual(result["stations_removed"], [])
        self.assertIn("1 station kept", result["detail"])
        self.assertEqual(self.client.get(reverse("api_outside_region")).json()["total"], 0)

    def test_a_station_left_with_nothing_inside_is_reported_as_removed(self):
        self.store(OUTSIDE["Paris"], station="AWAY")
        self.client.force_login(self.admin)
        result = self.client.post(reverse("api_outside_region_purge")).json()
        self.assertEqual(result["stations_removed"], ["AWAY"])
        self.assertFalse(Station.objects.filter(code="AWAY").exists())

    def test_a_marker_placed_outside_is_counted_even_with_no_stray_readings(self):
        station = self.store(INSIDE["Galway"])
        Station.objects.filter(pk=station.pk).update(
            latitude=OUTSIDE["Paris"][0], longitude=OUTSIDE["Paris"][1], location_source="manual")
        self.client.force_login(self.admin)
        data = self.client.get(reverse("api_outside_region")).json()
        self.assertEqual(data["readings"], 0)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["misplaced"], [{"code": station.code, "name": station.name}])

    def test_purging_a_clean_store_says_so_rather_than_succeeding_silently(self):
        self.store(INSIDE["Galway"])
        self.client.force_login(self.admin)
        response = self.client.post(reverse("api_outside_region_purge"))
        self.assertEqual(response.status_code, 404)
        self.assertIn("inside Ireland", response.json()["error"])

    @override_settings(OBSERVATORY_RESTRICT_TO_REGION=False)
    def test_the_purge_refuses_when_readings_are_not_restricted_at_all(self):
        self.store(INSIDE["Galway"], OUTSIDE["Paris"])
        self.client.force_login(self.admin)
        response = self.client.post(reverse("api_outside_region_purge"))
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.client.get(reverse("api_outside_region")).json()["enabled"])
        self.assertEqual(len(storage.read_series("GW-014", "PM2.5")), 2)

    def test_the_data_manager_offers_the_control_and_names_the_region(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("data")).content.decode()
        self.assertIn('id="purge-outside-region"', page)
        self.assertIn("Readings taken outside Ireland", page)


class MisplacedMarkerTests(TempDataMixin, TestCase):
    """A marker outside the region is reported only when a person put it there."""

    def store(self, *points, station="GW-014"):
        with override_settings(OBSERVATORY_RESTRICT_TO_REGION=False):
            path = Path(self.tmp) / f"{station}.csv"
            rows(*points, station=station).to_csv(path, index=False)
            ingest_file(path)
        return Station.objects.get(code=station)

    def test_a_marker_dragged_out_by_strays_is_not_a_separate_problem(self):
        station = self.store(INSIDE["Galway"], OUTSIDE["New York"], OUTSIDE["New York"])
        self.assertFalse(region.contains(station.latitude, station.longitude))   # median is in the US
        self.assertEqual(find_outside_region().misplaced, [])
        purge_outside_region()
        station.refresh_from_db()
        self.assertTrue(region.contains(station.latitude, station.longitude))

    def test_a_marker_that_cannot_be_re_derived_inside_is_cleared(self):
        """Readings on opposite sides of the island can leave the median in the sea between."""
        station = self.store(INSIDE["Mizen Head"], INSIDE["Malin Head"], OUTSIDE["New York"])
        report = purge_outside_region()
        station.refresh_from_db()
        self.assertEqual(report.readings, 1)
        self.assertEqual(station.reading_count, 2)          # both Irish readings kept
        if station.latitude is not None:
            self.assertTrue(region.contains(station.latitude, station.longitude))
        else:
            self.assertIn("too far apart to place inside Ireland", " ".join(report.notes))
