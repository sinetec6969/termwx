from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from termwx import aviation
from termwx.api.http import FetchError

FIXTURES = Path(__file__).parent / "fixtures"
# The AWC/NWS aviation fixtures were recorded around this time.
FIXTURE_NOW = datetime(2026, 10, 7, 16, 10, tzinfo=timezone.utc)


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture(autouse=True)
def no_notam_credentials(monkeypatch):
    """Never reach the real FAA NMS from tests, whatever the developer's environment holds."""
    from termwx.api import faa_notam

    for var in (faa_notam.ENV_ID, faa_notam.ENV_SECRET, faa_notam.ENV_SERVER):
        monkeypatch.delenv(var, raising=False)
    faa_notam.forget_token()


@pytest.fixture(autouse=True)
def pinned_aviation_clock(monkeypatch):
    """Ages, validity windows and expiry are computed against the fixtures' recording time."""
    monkeypatch.setattr(aviation, "utcnow", lambda: FIXTURE_NOW)


ROUTES = [
    ("geocoding-api.open-meteo.com", "geocode.json"),
    ("air-quality-api.open-meteo.com", "aqi.json"),
    ("api.open-meteo.com/v1/forecast", "openmeteo.json"),
    ("api.weather.gov/alerts", "nws_alerts.json"),
    ("noaa-scales.json", "scales.json"),
    ("noaa-planetary-k-index-forecast.json", "kp.json"),
    ("rtsw_wind_1m.json", "rtsw_wind.json"),
    ("rtsw_mag_1m.json", "rtsw_mag.json"),
    ("solar-wind-mag-field.json", "mag.json"),
    ("xray-flares-latest.json", "flares.json"),
    ("10cm-flux.json", "f107.json"),
    ("products/alerts.json", "swpc_alerts.json"),
    ("ovation_aurora_latest.json", "aurora.json"),
    ("aviationweather.gov/api/data/stationinfo", "awc_stationinfo.json"),
    ("aviationweather.gov/api/data/airport", "awc_airport.json"),
    ("aviationweather.gov/api/data/metar", "awc_metar.json"),
    ("aviationweather.gov/api/data/taf", "awc_taf.json"),
    ("aviationweather.gov/api/data/pirep", "awc_pirep.json"),
    ("aviationweather.gov/api/data/airsigmet", "awc_airsigmet.json"),
    ("aviationweather.gov/api/data/isigmet", "awc_isigmet.json"),
    ("aviationweather.gov/api/data/gairmet", "awc_gairmet.json"),
    ("aviationweather.gov/api/data/airmet", "awc_airmet_ak.geojson.json"),
    ("aviationweather.gov/api/data/cwa", "awc_cwa.json"),
    ("api.weather.gov/points/", "nws_points.json"),
    ("api.weather.gov/products/types/AFD", "nws_afd_list.json"),
    ("api.weather.gov/products/", "nws_afd.json"),
    ("tfr.faa.gov/geoserver/TFR/ows", "faa_tfr_wfs.json"),
]
TEXT_ROUTES = [
    ("aviationweather.gov/api/data/windtemp", "awc_windtemp_us.txt"),
    ("tfr.faa.gov/download/detail_6_9169.xml", "faa_tfr_detail_6_9169.xml"),
    ("tfr.faa.gov/download/detail_6_4348.xml", "faa_tfr_detail_6_4348.xml"),
]


class FakeHttp:
    """Serves fixtures instead of the network and records requests."""

    def __init__(self, overrides: dict | None = None):
        self.calls: list[tuple[str, dict | None]] = []
        self.overrides = overrides or {}

    async def get_json(self, url, params=None, headers=None, cache=True):
        self.calls.append((url, params))
        for needle, value in self.overrides.items():
            if needle in url:
                if isinstance(value, Exception):
                    raise value
                return value, False
        if "ipinfo.io" in url:
            return {"city": "Charlotte", "region": "North Carolina", "country": "US", "loc": "35.2271,-80.8431"}, False
        for needle, fixture in ROUTES:
            if needle in url:
                return load(fixture), False
        raise FetchError(f"no fixture for {url}")

    async def get_text(self, url, params=None, headers=None):
        self.calls.append((url, params))
        for needle, value in self.overrides.items():
            if needle in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise FetchError(f"no fixture for {url}")

    async def get_text_cached(self, url, params=None, headers=None):
        self.calls.append((url, params))
        for needle, value in self.overrides.items():
            if needle in url:
                if isinstance(value, Exception):
                    raise value
                return value or "", False
        for needle, fixture in TEXT_ROUTES:
            if needle in url:
                return load_text(fixture), False
        raise FetchError(f"no fixture for {url}")

    async def aclose(self):
        pass


@pytest.fixture
def fake_http():
    return FakeHttp()


__all__ = ["FIXTURE_NOW", "FakeHttp", "FetchError", "load", "load_text"]
