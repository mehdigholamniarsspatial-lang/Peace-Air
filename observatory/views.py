"""HTML pages."""
import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .aircasting.client import AirCastingDownloader
from .about_content import load_about_content
from .forms import DeviceForm, StationLocationForm, SurveyForm
from . import survey
from .models import AirCastingDevice, ImportRun, ScheduleConfig, Station, SurveyContact, SurveyResponse
from .services import region
from .services.sync import build_config, download_device_in_background
from .services.deletion import delete_device_and_data, delete_station_and_data

superuser_required = user_passes_test(lambda user: user.is_superuser)


def map_explorer(request):
    return render(request, "observatory/map.html", {
        "nav": "map",
        "initial_station": request.GET.get("station", ""),
    })


def after_login(request):
    """Send an administrator to the data manager, anyone else to the map.

    The data manager is where an administrator's work is — imports, sensors, stations —
    so it is the useful landing page for them. It is not in the menu for anyone else,
    so dropping a non-administrator there would strand them on a page they cannot
    navigate back to.
    """
    return redirect("data" if request.user.is_superuser else "map")


def analysis_redirect(request):
    """Station analysis is part of the map explorer now; carry the query across."""
    query = request.GET.urlencode()
    return redirect(f"{reverse('map')}?{query}" if query else "map")


@superuser_required
def data_manager(request):
    """Imports, datasets, sensors and station locations — the Settings page folded in.

    Administrators only: it lists sensor identifiers, import history and the controls
    that delete stored readings. Hiding the menu entry alone would have left the page
    one guessed URL away.
    """
    return render(request, "observatory/data_manager.html", {
        "nav": "data",
        "region_name": region.name(),
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


ANSWER_FIELDS = [
    "county", "area_type", "near_border", "air_quality_rating", "pollution_sources",
    "dashboard_usefulness", "dashboard_improvements", "dashboard_improvements_other",
    "sensor_trust", "trust_concerns", "participation", "open_data_support", "additional_comments",
]


def _flatten_payload(body: dict) -> dict:
    """Turn the JSON the page posts back into the flat shape the form expects."""
    consent = body.get("consent") or {}
    answers = body.get("answers") or {}
    contact = body.get("contact") or {}
    data = {"consent": consent.get("given"), "age_confirm": consent.get("age_confirmed"),
            "wants_results": contact.get("wants_results"), "contact_email": contact.get("email") or ""}
    for field in ANSWER_FIELDS:
        value = answers.get(field)
        data[field] = [] if value is None and field.endswith("s") else value
    return data


def _save_survey(form: SurveyForm) -> None:
    """Store the answers and, separately, the address.

    The two are written as independent rows with no key between them: the answers must
    never lead back to the person who asked for the results. ``SurveyContact`` keeps a
    date rather than a timestamp for the same reason.
    """
    data = form.cleaned_data
    SurveyResponse.objects.create(
        consent_given=data["consent"], age_confirmed=data["age_confirm"],
        **{field: data[field] for field in ANSWER_FIELDS})
    if data.get("wants_results") and data.get("contact_email"):
        SurveyContact.objects.create(email=data["contact_email"])


def _survey_context(**extra):
    return {
        "nav": "feedback",
        "controller": survey.CONTROLLER_NAME,
        "contact_email": survey.CONTACT_EMAIL,
        "retention_period": survey.RETENTION_PERIOD,
        "county_groups": survey.COUNTY_GROUPS,
        "trust_with_doubts": sorted(survey.SENSOR_TRUST_WITH_DOUBTS),
        "free_text_warning": survey.FREE_TEXT_WARNING,
        # Bound fields are reached by key, not attribute; used to keep the "something
        # else" box visible when a submission comes back rejected.
        "improvements_chosen": [str(v) for v in (extra["form"]["dashboard_improvements"].value() or [])]
                               if extra.get("form") else [],
        **extra,
    }


def feedback(request):
    """Public citizen feedback survey. Works as a plain form post without JavaScript."""
    wants_json = "application/json" in request.META.get("CONTENT_TYPE", "")
    if request.method != "POST":
        return render(request, "observatory/feedback.html", _survey_context(form=SurveyForm()))

    if wants_json:
        try:
            posted = _flatten_payload(json.loads(request.body or "{}"))
        except (json.JSONDecodeError, AttributeError):
            return JsonResponse({"error": "Could not read the submitted answers."}, status=400)
    else:
        posted = request.POST

    form = SurveyForm(posted)
    if form.is_valid():
        _save_survey(form)
        if wants_json:
            return JsonResponse({"ok": True})
        return render(request, "observatory/feedback.html", _survey_context(submitted=True))
    if wants_json:
        return JsonResponse({"ok": False, "errors": form.errors}, status=400)
    return render(request, "observatory/feedback.html", _survey_context(form=form))


@superuser_required
def reports(request):
    """Administrators only: the import and deletion log for the whole platform."""
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
