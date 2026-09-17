"""Synthetic demo network: 12 fixed stations across Ireland.

The demo writes files in the *AirCasting export layout* and imports them through the
normal pipeline, so it exercises exactly the code path real downloads use. Every file
and station produced here is labelled as demo data.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from ..aircasting.client import COLUMNS
from ..models import DeviceAlias, Station
from . import storage

STATIONS = [
    ("GW-014", "Galway", 53.2743, -9.0514), ("DB-008", "Dublin", 53.3498, -6.2603),
    ("CK-003", "Cork", 51.8985, -8.4756), ("LM-006", "Limerick", 52.6638, -8.6267),
    ("SL-002", "Sligo", 54.2766, -8.4761), ("DL-011", "Letterkenny", 54.9558, -7.7342),
    ("AT-005", "Athlone", 53.4239, -7.9407), ("WF-009", "Waterford", 52.2593, -7.1101),
    ("KK-004", "Kilkenny", 52.6541, -7.2448), ("TR-007", "Tralee", 52.2713, -9.7026),
    ("DK-010", "Dundalk", 54.0090, -6.4049), ("CB-012", "Castlebar", 53.8550, -9.2988),
]
SENSORS = [("AirBeam3-PM2.5", "Particulate Matter", "µg/m³"), ("AirBeam3-PM10", "Particulate Matter", "µg/m³"),
           ("AirBeam3-PM1", "Particulate Matter", "µg/m³"), ("AirBeam3-RH", "Humidity", "%"),
           ("AirBeam3-F", "Temperature", "F")]


def _series(rng, n, base, minutes):
    hours = np.arange(n) * minutes / 60
    diurnal = 1 + 0.25 * np.sin((hours % 24 - 7) / 24 * 2 * np.pi) + 0.2 * np.exp(-((hours % 24 - 19) ** 2) / 6)
    ar = np.zeros(n)
    for i in range(1, n):
        ar[i] = 0.93 * ar[i - 1] + rng.normal(0, 0.12)
    episodes = np.zeros(n)
    for _ in range(rng.integers(1, 4)):
        centre, width, height = rng.integers(0, n), rng.integers(20, 60), rng.uniform(0.6, 1.6)
        episodes += height * np.exp(-((np.arange(n) - centre) ** 2) / (2 * width ** 2))
    return base * diurnal * np.exp(ar + episodes)


def build(days: int = 14, minutes: int = 10, end: datetime | None = None, seed: int = 7) -> list:
    rng = np.random.default_rng(seed)
    end = (end or datetime.now()).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    times = pd.date_range(start, end, freq=f"{minutes}min", inclusive="left")
    n = len(times)
    folder = storage.sub_dir("raw", "demo")
    paths = []
    for i, (code, name, lat, lon) in enumerate(STATIONS):
        key = f"demo:AirBeam3-DEMO{i:02d}"
        station, _ = Station.objects.update_or_create(code=code, defaults={"name": name, "region": "Ireland"})
        DeviceAlias.objects.update_or_create(key=key, defaults={"station": station})
        urban = 1.5 if name in {"Dublin", "Cork", "Limerick"} else 1.0
        pm25 = _series(rng, n, rng.uniform(6.5, 9.5) * urban, minutes)
        values = {
            "AirBeam3-PM2.5": pm25,
            "AirBeam3-PM1": pm25 * rng.uniform(0.62, 0.72) + rng.normal(0, 0.3, n),
            "AirBeam3-PM10": pm25 * rng.uniform(1.5, 1.9) + np.abs(rng.normal(0, 1.5, n)),
            "AirBeam3-RH": np.clip(82 - 12 * np.sin(((np.arange(n) * minutes / 60) % 24 - 9) / 24 * 2 * np.pi) + rng.normal(0, 3, n), 35, 100),
            "AirBeam3-F": 57 + 7 * np.sin(((np.arange(n) * minutes / 60) % 24 - 9) / 24 * 2 * np.pi) + rng.normal(0, 1.2, n),
        }
        rows = []
        raw_ms = (times - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)
        for s, (sensor, mtype, unit) in enumerate(SENSORS):
            v = np.round(np.clip(values[sensor], 0.5, None), 1)
            rows.append(pd.DataFrame({
                "device_group": key, "sensor_package_name": "", "device_query": key, "session_id": 900000 + i,
                "stream_id": 9000000 + i * 10 + s, "session_type": "FixedSession", "sensor_name": sensor,
                "measurement_type": mtype, "unit": unit, "raw_time": raw_ms, "source_time": times.strftime("%Y-%m-%dT%H:%M:%S"),
                "time_utc": "", "time_status": "source_clock_unverified", "value": v, "latitude": lat, "longitude": lon,
                "coordinate_source": "fixed_session", "source_row_index": np.arange(1, n + 1)}))
        path = folder / f"demo_{code}.csv"
        pd.concat(rows)[COLUMNS].to_csv(path, index=False)
        paths.append(path)
    return paths
