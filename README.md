# Air Quality Observatory

A Django platform for PEACE-Air Work Package 2. It downloads AirCasting sensor recordings stores them as
structured CSV files, lets you explore them on a map with time-series and statistical
views, and collects citizen feedback through a public survey.

The app opens on **About**, which the PEACE-Air logo also returns to.

| Section | Who can see it | What it holds |
|---|---|---|
| **About** | everyone | The project, WP2, partners and funding. The landing page. |
| **Map explorer** | everyone | Map, time series with confidence band, distribution, aggregation and descriptive statistics for the selected station. |
| **Feedback survey** | everyone | The citizen questionnaire at `/feedback/`. |
| **Data manager** | administrators | Imports, sensors, stations, downloadable datasets, housekeeping. |
| **Reports** | administrators | Every import, sync and deletion. |
| **Admin panel** | administrators | Django admin: survey responses, raw records. |

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser                        # the administrator account
python manage.py seed_demo                              # optional: 12 synthetic Irish stations
python manage.py import_csv sample_data/airbeam3_sample.csv
python manage.py runserver
```

With Anaconda, replace the first line with `conda create -n peace-air python=3.12` and
`conda activate peace-air`.

Open http://127.0.0.1:8000, which lands on the About page. Leaflet and Chart.js are bundled
in `observatory/static`, so only the basemap tiles and the web font need internet access —
and the survey page loads neither, so it makes no third-party requests at all.

Run the test suite with `python manage.py test observatory`.

## Who can do what

Viewing is open; changing anything is not. Signing in as an administrator lands on the Data
manager, and anyone else lands on the map.

* **Public** — About, Map explorer, the feedback survey, dataset downloads, and the
  read-only station APIs.
* **Administrators** (Django superusers) — the Data manager and Reports pages, the JSON
  behind them (`/api/devices/`, `/api/imports/`, `/api/orphans/`), every write endpoint, and
  the Django admin. These pages are closed at the view, not merely hidden from the menu.

## Getting real data in

There are three ways, all ending in the same import pipeline:

1. **AirCasting sync.** Add a sensor in the *Data manager* (e.g. `AIRBEAM3:B0B21C7627C4`,
   known session `21374`) with a **Download from** date and an optional **Download until**;
   adding it starts the first download straight away, and the *Sensors* table shows how it is
   getting on. *Test access* checks an ID against the API without downloading. Later syncs are
   incremental. For scheduled imports switch on *Scheduled imports* and keep
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
  cell became a `DownloadConfig` object, so each registered sensor gets its own run. Syncs are
  incremental: both the search and the download restart two days before the last success, so
  late uploads are caught without re-scanning the whole history every tick.
  Raw responses, `manifest.json` and the ZIP are kept exactly as the notebook produces them.
* **CSV storage.** The database only holds metadata (stations, device aliases, import log,
  dataset catalogue, schedule, survey responses). Readings live in one tidy CSV per station and
  measurement, sorted by time and de-duplicated on `(time, session_id, stream_id)`, so
  re-importing a file never double counts. Original uploads are kept untouched under `data/raw/`.
* **One point per station.** Every source identity (an AirCasting `device_group` or a simple
  `station` value) is mapped to a station through `DeviceAlias`. An unseen source whose
  coordinates match an existing station (to 5 decimal places, about 1 m) joins that station, and
  the map API returns exactly one GeoJSON feature per station regardless of how many recordings
  it has. Mobile sessions are placed at the median of their reading coordinates.
* **Indoor sessions.** AirCasting withholds coordinates for indoor sessions (the supplied sample
  is one). Such stations are imported and analysable, are listed on the map as "without
  coordinates", and can be placed from the *Data manager*. Manual locations are never overwritten.
* **Time.** Timestamps are kept as the sensor's source clock, matching the notebook's default
  `TIME_CONVENTION="unverified"`. Set `AIRCASTING_TIME_CONVENTION` to `utc` or `local_as_utc`
  once you have confirmed your data's convention.
* **Temperature in Celsius.** AirBeam sensors report Fahrenheit, and that is what the stored CSV
  keeps. `services/units.py` converts on the way out — charts, statistics, exports and the weekly
  datasets all read °C. Converting the values before any statistic is computed means the standard
  deviation takes the 5/9 scale and correctly drops the 32° offset. Run
  `python manage.py rebuild_datasets` to restate dataset files written before this change.

## Statistics on the map explorer

* **Aggregation** (Automatic / individual readings / 10-minute / hourly / 6-hour / daily) sets
  how the series is drawn. Automatic picks 10-minute means for a short window and hourly beyond.
* **Mean confidence interval (90 / 95 / 99 %)** is the shaded band around each aggregated mean,
  `mean ± t(n−1) · s/√n` for the readings in that bucket.
* The **six statistics cards** describe every reading in the selected period, not the aggregated
  points — so Maximum is the real peak rather than the largest hourly mean. The page says so
  above the cards, because aggregation visibly reshapes the plot while these figures stay put.
* The **y-axis** starts at zero for concentrations. For temperature and humidity it fits the
  confidence band instead, so a 17–33 °C day is not drawn as a flat line in the top third.
* Histogram bins use the Freedman–Diaconis rule rounded to a readable width (8–60 bins).

The percentile filter (`range=all|central90|central95`) is still available on the analysis API;
it is no longer a control on the page.

## Deleting data

Deleting has to remove three things that live in different places: the database rows, the
generated files under `data/`, and the cached downloads a sensor would otherwise be rebuilt
from. `services/deletion.py` does all three and reports exactly what went; the removal is
recorded in Reports.

* Deleting a **dataset** removes the matching readings, and drops the station too if nothing is
  left of it.
* Deleting a **sensor** removes every station attributable to it — matched by `device_group`
  spelling *and* by known session id — along with its cached AirCasting downloads.
* Deleting from the **Django admin** routes through the same services, so it cannot leave
  orphaned files behind.
* `services/cleanup.py` sweeps what a deletion orphans. *Stored data not listed above* in the
  Data manager, or `python manage.py purge_orphans [--apply --include-stations]`, reconciles the
  whole store: anything on disk the catalogue does not list is reported and removed.

## Citizen feedback survey

A public, anonymous questionnaire at `/feedback/`: no name, address, postcode, date of birth or
special-category data, location no finer than a county, an unticked consent gate and an age
confirmation. It posts as a plain HTML form and works without JavaScript; the script adds the
live counter, conditional fields, inline errors and a `sessionStorage` draft.

Answers and the optional "send me the results" email are stored in **separate tables with
nothing joining them** (`SurveyResponse`, `SurveyContact`), so an address cannot be traced back
to the answers someone gave — the contact row keeps a date rather than a timestamp for the same
reason. Review submissions in the Django admin under *Survey responses*, where they are
read-only and filterable, with an action that exports the selection to CSV. Delete the rows in
*Survey contacts* once the summary has been circulated, as the privacy notice promises.

Controller, contact address and retention period live in `observatory/survey.py`.

## API

| Endpoint | Purpose | Access |
|---|---|---|
| `GET /api/stations/?measurement=PM2.5&start=YYYY-MM-DD&end=YYYY-MM-DD&region=` | GeoJSON, one feature per station, plus `unplaced` stations | public |
| `GET /api/stations/<code>/` | Station metadata, measurements and data extents | public |
| `GET /api/stations/<code>/analysis/?measurement=&start=&end=&aggregation=raw\|10min\|hourly\|6h\|daily&ci=90\|95\|99&range=all\|central90\|central95` | Series with CI, histogram, statistics | public |
| `GET /api/stations/<code>/export.csv?…` | Filtered readings as CSV (same parameters) | public |
| `GET /api/summary/`, `GET /api/datasets/` | Counts and the dataset catalogue | public |
| `GET /datasets/<id>/download/`, `GET /datasets/download/?ids=1,2` | Dataset files / ZIP | public |
| `GET /api/devices/`, `GET /api/imports/` | Registered sensors, import log | administrator |
| `GET /api/orphans/`, `POST /api/orphans/purge/` | Storage the catalogue does not list | administrator |
| `POST /api/import/` (multipart `file`), `POST /api/sync/`, `GET/POST /api/schedule/`, `POST /api/datasets/delete/` | Imports, scheduling, deletion | administrator |

`end` dates are inclusive.

## Before deploying

Set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0` and `DJANGO_ALLOWED_HOSTS`, and serve through a real
WSGI server rather than `runserver`. Run syncs with `python manage.py run_scheduler` or a task
queue rather than the development server's embedded worker. Keep `data/` private, since exports
can contain precise locations. This is a live application, not a static report: the schedule,
the survey and the dataset rebuilds only run while it is running.

## Project layout

```
config/                     Django settings and root URLs
observatory/
  aircasting/client.py      Notebook downloader as a module
  services/ingest.py        CSV normalisation, station resolution, dataset building
  services/storage.py       CSV storage layout and caching
  services/analytics.py     Aggregation, confidence intervals, percentile filter, histogram, statistics
  services/units.py         Fahrenheit to Celsius, applied at every display boundary
  services/deletion.py      Complete, filesystem-aware deletion
  services/cleanup.py       Reconciles the store with the catalogue
  services/sync.py          Runs the downloader per sensor and imports the export
  services/demo.py          Synthetic demo network (written in AirCasting layout)
  survey.py                 Feedback survey question set, controller and retention
  management/commands/      seed_demo, import_csv, fetch_aircasting, run_scheduler,
                            purge_orphans, rebuild_datasets
  api.py, views.py, urls.py JSON endpoints and pages
  templates/, static/       Leaflet + Chart.js front end
  tests/                    Ingest, de-duplication, statistics, units, deletion, survey, access
sample_data/                The supplied notebook and AirBeam3 sample
```

## Screenshots

`docs/screenshots/` shows the main pages running on the demo network (captured offline, so the
basemap tiles are not drawn). They predate the merge of Station analysis into the Map explorer.
