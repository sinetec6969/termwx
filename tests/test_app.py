from __future__ import annotations

from conftest import FakeHttp, FetchError
from termwx import units as U
from termwx.app import TermWx
from termwx.config import Config
from termwx.models import Location
from termwx.screens import AlertsScreen, LocationScreen, SpaceScreen
from termwx.widgets import AlertBanner, DataPanel

CLT = Location("Charlotte", 35.2271, -80.8431, "North Carolina", "United States", "US", "America/New_York")


def make_app(tmp_path, http=None, **kw) -> TermWx:
    cfg = Config(path=tmp_path / "config.toml", location=CLT, desktop_notify=False, bell=False, animate=False)
    return TermWx(cfg, http=http or FakeHttp(), **kw)


async def settle(pilot, app):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_app_loads_all_panels(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        assert app.location == CLT
        assert app.weather and app.weather.current.temp
        assert app.space and app.space.kp
        assert app.alerts_result and len(app.alerts_result.alerts) == 7
        for pid in ("current", "hourly", "forecast", "alerts", "space"):
            assert app.query_one(f"#{pid}", DataPanel).data is not None
        banner = app.query_one(AlertBanner)
        assert banner.has_class("-show") and banner.has_class("sev-moderate")
        # alerts were announced once and remembered
        assert len(app.seen_alert_ids) == 7


async def test_location_search_flow(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LocationScreen)
        await pilot.press(*"Denver, CO")
        await pilot.pause(0.6)
        await app.screen.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot, app)
        assert not isinstance(app.screen, LocationScreen)
        assert app.location.name == "Denver" and app.location.region == "Colorado"
        assert Config.load(tmp_path / "config.toml").location.name == "Denver"


async def test_units_toggle_refetches(tmp_path):
    http = FakeHttp()
    app = make_app(tmp_path, http=http)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        await pilot.press("u")
        await settle(pilot, app)
        assert app.config.units == U.METRIC
        forecast_calls = [p for url, p in http.calls if "v1/forecast" in url]
        assert forecast_calls[-1]["temperature_unit"] == "celsius"


async def test_demo_alert_and_alerts_screen(tmp_path):
    app = make_app(tmp_path, demo_alert=True)
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(pilot, app)
        banner = app.query_one(AlertBanner)
        assert banner.alerts[0].event == "Severe Thunderstorm Warning"
        assert banner.has_class("sev-severe")
        await pilot.press("w")
        await pilot.pause()
        assert isinstance(app.screen, AlertsScreen)
        await pilot.press("escape")
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, SpaceScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SpaceScreen)


async def test_offline_weather_shows_error_not_crash(tmp_path):
    http = FakeHttp({"open-meteo.com": FetchError("offline"), "services.swpc.noaa.gov": FetchError("offline")})
    app = make_app(tmp_path, http=http)
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot, app)
        assert app.weather is None and "weather" in app.errors
        assert "space" in app.errors


async def test_favorites(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        await pilot.press("s")
        assert app.config.is_favorite(CLT)
        app.config.favorites.append(Location("Denver", 39.74, -104.98, "Colorado", country_code="US"))
        await pilot.press("f")
        await settle(pilot, app)
        assert app.location.name == "Denver"
        await pilot.press("f")
        await settle(pilot, app)
        assert app.location.name == "Charlotte"


async def test_narrow_layout_classes(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(70, 30)) as pilot:
        await settle(pilot, app)
        assert app.screen.has_class("-narrow") and app.screen.has_class("-tiny")
        assert not app.query_one("#scene").display
