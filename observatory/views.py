"""HTML pages."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .aircasting.client import AirCastingDownloader
from .about_content import load_about_content
from .forms import DeviceForm, StationLocationForm
from .models import AirCastingDevice, ImportRun, ScheduleConfig, Station
from .services.sync import build_config, download_device_in_background
from .services.deletion import delete_device_and_data, delete_station_and_data

superuser_required = user_passes_test(lambda user: user.is_superuser)


def map_explorer(request):
    return render(request, "observatory/map.html", {
        "nav": "map",
        "initial_station": request.GET.get("station", ""),
    })


def analysis_redirect(request):
    """Station analysis is part of the map explorer now; carry the query across."""
    query = request.GET.urlencode()
    return redirect(f"{reverse('map')}?{query}" if query else "map")


def data_manager(request):
    """Imports, datasets, sensors and station locations — the Settings page folded in."""
    return render(request, "observatory/data_manager.html", {
        "nav": "data",
        "regions": Station.objects.exclude(region="").order_by().values_list("region", flat=True).distinct(),
        "default_region": settings.OBSERVATORY_DEFAULT_REGION,
        "schedule": ScheduleConfig.load(),
        "stations": Station.objects.prefetch_related("aliases"),
        "devices": AirCastingDevice.objects.select_related("station"),
        "device_form": DeviceForm(),
        "test_results": request.session.pop("device_test", None),
    })


def about(request):
    return render(request, "observatory/about.html", {
        "nav": "about",
        "about": load_about_content(),
    })


def reports(request):
    return render(request, "observatory/reports.html", {
        "nav": "reports",
        "runs": ImportRun.objects.all()[:100],
    })


@require_POST
@superuser_required
def station_update(request, code):
    station = get_object_or_404(Station, code=code)
    old = (station.latitude, station.longitude)
    form = StationLocationForm(request.POST, instance=station)
    if form.is_valid():
        station = form.save(commit=False)
        if (station.latitude, station.longitude) != old:
            station.location_source = "manual" if station.latitude is not None else "none"
        station.save()
        messages.success(request, f"Saved {station.name}.")
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect("data")


@require_POST
@superuser_required
def device_add(request):
    form = DeviceForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
        return redirect("data")
    device = form.save()
    # Downloading the whole configured period can take many minutes, so it runs in the
    # background and the Data manager shows how it is getting on.
    period = f"from {device.download_from}" + (f" to {device.download_until}" if device.download_until else " to now")
    if download_device_in_background(device):
        messages.success(request, f"Added {device.device_id} and started downloading its recordings {period}. "
                                  "Progress is shown in the Data manager.")
    else:
        messages.success(request, f"Added {device.device_id}. A download is already running; this sensor is "
                                  f"included in the next one, covering {period}.")
    return redirect("data")


@require_POST
@superuser_required
def device_delete(request, pk):
    device = get_object_or_404(AirCastingDevice, pk=pk)
    report = delete_device_and_data(device)
    messages.success(request, " ".join([report.describe(), *report.notes]))
    return redirect("data")


@require_POST
@superuser_required
def station_delete(request, code):
    station = get_object_or_404(Station, code=code)
    report = delete_station_and_data(station)
    messages.success(request, " ".join([report.describe(), *report.notes]))
    return redirect("data")


@require_POST
@superuser_required
def device_test(request, pk):
    device = get_object_or_404(AirCastingDevice, pk=pk)
    try:
        results = AirCastingDownloader(build_config(device)).test_access()
    except Exception as error:  # noqa: BLE001
        results = [{"query": device.device_id, "ok": False, "result": "Error", "detail": str(error)}]
    request.session["device_test"] = {"device": device.device_id, "results": results}
    return redirect("data")
