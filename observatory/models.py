"""Metadata models.

Readings themselves are stored as CSV files (see ``services/storage.py``). The database
only holds what the interface needs to look things up quickly: stations and the device
keys that map onto them, registered AirCasting devices, the import log, the catalogue
of downloadable weekly datasets and the import schedule.
"""
from datetime import date, timedelta

from django.db import models
from django.utils import timezone


class Station(models.Model):
    LOCATION_SOURCES = [
        ("fixed_session", "Fixed session coordinates"),
        ("measurement_median", "Median of mobile readings"),
        ("file", "Coordinates in imported file"),
        ("manual", "Entered manually"),
        ("none", "No location yet"),
    ]
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=120)
    region = models.CharField(max_length=80, blank=True, default="Ireland")
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    location_source = models.CharField(max_length=24, choices=LOCATION_SOURCES, default="none")
    is_indoor = models.BooleanField(default=False)
    first_reading = models.DateTimeField(null=True, blank=True)
    last_reading = models.DateTimeField(null=True, blank=True)
    reading_count = models.PositiveBigIntegerField(default=0)
    # [{"key": "PM2.5", "label": "PM2.5", "unit": "µg/m³", "type": "Particulate Matter", "count": 1008}]
    measurements = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None


class DeviceAlias(models.Model):
    """Maps a source identity (e.g. an AirCasting ``device_group``) to a station.

    This is what keeps a fixed station with many recordings as one map point: every
    import resolves its rows through this table, and a new source whose coordinates
    match an existing station is attached to that station instead of creating a copy.
    """
    key = models.CharField(max_length=255, unique=True)
    station = models.ForeignKey(Station, related_name="aliases", on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.key} → {self.station.code}"


class AirCastingDevice(models.Model):
    device_id = models.CharField(max_length=64, unique=True, help_text="As written on the label, e.g. AIRBEAM3:B0B21C7627C4")
    label = models.CharField(max_length=120, blank=True)
    known_session_ids = models.CharField(max_length=255, blank=True, help_text="Comma-separated session numbers from map links")
    project_tags = models.CharField(max_length=255, blank=True, help_text="Comma-separated authorised project tags")
    download_from = models.DateField(default=date(2020, 1, 1),
                                     help_text="Download recordings made from this date onwards")
    download_until = models.DateField(null=True, blank=True,
                                      help_text="Leave empty to keep downloading up to now")
    station = models.ForeignKey(Station, null=True, blank=True, on_delete=models.SET_NULL,
                                help_text="Optional: store this device's readings under an existing station")
    active = models.BooleanField(default=True)
    last_success = models.DateTimeField(null=True, blank=True)
    last_attempt = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, help_text="Why the most recent download failed; cleared on success")

    def __str__(self):
        return self.label or self.device_id

    def session_ids(self):
        return [int(s) for s in self.known_session_ids.replace(";", ",").split(",") if s.strip().isdigit()]

    def tags(self):
        return [t.strip() for t in self.project_tags.split(",") if t.strip()]

    @property
    def status(self):
        """One word for the sensor list: what happened on the most recent attempt."""
        if not self.active:
            return "paused"
        if self.last_error:
            return "failed"
        if self.last_success:
            return "ready"
        return "waiting" if self.last_attempt else "new"


class ImportRun(models.Model):
    SOURCES = [("upload", "File upload"), ("aircasting", "AirCasting sync"), ("cli", "Command line"),
               ("demo", "Demo data"), ("deletion", "Deletion")]
    STATUSES = [("running", "Running"), ("complete", "Complete"), ("partial", "Complete with warnings"), ("failed", "Failed")]
    source = models.CharField(max_length=16, choices=SOURCES)
    status = models.CharField(max_length=16, choices=STATUSES, default="running")
    filename = models.CharField(max_length=255, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    rows_read = models.PositiveBigIntegerField(default=0)
    rows_stored = models.PositiveBigIntegerField(default=0)
    rows_duplicate = models.PositiveBigIntegerField(default=0)
    rows_rejected = models.PositiveBigIntegerField(default=0)
    rows_deleted = models.PositiveBigIntegerField(default=0)
    stations = models.JSONField(default=list, blank=True)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.get_source_display()} {self.started_at:%Y-%m-%d %H:%M} ({self.status})"


class Dataset(models.Model):
    """A downloadable weekly CSV per station, rebuilt whenever an import touches that week."""
    station = models.ForeignKey(Station, related_name="datasets", on_delete=models.CASCADE)
    period_start = models.DateField()
    period_end = models.DateField()
    filename = models.CharField(max_length=255)
    readings = models.PositiveBigIntegerField(default=0)
    measurements = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-period_start", "station__name"]
        constraints = [models.UniqueConstraint(fields=["station", "period_start"], name="one_dataset_per_station_week")]

    def __str__(self):
        return self.filename


class ScheduleConfig(models.Model):
    enabled = models.BooleanField(default=False)
    interval_minutes = models.PositiveIntegerField(default=15)
    last_run = models.DateTimeField(null=True, blank=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def next_run(self):
        if not self.enabled:
            return None
        base = self.last_run or timezone.now()
        nxt = base + timedelta(minutes=self.interval_minutes)
        return max(nxt, timezone.now()) if self.last_run else nxt
