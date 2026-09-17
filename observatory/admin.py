import csv

from django.contrib import admin
from django.http import HttpResponse

from .models import (AirCastingDevice, Dataset, DeviceAlias, ImportRun, ScheduleConfig,
                     Station, SurveyContact, SurveyResponse)
from .services.deletion import delete_dataset_and_readings, delete_device_and_data, delete_station_and_data


class ServiceDeleteMixin:
    """Route admin deletes through the deletion services.

    ``Model.delete()`` only removes database rows, which would leave the generated CSV
    files behind — and a later import that reuses the same station code would silently
    pick those orphaned readings back up. The admin has to delete the same way the
    dashboard does.
    """
    delete_service = None

    def delete_model(self, request, obj):
        type(self).delete_service(obj)

    def delete_queryset(self, request, queryset):
        for obj in list(queryset):
            type(self).delete_service(obj)


class AliasInline(admin.TabularInline):
    model = DeviceAlias
    extra = 0


@admin.register(Station)
class StationAdmin(ServiceDeleteMixin, admin.ModelAdmin):
    delete_service = staticmethod(delete_station_and_data)
    list_display = ("code", "name", "region", "latitude", "longitude", "location_source", "reading_count", "last_reading")
    search_fields = ("code", "name", "aliases__key")
    list_filter = ("region", "location_source", "is_indoor")
    inlines = [AliasInline]


@admin.register(Dataset)
class DatasetAdmin(ServiceDeleteMixin, admin.ModelAdmin):
    delete_service = staticmethod(delete_dataset_and_readings)
    list_display = ("filename", "station", "period_start", "period_end", "readings", "updated_at")
    search_fields = ("filename", "station__code", "station__name")
    list_filter = ("station__region", "period_start")


@admin.register(AirCastingDevice)
class AirCastingDeviceAdmin(ServiceDeleteMixin, admin.ModelAdmin):
    delete_service = staticmethod(delete_device_and_data)
    list_display = ("device_id", "label", "station", "active", "last_success")
    list_filter = ("active",)
    search_fields = ("device_id", "label")


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "source", "status", "filename", "rows_read", "rows_stored", "rows_duplicate",
                    "rows_rejected", "rows_deleted")
    list_filter = ("source", "status")


admin.site.register(ScheduleConfig)


# --------------------------------------------------------------------------- survey
ANSWER_COLUMNS = [
    "submitted_at", "county", "area_type", "near_border", "air_quality_rating",
    "pollution_sources", "dashboard_usefulness", "dashboard_improvements",
    "dashboard_improvements_other", "sensor_trust", "trust_concerns", "participation",
    "open_data_support", "additional_comments",
]


@admin.action(description="Export selected responses to CSV")
def export_responses(modeladmin, request, queryset):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="citizen_feedback.csv"'
    writer = csv.writer(response)
    writer.writerow(ANSWER_COLUMNS)
    for row in queryset:
        writer.writerow([
            ", ".join(value) if isinstance(value := getattr(row, column), list) else value
            for column in ANSWER_COLUMNS
        ])
    return response


@admin.register(SurveyResponse)
class SurveyResponseAdmin(admin.ModelAdmin):
    """Read-only: a submitted answer is a record of what somebody said, not a draft."""
    list_display = ("submitted_at", "county", "area_type", "air_quality_rating",
                    "dashboard_usefulness", "sensor_trust", "open_data_support")
    list_filter = ("county", "area_type", "air_quality_rating", "dashboard_usefulness",
                   "sensor_trust", "open_data_support", "submitted_at")
    search_fields = ("additional_comments", "dashboard_improvements_other")
    date_hierarchy = "submitted_at"
    actions = [export_responses]
    readonly_fields = [field.name for field in SurveyResponse._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SurveyContact)
class SurveyContactAdmin(admin.ModelAdmin):
    """Addresses given to receive the results, held apart from the answers.

    Nothing here links a person to what they said, and nothing should be added that
    would. Delete these once the summary has been sent.
    """
    list_display = ("email", "created_on")
    list_filter = ("created_on",)
    search_fields = ("email",)
    readonly_fields = ("created_on",)
