from __future__ import annotations

import json
from pathlib import Path

import pytest

from termwx.api.http import FetchError

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


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

    async def aclose(self):
        pass


@pytest.fixture
def fake_http():
    return FakeHttp()


__all__ = ["FakeHttp", "FetchError", "load"]
