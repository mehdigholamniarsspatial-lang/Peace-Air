# Air Quality Observatory

A Django platform for PEACE-Air Work Package 2. It downloads AirCasting sensor recordings stores them as
structured CSV files, lets you explore them on a map with time-series and statistical
views, and collects citizen feedback through a public survey.

The app opens on **About**, which the PEACE-Air logo also returns to.

| Section | Who can see it | What it holds |
|---|---|---|
| **About** | everyone | The project, WP2, partners and funding. The landing page. |
| **Map explorer** | everyone | Map, the sensor's track where it moved, time series with confidence band, distribution, aggregation and descriptive statistics for the selected station. |
| **Feedback survey** | everyone | The citizen questionnaire at `/feedback/`. |
| **Data manager** | administrators | Imports, sensors, stations, downloadable datasets, housekeeping, removing readings taken outside the region. |
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
   getting on. A **session number** may name either kind of recording — a fixed station or a
   mobile track, such as the `sessionId` in an aircasting.org map link — and *Test access*
   says which kind it found. Mobile sessions arrive with a coordinate per reading, so syncing
   one is all it takes for its track to appear on the map. Later syncs are incremental.
   A sensor can only be registered **once**: adding one that is already there is refused with
   a message naming it, whatever spelling is used — `AIRBEAM3:B0B21C7627C4` and
   `AirBeam3-b0b21c7627c4` are the same hardware, and registering both would download it
   twice into one station. Remove the existing sensor first to add it again, remembering that
   removing it deletes its stations, readings, datasets and cached downloads too. For scheduled imports switch on *Scheduled imports* and keep
   `python manage.py run_scheduler` running (or call `fetch_aircasting` from cron / Task Scheduler).
2. **Upload** a CSV on the Data manager page (drag and drop or *Browse files*).
3. **Command line:** `python manage.py import_csv file1.csv file2.csv [--station GW-014]`.

Accepted CSV layouts:

| Layout | Required columns | Optional |
|---|---|---|
| AirCasting export (notebook / sync output) | `sensor_name`, `value`, one of `source_time` / `time_utc` / `raw_time` | `device_group`, `session_id`, `stream_id`, `latitude`, `longitude`, `coordinate_source`, … |
| AirCasting **session export** (the per-recording CSV the aircasting.org map offers) | — recognised by its own header block | — |
| Simple long format (other networks) | `station`, `time`, `measurement`, `value` | `station_name`, `unit`, `latitude`, `longitude`, `region` |

A session export is the file you get from a session's page on
[aircasting.org](https://aircasting.org) — one row per GPS fix, one column per measured
channel, under a block of paired metadata rows naming the sensor package, the channels,
their measurement types and their units (spelled out, e.g. *micrograms per cubic meter*).
Drop it on the Data manager like any other CSV: it is folded into the layout above, its
units are restated as symbols, and the session number is read from the download's file
name (`<session name>_<session id>__<export stamp>.csv`). Because every row keeps the
coordinate it was taken at, importing one gives that sensor a **track**.

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
  cell became a `DownloadConfig` object, so each registered sensor gets its own run. One
  deliberate departure: the notebook resolved a session given by number against the *fixed*
  endpoint only, which cannot answer for a mobile recording. `lookup_session` asks both and
  reports what each said, so a session number copied from a map link works whichever kind it
  names. Syncs are incremental: both the search and the download restart two days before the last success, so
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
  Coordinates cannot reconcile the several ways AirCasting spells one sensor — the downloader
  writes `query:AirBeam3-b0b21c7627c4;…` where a session export writes `AirBeam3:b0b21c7627c4`,
  and an indoor session has no coordinates at all — so the hardware address in the key does:
  a new identity carrying a MAC already standing for a station joins it instead of creating a
  second copy of one sensor.
* **Only Ireland.** Every reading is checked against a boundary on the way in, and one taken
  outside it is not stored. A sensor's GPS is not always right — a fix can land at (0, 0), or
  at the app's own default coordinates on the other side of the Atlantic — and a point like
  that is not a measurement of Irish air, but it does stretch the map across half the world.
  The built-in boundary is the **island of Ireland**, Republic and Northern Ireland together,
  drawn about ten kilometres offshore all the way round: deleting a reading cannot be undone,
  so a coast road, an offshore island or a boat in a bay stays inside while anywhere off the
  island does not. Readings with *no* coordinates are never touched — an indoor session has
  its position withheld, and "unknown" is not "elsewhere". See `services/region.py`.
* **Mobile sessions keep a position per reading**, so they are a path rather than a point. Each
  station measurement records how many distinct places its readings were taken at, which is what
  separates the two: a fixed station repeats one coordinate however many readings it holds.
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

## The sensor's track

A recording made while the sensor was moving is drawn on the map as the path it travelled,
coloured by what it was measuring along the way. It appears whenever the selected station
has more than one position in the chosen window, and the *Sensor track* switch in the
toolbar turns it off.

* **Colours** follow the scale aircasting.org shows for the same sensor — 0 / 12 / 35 / 55 /
  150 µg/m³ for PM2.5 and PM1, 0 / 20 / 50 / 100 / 200 for PM10, and the equivalents for
  humidity and temperature (in Celsius, as everything here is). A measurement with no
  published scale is banded by its own quartiles. The legend on the map is always the scale
  actually in use.
* **The bar under the map** walks the sensor along its path: drag to scrub, or press play.
  The readout gives the time to the second and the reading at that fix. Where a window holds
  several recordings, the list on the right picks one or shows them all.
* **Hovering the time series** moves the same cursor to where the sensor was at that moment,
  and clicking anywhere on the path answers with the reading taken there.
* The line is broken wherever more than five minutes passed between two fixes, rather than
  drawn straight across whatever the sensor was carried past while it was not reporting.
* Long recordings are thinned to at most 4,000 fixes with one stride across the window, so
  sessions keep their relative density and the first and last fix of each are always kept.

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
* **Readings taken outside Ireland** in the Data manager checks what is stored outside the
  region, lists it per station and removes it once confirmed — the same work as the command
  below, for people who do not have a shell. `python manage.py purge_outside_region` lists
  readings stored outside the region — from before this check existed, or after the boundary
  changed — and removes them with `--apply`.
  A station is not deleted for being partly outside: only those readings go, its marker is
  re-derived from the ones that remain, and the station itself is removed only if nothing is
  left of it. Unlike the other deletions this one does not sweep orphaned files, because
  filtering readings should not reach past what was asked for.
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
| `GET /api/stations/<code>/track/?measurement=&start=&end=` | The sensor's path per recording (parallel `lat`/`lon`/`t`/`v` arrays), colour thresholds and bounds | public |
| `GET /api/stations/<code>/export.csv?…` | Filtered readings as CSV (same parameters) | public |
| `GET /api/summary/`, `GET /api/datasets/` | Counts and the dataset catalogue | public |
| `GET /datasets/<id>/download/`, `GET /datasets/download/?ids=1,2` | Dataset files / ZIP | public |
| `GET /api/devices/`, `GET /api/imports/` | Registered sensors, import log | administrator |
| `GET /api/orphans/`, `POST /api/orphans/purge/` | Storage the catalogue does not list | administrator |
| `GET /api/outside-region/`, `POST /api/outside-region/purge/` | Readings recorded outside the region, and their removal | administrator |
| `POST /api/import/` (multipart `file`), `POST /api/sync/`, `GET/POST /api/schedule/`, `POST /api/datasets/delete/` | Imports, scheduling, deletion | administrator |

`end` dates are inclusive.

## Before deploying

Covering somewhere other than Ireland means pointing `OBSERVATORY_REGION_GEOJSON` at a WGS84
Polygon or MultiPolygon file and setting `OBSERVATORY_REGION_NAME` to match; run
`purge_outside_region` afterwards to restate what is already stored. `OBSERVATORY_RESTRICT_TO_REGION=0`
turns the check off altogether and stores every reading wherever it was taken.

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
  services/analytics.py     Aggregation, confidence intervals, percentile filter, histogram, statistics, track
  services/units.py         Fahrenheit to Celsius, applied at every display boundary
  services/region.py        The area covered (the island of Ireland) and what falls outside it
  services/deletion.py      Complete, filesystem-aware deletion
  services/cleanup.py       Reconciles the store with the catalogue
  services/sync.py          Runs the downloader per sensor and imports the export
  services/demo.py          Synthetic demo network (written in AirCasting layout)
  survey.py                 Feedback survey question set, controller and retention
  management/commands/      seed_demo, import_csv, fetch_aircasting, run_scheduler,
                            purge_orphans, purge_outside_region, rebuild_datasets
  api.py, views.py, urls.py JSON endpoints and pages
  templates/, static/       Leaflet + Chart.js front end
  tests/                    Ingest, de-duplication, statistics, units, tracks, region, deletion,
                            survey, access
sample_data/                The supplied notebook and AirBeam3 sample
```

## Screenshots

`docs/screenshots/` shows the main pages running on the demo network (captured offline, so the
basemap tiles are not drawn). They predate the merge of Station analysis into the Map explorer.
