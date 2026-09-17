# Air Quality Observatory

A Django platform that downloads AirCasting sensor recordings (using the logic of
`AirCasting_Download_AIRBEAM3_B0B21C7627C4.ipynb`), stores them as structured CSV files,
and lets you explore them on a map with time-series and statistical views.

The app opens on **About**, which the PEACE-Air logo also returns to. Three sections do
the work: **Map explorer** (map, time series, aggregation, observation range and
descriptive statistics in one place), **Data manager** (imports, sensors, stations,
downloadable datasets and housekeeping) and **Reports** (every import and deletion).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo                              # optional: 12 synthetic Irish stations
python manage.py import_csv sample_data/airbeam3_sample.csv
python manage.py runserver
```

Open http://127.0.0.1:8000, which lands on the About page. Leaflet and Chart.js are bundled in `observatory/static`, so only
the basemap tiles and the web font need internet access.

Run the test suite with `python manage.py test observatory`.

## Getting real data in

There are three ways, all ending in the same import pipeline:

1. **AirCasting sync.** Add a device in *Settings* (e.g. `AIRBEAM3:B0B21C7627C4`, known
   session `21374`), use *Test access* to check the server answers, then press *Run sync* on
   the Data manager page, or run `python manage.py fetch_aircasting`.
   For scheduled imports switch on *Scheduled imports* and keep
   `python manage.py run_scheduler` running (or call `fetch_aircasting` from cron / Task Scheduler).
2. **Upload** a CSV on the Data manager page (drag and drop or *Browse files*).
3. **Command line:** `python manage.py import_csv file1.csv file2.csv [--station GW-014]`.

Accepted CSV layouts:

| Layout | Required columns | Optional |
|---|---|---|
| AirCasting export (notebook / sync output) | `sensor_name`, `value`, one of `source_time` / `time_utc` / `raw_time` | `device_group`, `session_id`, `stream_id`, `latitude`, `longitude`, `coordinate_source`, … |
| Simple long format (other networks) | `station`, `time`, `measurement`, `value` | `station_name`, `unit`, `latitude`, `longitude`, `region` |

## How it works

```
AirCasting API ──► aircasting/client.py ──► data/raw/aircasting/<device>/export_*/all_recordings.csv
Upload / CLI  ──────────────────────────► data/raw/uploads/<timestamp>_<file>.csv
                                                    │
                                   services/ingest.py (normalise, validate, resolve station)
                                                    │
                      data/stations/<CODE>/<MEASUREMENT>.csv   ◄── single source of truth
                      data/datasets/<Station>_<CODE>_<week-end>.csv   (weekly downloads)
                                                    │
                  services/analytics.py (pandas/NumPy/SciPy) ──► JSON API ──► Leaflet + Chart.js
```

* **Notebook integration.** `observatory/aircasting/client.py` keeps the notebook's request,
  caching, windowing, region-filter, timestamp and export code. The global configuration
  cell became a `DownloadConfig` object, so each registered device gets its own run. Syncs are
  incremental: they restart two days before the last successful sync so late uploads are caught.
  Raw responses, `manifest.json` and the ZIP are kept exactly as the notebook produces them.
* **CSV storage.** The database only holds metadata (stations, device aliases, import log,
  dataset catalogue, schedule). Readings live in one tidy CSV per station and measurement,
  sorted by time and de-duplicated on `(time, session_id, stream_id)`, so re-importing a file
  never double counts. Original uploads are kept untouched under `data/raw/`.
* **One point per station.** Every source identity (an AirCasting `device_group` or a simple
  `station` value) is mapped to a station through `DeviceAlias`. An unseen source whose
  coordinates match an existing station (to 5 decimal places, about 1 m) joins that station, and
  the map API returns exactly one GeoJSON feature per station regardless of how many recordings
  it has. Mobile sessions are placed at the median of their reading coordinates.
* **Indoor sessions.** AirCasting withholds coordinates for indoor sessions (the supplied sample
  is one). Such stations are imported and analysable, are listed on the map as "without
  coordinates", and can be placed from *Settings*. Manual locations are never overwritten.
* **Time.** Timestamps are kept as the sensor's source clock, matching the notebook's default
  `TIME_CONVENTION="unverified"`. Set `AIRCASTING_TIME_CONVENTION` to `utc` or `local_as_utc`
  once you have confirmed your data's convention.

## The two statistical controls

The brief asked for a confidence-interval filter; the design separates two ideas, and so does the app:

* **Mean confidence interval (90 / 95 / 99 %)** — the shaded band around each aggregated
  mean, `mean ± t(n−1) · s/√n` for the readings in that 10-minute / hourly / 6-hour / daily bucket.
  The Mean card also shows the interval for the whole period.
* **Observation range (All / Central 90 % / Central 95 %)** — filters the readings themselves,
  keeping values between the matching lower and upper percentiles of the selected window
  (e.g. P2.5–P97.5). The series, histogram, statistics and *Export CSV* all use the kept readings;
  the stored files are not modified. The page states the bounds used and how many readings were excluded.

Histogram bins use the Freedman–Diaconis rule rounded to a readable width (8–60 bins).

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/stations/?measurement=PM2.5&start=YYYY-MM-DD&end=YYYY-MM-DD&region=` | GeoJSON, one feature per station, plus `unplaced` stations |
| `GET /api/stations/<code>/` | Station metadata, measurements and data extents |
| `GET /api/stations/<code>/analysis/?measurement=&start=&end=&aggregation=raw\|10min\|hourly\|6h\|daily&ci=90\|95\|99&range=all\|central90\|central95` | Series with CI, histogram, statistics |
| `GET /api/stations/<code>/export.csv?…` | Filtered readings as CSV (same parameters) |
| `GET /api/summary/`, `GET /api/datasets/`, `GET /api/imports/` | Data manager |
| `POST /api/import/` (multipart `file`), `POST /api/sync/`, `GET/POST /api/schedule/` | Imports and scheduling |
| `GET /datasets/<id>/download/`, `GET /datasets/download/?ids=1,2` | Dataset files / ZIP |

`end` dates are inclusive.

## Before deploying

The dashboard and API require login. Manual imports, deletion, device management and schedule
changes require a Django superuser. For a shared server, set `DJANGO_SECRET_KEY`,
`DJANGO_DEBUG=0` and `DJANGO_ALLOWED_HOSTS`; run syncs with the scheduler command or a task queue
rather than the local development server's embedded worker; and keep `data/` private, since
exports can contain precise locations.

## Project layout

```
config/                     Django settings and root URLs
observatory/
  aircasting/client.py      Notebook downloader as a module
  services/ingest.py        CSV normalisation, station resolution, dataset building
  services/storage.py       CSV storage layout and caching
  services/analytics.py     Aggregation, confidence intervals, percentile filter, histogram, statistics
  services/sync.py          Runs the downloader per device and imports the export
  services/demo.py          Synthetic demo network (written in AirCasting layout)
  management/commands/      seed_demo, import_csv, fetch_aircasting, run_scheduler
  api.py, views.py, urls.py JSON endpoints and pages
  templates/, static/       Leaflet + Chart.js front end
  tests/                    Ingest, spatial de-duplication and statistics tests
sample_data/                The supplied notebook and AirBeam3 sample
```

## Screenshots

`docs/screenshots/` shows the three main pages running on the demo network (captured offline,
so the basemap tiles are not drawn).
