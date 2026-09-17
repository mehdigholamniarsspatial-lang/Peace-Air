"""JSON and file endpoints used by the front end."""
import io
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import AirCastingDevice, Dataset, ImportRun, ScheduleConfig, Station
from .services import analytics, cleanup, storage, units
from .services.deletion import delete_datasets, device_stations, log_deletion
from .services.ingest import ingest_file, save_upload
from .services.sync import run_sync_in_background


def _dt(value, end=False):
    if not value:
        return None
    ts = datetime.fromisoformat(value)
    return ts + timedelta(days=1) if end and len(value) == 10 else ts


def _bad(message, status=400):
    return JsonResponse({"error": message}, status=status)


def _superuser_only(request):
    if not request.user.is_superuser:
        return _bad("Administrator access is required.", status=403)
    return None


def _run_json(run):
    if not run:
        return None
    return {"id": run.id, "source": run.get_source_display(), "status": run.status, "status_label": run.get_status_display(),
            "filename": run.filename, "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "rows_read": run.rows_read, "rows_stored": run.rows_stored, "rows_duplicate": run.rows_duplicate,
            "rows_rejected": run.rows_rejected, "rows_deleted": run.rows_deleted,
            "stations": run.stations, "message": run.message}


def _station_json(s):
    # Stored metadata keeps the sensor's own unit; the interface shows Celsius.
    return {"code": s.code, "name": s.name, "region": s.region, "latitude": s.latitude, "longitude": s.longitude,
            "location_source": s.get_location_source_display(), "is_indoor": s.is_indoor,
            "reading_count": s.reading_count, "measurements": units.convert_measurements(s.measurements),
            "first_reading": s.first_reading.isoformat() if s.first_reading else None,
            "last_reading": s.last_reading.isoformat() if s.last_reading else None}


@require_GET
def stations_geojson(request):
    """One feature per station: fixed stations with many recordings never produce duplicate markers."""
    measurement = request.GET.get("measurement", settings.OBSERVATORY_DEFAULT_MEASUREMENT)
    try:
        start, end = _dt(request.GET.get("start")), _dt(request.GET.get("end"), end=True)
    except ValueError:
        return _bad("start and end must be ISO dates")
    qs = Station.objects.all()
    if request.GET.get("region"):
        qs = qs.filter(region=request.GET["region"])
    features, unplaced = [], []
    for s in qs:
        props = {**_station_json(s), **{"snapshot": analytics.station_snapshot(s.code, measurement, start, end)},
                 "unit": units.display_unit(
                     next((m["unit"] for m in s.measurements if m["key"] == measurement), ""), measurement)}
        if s.has_location:
            features.append({"type": "Feature", "id": s.code, "properties": props,
                             "geometry": {"type": "Point", "coordinates": [s.longitude, s.latitude]}})
        else:
            unplaced.append(props)
    return JsonResponse({"type": "FeatureCollection", "features": features, "unplaced": unplaced, "measurement": measurement})


@require_GET
def station_detail(request, code):
    s = get_object_or_404(Station, code=code)
    data = _station_json(s)
    data["extents"] = {m["key"]: analytics.extent(s.code, m["key"]) for m in s.measurements}
    return JsonResponse(data)


@require_GET
def station_analysis(request, code):
    get_object_or_404(Station, code=code)
    try:
        q = analytics.Query.from_request(code, request.GET)
    except ValueError as error:
        return _bad(str(error))
    return JsonResponse(analytics.analyse(q))


@require_GET
def station_export(request, code):
    station = get_object_or_404(Station, code=code)
    try:
        q = analytics.Query.from_request(code, request.GET)
    except ValueError as error:
        return HttpResponseBadRequest(str(error))
    rows = analytics.filtered_rows(q).assign(station_code=station.code, station_name=station.name,
                                             measurement=q.measurement)
    rows["time"] = rows["time"].dt.strftime("%Y-%m-%dT%H:%M:%S")
    buf = io.StringIO()
    rows[["station_code", "station_name", "time", "measurement", "value", "unit"]].to_csv(buf, index=False)
    name = f"{station.code}_{storage.measurement_slug(q.measurement)}_{q.obs_range}.csv"
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


@require_GET
def summary(request):
    last = ImportRun.objects.first()
    schedule = ScheduleConfig.load()
    return JsonResponse({
        "stations": Station.objects.count(),
        "stations_without_location": Station.objects.filter(latitude=None).count(),
        "stored_readings": Station.objects.aggregate(total=Sum("reading_count"))["total"] or 0,
        "last_import": _run_json(last),
        "schedule": {"enabled": schedule.enabled, "interval_minutes": schedule.interval_minutes,
                     "next_run": schedule.next_run.isoformat() if schedule.next_run else None},
    })


@require_GET
def datasets(request):
    qs = Dataset.objects.select_related("station")
    if request.GET.get("q"):
        qs = qs.filter(filename__icontains=request.GET["q"])
    if request.GET.get("region"):
        qs = qs.filter(station__region=request.GET["region"])
    if request.GET.get("period"):
        qs = qs.filter(period_start=request.GET["period"])
    periods = sorted(set(Dataset.objects.values_list("period_start", flat=True)), reverse=True)
    page = Paginator(qs, int(request.GET.get("page_size", 8))).get_page(request.GET.get("page", 1))
    return JsonResponse({
        "results": [{"id": d.id, "filename": d.filename, "station": d.station.code, "station_name": d.station.name,
                     "period_start": d.period_start.isoformat(), "period_end": d.period_end.isoformat(),
                     "readings": d.readings, "measurements": d.measurements, "status": "ready",
                     "url": f"/datasets/{d.id}/download/"} for d in page.object_list],
        "page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count,
        "periods": [p.isoformat() for p in periods],
    })


@require_POST
def datasets_delete(request):
    denied = _superuser_only(request)
    if denied:
        return denied
    try:
        payload = json.loads(request.body or "{}")
        ids = {int(value) for value in payload.get("ids", []) if str(value).isdigit()}
    except (TypeError, ValueError, json.JSONDecodeError):
        return _bad("ids must be a list of dataset identifiers")
    if not ids:
        return _bad("Select at least one dataset")

    items = list(Dataset.objects.select_related("station").filter(pk__in=ids))
    if not items:
        return _bad("Those datasets have already been deleted.", status=404)
    report = delete_datasets(items)
    return JsonResponse({"deleted": report.datasets, "readings": report.readings,
                         "stations_removed": report.stations, "detail": report.describe(),
                         "notes": report.notes})


@require_GET
def devices(request):
    """The registered sensors, so the Data manager can list them alongside the datasets.

    Administrators only, like the page it feeds: sensor identifiers and download errors
    would otherwise still be readable as JSON after the page itself was closed.
    """
    denied = _superuser_only(request)
    if denied:
        return denied
    rows = []
    for device in AirCastingDevice.objects.select_related("station").order_by("device_id"):
        # A sensor usually has no station FK: imports match it by source identity instead,
        # so resolve the same way the delete does rather than showing a dash.
        stations = sorted(device_stations(device), key=lambda s: s.code)
        rows.append({
            "id": device.pk,
            "device_id": device.device_id,
            "label": device.label,
            "station": ", ".join(s.code for s in stations) or None,
            "station_name": ", ".join(s.name for s in stations) or None,
            "readings": sum(s.reading_count for s in stations) if stations else None,
            "download_from": device.download_from.isoformat(),
            "download_until": device.download_until.isoformat() if device.download_until else None,
            "active": device.active,
            "status": device.status,
            "last_success": device.last_success.isoformat() if device.last_success else None,
            "last_attempt": device.last_attempt.isoformat() if device.last_attempt else None,
            "last_error": device.last_error,
            "sessions": device.known_session_ids,
        })
    return JsonResponse({"results": rows})


@require_GET
def orphans(request):
    """What is on disk that the Data manager does not list."""
    denied = _superuser_only(request)
    if denied:
        return denied
    return JsonResponse(cleanup.find_orphans().as_json())


@require_POST
def orphans_purge(request):
    denied = _superuser_only(request)
    if denied:
        return denied
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return _bad("Invalid JSON")
    survey = cleanup.find_orphans()
    if not survey.all:
        return _bad("There is nothing to remove: everything stored is listed in the Data manager.", status=404)
    include_stations = bool(body.get("include_stations"))
    report = cleanup.purge(include_stations=include_stations, survey=survey)
    log_deletion(report, "Orphaned data")
    return JsonResponse({"deleted": report.datasets, "readings": report.readings,
                         "stations_removed": report.stations, "detail": report.describe(),
                         "notes": report.notes})


def _dataset_path(d):
    path = storage.sub_dir("datasets") / d.filename
    if not path.exists():
        raise Http404("Dataset file is missing. Re-import the source file to rebuild it.")
    return path


@require_GET
def dataset_download(request, pk):
    d = get_object_or_404(Dataset, pk=pk)
    return FileResponse(_dataset_path(d).open("rb"), as_attachment=True, filename=d.filename)


@require_GET
def datasets_zip(request):
    ids = [int(i) for i in request.GET.get("ids", "").split(",") if i.isdigit()]
    items = list(Dataset.objects.filter(pk__in=ids))
    if not items:
        return HttpResponseBadRequest("Select at least one dataset.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in items:
            zf.write(_dataset_path(d), d.filename)
    resp = HttpResponse(buf.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="datasets_{timezone.now():%Y%m%d_%H%M}.zip"'
    return resp


@require_POST
def import_upload(request):
    denied = _superuser_only(request)
    if denied:
        return denied
    upload = request.FILES.get("file")
    if not upload:
        return _bad("Choose a CSV file to import.")
    if not upload.name.lower().endswith(".csv"):
        return _bad("Only .csv files can be imported.")
    if upload.size > settings.OBSERVATORY_MAX_UPLOAD_MB * 1024 * 1024:
        return _bad(f"File is larger than {settings.OBSERVATORY_MAX_UPLOAD_MB} MB.")
    path = save_upload(upload)
    run = ImportRun.objects.create(source="upload", filename=upload.name)
    run = ingest_file(path, run=run)
    return JsonResponse(_run_json(run), status=200 if run.status != "failed" else 422)


@require_POST
def sync(request):
    denied = _superuser_only(request)
    if denied:
        return denied
    started = run_sync_in_background()
    return JsonResponse({"started": started, "message": "Sync started." if started else "A sync is already running."},
                        status=202 if started else 409)


@require_http_methods(["GET", "POST"])
def schedule(request):
    cfg = ScheduleConfig.load()
    if request.method == "POST":
        denied = _superuser_only(request)
        if denied:
            return denied
        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return _bad("Invalid JSON")
        if "enabled" in body:
            cfg.enabled = bool(body["enabled"])
        if "interval_minutes" in body:
            minutes = int(body["interval_minutes"])
            if not 5 <= minutes <= 1440:
                return _bad("Interval must be between 5 and 1440 minutes.")
            cfg.interval_minutes = minutes
        cfg.save()
    return JsonResponse({"enabled": cfg.enabled, "interval_minutes": cfg.interval_minutes,
                         "next_run": cfg.next_run.isoformat() if cfg.next_run else None})


@require_GET
def imports(request):
    """The import and deletion log — administrators only, like the Reports page."""
    denied = _superuser_only(request)
    if denied:
        return denied
    return JsonResponse({"results": [_run_json(r) for r in ImportRun.objects.all()[:20]]})
