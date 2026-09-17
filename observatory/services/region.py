"""The area the observatory covers, and whether a reading was taken inside it.

A sensor's GPS is not always right: a fix can land in the sea off Africa, at (0, 0), or
simply somewhere the project does not cover. A point like that is not a measurement of
Irish air, and drawn on the map it stretches the view across half the world. So readings
are checked against a boundary on the way in, and those outside it are not stored.

The built-in boundary is the **island of Ireland** — the Republic and Northern Ireland
together, as PEACE-Air spans both. It is deliberately drawn about ten kilometres out to
sea all the way round. Deleting a reading cannot be undone, so where the line is
uncertain it errs towards keeping: a sensor carried along a coast road, out to an island
or onto a boat in a bay stays inside, while anywhere off the island does not. Enclosed
water counts as inside for the same reason — the line is drawn across the mouths of
Galway Bay, the Shannon Estuary, Dublin Bay, Belfast Lough and Carlingford Lough rather
than following them inland.

Set ``OBSERVATORY_REGION_GEOJSON`` to a WGS84 GeoJSON file to use a different boundary,
or ``OBSERVATORY_RESTRICT_TO_REGION=0`` to keep everything.
"""
from __future__ import annotations

import functools

import numpy as np
import pandas as pd
from django.conf import settings

from ..aircasting.client import load_region

# The island of Ireland, drawn clockwise from the north and held roughly 10 km offshore,
# as (longitude, latitude). Coarse by design: this separates Ireland from Great Britain
# and from the rest of the world, and is not a coastline.
IRELAND = [
    (-7.40, 55.50), (-7.05, 55.40), (-6.85, 55.30), (-6.55, 55.32),   # Inishowen to Portrush
    (-6.20, 55.38), (-5.95, 55.25), (-5.60, 54.90), (-5.35, 54.65),   # Rathlin to Donaghadee
    (-5.30, 54.40), (-5.45, 54.15), (-5.80, 53.98), (-6.00, 53.75),   # Ards to Carlingford
    (-5.95, 53.45), (-5.90, 53.25), (-5.85, 52.95), (-6.05, 52.55),   # Skerries to Cahore
    (-6.20, 52.05), (-6.95, 52.00), (-7.55, 51.92), (-8.00, 51.68),   # Carnsore to Ballycotton
    (-8.55, 51.45), (-9.50, 51.28), (-9.95, 51.38), (-10.35, 51.52),  # Kinsale to Dursey
    (-10.60, 51.90), (-10.85, 52.15), (-10.20, 52.35), (-10.15, 52.60),  # Skelligs to Loop Head
    (-10.10, 52.95), (-10.15, 53.15), (-10.45, 53.40), (-10.50, 53.65),  # Clare to Inishbofin
    (-10.45, 53.95), (-10.40, 54.20), (-10.15, 54.45), (-9.30, 54.45),   # Achill to north Mayo
    (-8.60, 54.50), (-8.45, 54.60), (-8.95, 54.70), (-8.80, 54.90),      # Sligo to Donegal Bay
    (-8.75, 55.05), (-8.45, 55.30), (-8.05, 55.35), (-7.70, 55.40),      # Arranmore to Fanad
]


def enabled() -> bool:
    return bool(getattr(settings, "OBSERVATORY_RESTRICT_TO_REGION", True))


def name() -> str:
    return getattr(settings, "OBSERVATORY_REGION_NAME", "Ireland")


@functools.lru_cache(maxsize=4)
def _polygons_for(geojson: str):
    """The boundary, read once per configured file.

    A polygon is a list of rings: the outline first, then any holes. ``load_region`` is
    the notebook's own GeoJSON reader, so a boundary supplied here is understood exactly
    as the downloader's region filter understands one — including its checks for WGS84,
    closed rings and the antimeridian.
    """
    if not geojson:
        return ((tuple(IRELAND),),)
    found = load_region(geojson)
    if not found:
        raise ValueError(f"{geojson} contains no polygons to use as a boundary")
    return tuple(tuple(tuple((float(p[0]), float(p[1])) for p in ring) for ring in polygon)
                 for polygon in found)


def polygons():
    return _polygons_for(getattr(settings, "OBSERVATORY_REGION_GEOJSON", "") or "")


def _ring_contains(lon: np.ndarray, lat: np.ndarray, ring) -> np.ndarray:
    """Vectorised ray casting: how many edges lie to the left of each point.

    One pass per edge over every point rather than one pass per point over every edge,
    because an import can carry hundreds of thousands of readings and a boundary has a
    few dozen sides.
    """
    inside = np.zeros(lon.shape, dtype=bool)
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if y1 == y2:
            continue  # a horizontal edge is never crossed by a horizontal ray
        crosses = (y1 > lat) != (y2 > lat)
        cut = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
        inside ^= crosses & (lon < cut)
    return inside


def contains(latitude, longitude) -> bool:
    """Whether one point is inside the region. A missing coordinate is not."""
    values = inside(pd.Series([latitude], dtype="float64"), pd.Series([longitude], dtype="float64"))
    return bool(values.iat[0])


def inside(latitudes: pd.Series, longitudes: pd.Series) -> pd.Series:
    """Which of these points are inside the region.

    Rows without coordinates answer ``False``: they are not inside anything. Callers that
    keep such rows — an indoor session has its coordinates withheld, and its readings are
    still real — have to say so themselves, which :func:`outside` does.
    """
    lat = pd.to_numeric(latitudes, errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
    lon = pd.to_numeric(longitudes, errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
    known = np.isfinite(lat) & np.isfinite(lon)
    result = np.zeros(lat.shape, dtype=bool)
    if known.any():
        x, y = lon[known], lat[known]
        within = np.zeros(x.shape, dtype=bool)
        for polygon in polygons():
            # The first ring is the outline and the rest are holes cut out of it, which
            # is how GeoJSON describes, say, a lake inside a boundary.
            covered = _ring_contains(x, y, list(polygon[0]))
            for hole in polygon[1:]:
                covered &= ~_ring_contains(x, y, list(hole))
            within |= covered
        result[known] = within
    return pd.Series(result, index=latitudes.index)


def outside(latitudes: pd.Series, longitudes: pd.Series) -> pd.Series:
    """Which points are known to be outside the region.

    A reading with no coordinates is not outside it — nothing is known about where it was
    taken, and AirCasting withholds the position of every indoor session.
    """
    if not enabled():
        return pd.Series(False, index=latitudes.index)
    located = pd.to_numeric(latitudes, errors="coerce").notna() & pd.to_numeric(longitudes, errors="coerce").notna()
    return located & ~inside(latitudes, longitudes)
