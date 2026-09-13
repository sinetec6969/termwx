"""Open-Meteo forecast + air quality."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from .. import units as U
from ..models import Current, Day, Hour, Location, Weather
from .http import FetchError, Http

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AQI_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

CURRENT_VARS = [
    "temperature_2m", "apparent_temperature", "relative_humidity_2m", "dew_point_2m", "weather_code",
    "is_day", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "pressure_msl", "cloud_cover",
    "precipitation", "visibility", "uv_index",
]
HOURLY_VARS = ["temperature_2m", "precipitation_probability", "weather_code", "pressure_msl"]
DAILY_VARS = [
    "weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_probability_max",
    "precipitation_sum", "wind_speed_10m_max", "sunrise", "sunset", "uv_index_max",
]


def forecast_params(loc: Location, units: str) -> dict:
    return {
        "latitude": round(loc.lat, 4),
        "longitude": round(loc.lon, 4),
        "current": ",".join(CURRENT_VARS),
        "hourly": ",".join(HOURLY_VARS),
        "daily": ",".join(DAILY_VARS),
        "forecast_days": 5,
        "timezone": "auto",
        **U.api_params(units),
    }


def _f(v, default: float = 0.0) -> float:
    return default if v is None else float(v)


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def parse_forecast(data: dict, loc: Location, units: str, aqi: dict | None = None) -> Weather:
    c = data["current"]
    current = Current(
        time=datetime.fromisoformat(c["time"]),
        temp=_f(c.get("temperature_2m")),
        feels_like=_f(c.get("apparent_temperature")),
        humidity=_f(c.get("relative_humidity_2m")),
        dew_point=_f(c.get("dew_point_2m")),
        code=int(c.get("weather_code") or 0),
        is_day=bool(c.get("is_day", 1)),
        wind_speed=_f(c.get("wind_speed_10m")),
        wind_dir=_f(c.get("wind_direction_10m")),
        wind_gust=_f(c.get("wind_gusts_10m")),
        pressure=_f(c.get("pressure_msl")),
        cloud_cover=_f(c.get("cloud_cover")),
        precip=_f(c.get("precipitation")),
        visibility=_f(c.get("visibility"), 20000),
        uv=_f(c.get("uv_index")),
    )

    h = data.get("hourly", {})
    times = [datetime.fromisoformat(t) for t in h.get("time", [])]
    start_hour = current.time.replace(minute=0, second=0, microsecond=0)
    start = next((i for i, t in enumerate(times) if t >= start_hour), 0)
    hours = [
        Hour(
            time=times[i],
            temp=_f(h["temperature_2m"][i]),
            precip_prob=_f(h["precipitation_probability"][i]),
            code=int(h["weather_code"][i] or 0),
            pressure=_f(h["pressure_msl"][i]),
        )
        for i in range(start, min(start + 48, len(times)))
    ]

    d = data.get("daily", {})
    days = [
        Day(
            date=date.fromisoformat(t),
            code=int(d["weather_code"][i] or 0),
            temp_max=_f(d["temperature_2m_max"][i]),
            temp_min=_f(d["temperature_2m_min"][i]),
            precip_prob=_f(d["precipitation_probability_max"][i]),
            precip_sum=_f(d["precipitation_sum"][i]),
            wind_max=_f(d["wind_speed_10m_max"][i]),
            sunrise=_dt(d["sunrise"][i]),
            sunset=_dt(d["sunset"][i]),
            uv_max=_f(d["uv_index_max"][i]),
        )
        for i, t in enumerate(d.get("time", []))
    ]

    aqi_val = None
    if aqi:
        v = (aqi.get("current") or {}).get("us_aqi")
        aqi_val = int(v) if v is not None else None

    if not loc.timezone:
        loc.timezone = data.get("timezone", "")
    return Weather(
        location=loc,
        units=units,
        current=current,
        hours=hours,
        days=days,
        aqi=aqi_val,
        timezone_abbr=data.get("timezone_abbreviation", ""),
    )


async def fetch_weather(http: Http, loc: Location, units: str) -> Weather:
    async def get_aqi():
        try:
            data, _ = await http.get_json(
                AQI_URL, {"latitude": round(loc.lat, 4), "longitude": round(loc.lon, 4), "current": "us_aqi"}
            )
            return data
        except FetchError:
            return None

    (data, stale), aqi = await asyncio.gather(
        http.get_json(FORECAST_URL, forecast_params(loc, units)), get_aqi()
    )
    w = parse_forecast(data, loc, units, aqi)
    w.stale = stale
    return w
