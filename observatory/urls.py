from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import RedirectView

from . import api, views

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="observatory/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # About is the front door: the logo links here and so does the bare domain.
    path("", RedirectView.as_view(pattern_name="about", permanent=False), name="home"),
    path("about/", views.about, name="about"),
    path("map/", views.map_explorer, name="map"),
    path("data/", views.data_manager, name="data"),
    path("reports/", views.reports, name="reports"),

    # Station analysis now lives inside the map explorer, and Settings inside the data
    # manager; both keep redirecting so existing links and bookmarks still land somewhere.
    path("analysis/", views.analysis_redirect, name="analysis"),
    path("settings/", RedirectView.as_view(pattern_name="data", permanent=False), name="settings"),
    path("data/stations/<str:code>/", views.station_update, name="station_update"),
    path("data/stations/<str:code>/delete/", views.station_delete, name="station_delete"),
    path("data/devices/add/", views.device_add, name="device_add"),
    path("data/devices/<int:pk>/delete/", views.device_delete, name="device_delete"),
    path("data/devices/<int:pk>/test/", views.device_test, name="device_test"),

    path("api/stations/", api.stations_geojson, name="api_stations"),
    path("api/stations/<str:code>/", api.station_detail, name="api_station"),
    path("api/stations/<str:code>/analysis/", api.station_analysis, name="api_analysis"),
    path("api/stations/<str:code>/export.csv", api.station_export, name="api_export"),
    path("api/summary/", api.summary, name="api_summary"),
    path("api/datasets/", api.datasets, name="api_datasets"),
    path("api/datasets/delete/", api.datasets_delete, name="api_datasets_delete"),
    path("api/devices/", api.devices, name="api_devices"),
    path("api/orphans/", api.orphans, name="api_orphans"),
    path("api/orphans/purge/", api.orphans_purge, name="api_orphans_purge"),
    path("api/imports/", api.imports, name="api_imports"),
    path("api/import/", api.import_upload, name="api_import"),
    path("api/sync/", api.sync, name="api_sync"),
    path("api/schedule/", api.schedule, name="api_schedule"),
    path("datasets/download/", api.datasets_zip, name="datasets_zip"),
    path("datasets/<int:pk>/download/", api.dataset_download, name="dataset_download"),
]
