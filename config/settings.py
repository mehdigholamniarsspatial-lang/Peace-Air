"""Django settings for the Air Quality Observatory."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "observatory",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "observatory.context_processors.platform",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-ie"
# AirCasting fixed-session timestamps are a *source clock* whose convention is not
# verified (see the notebook). The platform therefore keeps naive wall-clock times
# end-to-end rather than silently converting them.
TIME_ZONE = "Europe/Dublin"
USE_I18N = True
USE_TZ = False

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "map"
LOGOUT_REDIRECT_URL = "login"

DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# --- Observatory-specific settings -------------------------------------------------
# All sensor readings live as CSV files under this folder (the database only keeps
# station metadata, import history and the dataset catalogue).
OBSERVATORY_DATA_DIR = Path(os.environ.get("OBSERVATORY_DATA_DIR", BASE_DIR / "data"))
OBSERVATORY_MAX_UPLOAD_MB = int(os.environ.get("OBSERVATORY_MAX_UPLOAD_MB", "200"))
OBSERVATORY_DEFAULT_MEASUREMENT = "PM2.5"
OBSERVATORY_DEFAULT_REGION = "Ireland"
# Two stations whose coordinates round to the same value at this precision are
# treated as one spatial point (5 decimal places is roughly 1 m).
OBSERVATORY_COORD_PRECISION = 5
# Esri World Topographic Map tiles (Web Mercator), displayed through Leaflet.
OBSERVATORY_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Topo_Map/MapServer/tile/{z}/{y}/{x}"
)
OBSERVATORY_TILE_ATTRIBUTION = (
    'Tiles &copy; <a href="https://www.esri.com/">Esri</a> &mdash; Esri, HERE, Garmin, '
    'Intermap, increment P Corp., GEBCO, USGS, FAO, NPS, NRCAN, GeoBase, IGN, '
    'Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), '
    'OpenStreetMap contributors, and the GIS User Community'
)
# AirCasting downloader (port of the supplied Jupyter notebook)
AIRCASTING_BASE_URL = "https://aircasting.org"
AIRCASTING_TIME_CONVENTION = os.environ.get("AIRCASTING_TIME_CONVENTION", "unverified")
AIRCASTING_TIMEZONE = "Europe/Dublin"
AIRCASTING_SYNC_OVERLAP_DAYS = 2

LOGGING = {
    "version": 1,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"observatory": {"handlers": ["console"], "level": "INFO"}},
}
