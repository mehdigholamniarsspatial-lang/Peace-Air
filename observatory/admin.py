from django.contrib import admin

from .models import AirCastingDevice, Dataset, DeviceAlias, ImportRun, ScheduleConfig, Station
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
