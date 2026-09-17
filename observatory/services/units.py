"""Present temperatures in Celsius.

AirBeam sensors report degrees Fahrenheit, which is what the CSV files under
``stations/`` hold. Like the observation-range filter, nothing here rewrites stored
readings: the conversion happens on the way out, at every boundary where a value is
plotted, summarised or exported. That keeps the archive faithful to what the sensor
sent while every number a person sees is in Celsius.

The trigger is the recorded *unit*, not the measurement key, so a file that already
holds Celsius is never converted a second time.
"""
from __future__ import annotations

import pandas as pd

FAHRENHEIT_UNITS = {"f", "°f", "degf", "deg f", "fahrenheit", "degrees fahrenheit"}
CELSIUS = "°C"


def is_fahrenheit(unit, measurement: str = "") -> bool:
    text = str(unit or "").strip().lower()
    if text:
        return text in FAHRENHEIT_UNITS
    # Readings imported without a unit fall back to the channel name AirBeam uses.
    return str(measurement or "").strip().upper() == "F"


def to_celsius(values):
    return (values - 32.0) * 5.0 / 9.0


def display_unit(unit, measurement: str = "") -> str:
    """The unit to show for a reading, once it has been through :func:`convert`."""
    return CELSIUS if is_fahrenheit(unit, measurement) else (unit or "")


def convert(frame: pd.DataFrame, measurement: str = "") -> pd.DataFrame:
    """Return ``frame`` with any Fahrenheit readings restated in Celsius.

    The input is never modified. Statistics computed afterwards come out right without
    special cases: a standard deviation of converted values carries the 5/9 scale and
    drops the 32° offset on its own.
    """
    if frame.empty or "value" not in frame:
        return frame
    units = frame["unit"] if "unit" in frame else pd.Series("", index=frame.index)
    mask = units.map(lambda unit: is_fahrenheit(unit, measurement))
    if not mask.any():
        return frame
    out = frame.copy()
    out.loc[mask, "value"] = to_celsius(out.loc[mask, "value"])
    if "unit" in out:
        out.loc[mask, "unit"] = CELSIUS
    return out


def convert_measurements(measurements: list[dict]) -> list[dict]:
    """Restate the units in a station's measurement summary for display."""
    return [{**m, "unit": display_unit(m.get("unit", ""), m.get("key", ""))} for m in measurements]
