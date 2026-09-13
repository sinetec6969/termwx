"""Unit handling and value formatting."""

from __future__ import annotations

IMPERIAL = "imperial"
METRIC = "metric"

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
# Arrow shows where the wind is blowing *to*.
ARROWS = ["↓", "↙", "←", "↖", "↑", "↗", "→", "↘"]


def api_params(units: str) -> dict[str, str]:
    if units == IMPERIAL:
        return {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch"}
    return {"temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm"}


def temp(v: float | None, units: str, suffix: bool = False) -> str:
    if v is None:
        return "--"
    s = f"{round(v):d}°"
    return s + ("F" if units == IMPERIAL else "C") if suffix else s


def speed_unit(units: str) -> str:
    return "mph" if units == IMPERIAL else "km/h"


def speed(v: float | None, units: str) -> str:
    return "--" if v is None else f"{round(v)} {speed_unit(units)}"


def precip(v: float | None, units: str) -> str:
    if v is None:
        return "--"
    return f"{v:.2f} in" if units == IMPERIAL else f"{v:.1f} mm"


def pressure(hpa: float | None, units: str) -> str:
    if hpa is None:
        return "--"
    return f"{hpa * 0.0295300:.2f} inHg" if units == IMPERIAL else f"{hpa:.0f} hPa"


def visibility(meters: float | None, units: str) -> str:
    if meters is None:
        return "--"
    if units == IMPERIAL:
        miles = meters / 1609.344
        return "10+ mi" if miles >= 10 else f"{miles:.1f} mi"
    km = meters / 1000
    return "20+ km" if km >= 20 else f"{km:.1f} km"


def compass(deg: float | None) -> str:
    if deg is None:
        return ""
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def wind_arrow(deg: float | None) -> str:
    if deg is None:
        return ""
    return ARROWS[int((deg % 360) / 45 + 0.5) % 8]


def to_fahrenheit(v: float, units: str) -> float:
    return v if units == IMPERIAL else v * 9 / 5 + 32


def temp_color(v: float, units: str) -> str:
    """Hex color on a cold→hot ramp, keyed on °F."""
    f = to_fahrenheit(v, units)
    stops = [
        (-10, "#b388ff"),
        (15, "#7c9cff"),
        (32, "#5ec8ff"),
        (50, "#4fe3c1"),
        (65, "#9be15d"),
        (75, "#f5d547"),
        (85, "#ff9f43"),
        (95, "#ff5b5b"),
        (110, "#ff2d95"),
    ]
    if f <= stops[0][0]:
        return stops[0][1]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if f <= t1:
            k = (f - t0) / (t1 - t0)
            return _mix(c0, c1, k)
    return stops[-1][1]


def _mix(a: str, b: str, k: float) -> str:
    ra, ga, ba = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    rb, gb, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return "#{:02x}{:02x}{:02x}".format(
        round(ra + (rb - ra) * k), round(ga + (gb - ga) * k), round(ba + (bb - ba) * k)
    )


def uv_label(uv: float | None) -> tuple[str, str]:
    if uv is None:
        return "--", "dim"
    if uv < 3:
        return f"{uv:.0f} Low", "#9be15d"
    if uv < 6:
        return f"{uv:.0f} Moderate", "#f5d547"
    if uv < 8:
        return f"{uv:.0f} High", "#ff9f43"
    if uv < 11:
        return f"{uv:.0f} Very High", "#ff5b5b"
    return f"{uv:.0f} Extreme", "#c86bfa"


def aqi_label(aqi: int | None) -> tuple[str, str]:
    if aqi is None:
        return "--", "dim"
    for limit, name, color in [
        (50, "Good", "#9be15d"),
        (100, "Moderate", "#f5d547"),
        (150, "Unhealthy (SG)", "#ff9f43"),
        (200, "Unhealthy", "#ff5b5b"),
        (300, "Very Unhealthy", "#c86bfa"),
    ]:
        if aqi <= limit:
            return f"{aqi} {name}", color
    return f"{aqi} Hazardous", "#b0306a"
