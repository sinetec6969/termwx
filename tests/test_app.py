from __future__ import annotations

from datetime import timedelta

import pytest

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


def shown(widget) -> str:
    """Plain text of what a Static/DataPanel currently displays."""
    from rich.console import Console

    console = Console(width=200, record=True, color_system=None, file=open("/dev/null", "w"))
    console.print(widget.content)
    return console.export_text()


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


# ── aviation ─────────────────────────────────────────────────────────────────
async def test_aviation_screen_opens_with_v(tmp_path):
    from termwx.screens_aviation import AviationScreen

    app = make_app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        av = app.aviation
        assert av.station.icao == "KCLT" and av.metar and av.taf.data and av.pireps.data is not None
        assert av.hazards.data and av.winds.data and av.winds.data.site == "GSP"
        panel = app.query_one("#aviation", DataPanel)
        assert panel.data is av and "KCLT" in shown(panel)
        await pilot.press("v")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, AviationScreen)
        assert "Charlotte/Douglas Intl" in shown(screen.query_one("#av-header"))
        assert "RWY 18C" in shown(screen.query_one("#av-tools"))  # from published runway data
        assert "22003KT" in shown(screen.query_one("#av-taf"))
        await pilot.press("right_square_bracket")
        assert app.runway[0] == "36C"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, AviationScreen)


async def test_aviation_screen_runway_and_airport_prompts(tmp_path):
    from termwx.screens_aviation import AviationScreen, PromptScreen

    app = make_app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        await pilot.press("v", "h")
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        await pilot.press(*"27", "enter")
        await pilot.pause()
        assert isinstance(app.screen, AviationScreen) and app.runway == ("27", 270)
        await pilot.press("c")
        await pilot.pause()
        app.screen.query_one("Input").value = ""
        await pilot.press(*"kjqf", "enter")
        await settle(pilot, app)
        assert app.config.aviation_station == "KJQF"
        assert app.aviation.choice.source == "explicit" and app.aviation.station.icao == "KJQF"
        assert "Concord Rgnl" in shown(app.screen.query_one("#av-header"))
        await pilot.press("c")
        await pilot.pause()
        app.screen.query_one("Input").value = ""
        await pilot.press(*"kzzz", "enter")
        await settle(pilot, app)
        assert app.aviation.station is None and "KZZZ not found" in shown(app.screen.query_one("#av-header"))
        await pilot.press("c")
        await pilot.pause()
        app.screen.query_one("Input").value = ""
        await pilot.press("enter")
        await settle(pilot, app)
        assert app.config.aviation_station == "" and app.aviation.station.icao == "KCLT"


async def test_discussion_screen_opens_with_d(tmp_path):
    from termwx.screens_aviation import DiscussionScreen

    app = make_app(tmp_path)
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(pilot, app)
        assert app.office == "GSP" and app.discussion and app.discussion.office == "GSP"
        assert "AFD GSP" in shown(app.query_one("#alerts", DataPanel))
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, DiscussionScreen)
        assert "AVIATION" in shown(app.screen.query_one("#afd-text"))
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DiscussionScreen)


async def test_aviation_partial_failure_keeps_everything_else(tmp_path):
    http = FakeHttp({
        "api/data/metar": FetchError("HTTP 502", 502),
        "api/data/pirep": FetchError("timeout"),
        "api/data/gairmet": FetchError("HTTP 503", 503),
        "api.weather.gov/products": FetchError("timeout"),
    })
    app = make_app(tmp_path, http=http)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        av = app.aviation
        assert av.station.icao == "KCLT" and av.metar is None and av.metars.error == "HTTP 502"
        assert av.pireps.error == "timeout" and av.taf.data is not None
        assert av.hazards.data.errors == ["G-AIRMET: HTTP 503"] and av.winds.data
        assert app.weather and app.space and app.alerts_result  # nothing else affected
        assert app.discussion is None and app.discussion_error == "timeout"
        summary = shown(app.query_one("#aviation", DataPanel))
        assert "HTTP 502" in summary and "PIREPs unavailable" in summary
        await pilot.press("v", "d")  # both screens still open
        await pilot.pause()


async def test_aviation_offline_with_no_cache(tmp_path):
    http = FakeHttp({"aviationweather.gov": FetchError("offline")})
    app = make_app(tmp_path, http=http)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        assert app.aviation.station is None and "offline" in app.aviation.choice.error
        assert app.aviation.choice.retryable and app.weather is not None
        assert sum("stationinfo" in u for u, _ in http.calls) == 1  # no tight retry loop while offline
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("escape")
        # Back online: the next METAR cycle retries station discovery.
        http.overrides.clear()
        app._metar_due()
        await settle(pilot, app)
        assert app.aviation.station.icao == "KCLT" and app.aviation.metar


async def test_no_station_nearby_is_not_retried(tmp_path):
    http = FakeHttp({"api/data/stationinfo": None})
    app = make_app(tmp_path, http=http)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        assert app.aviation.station is None and not app.aviation.choice.retryable
        calls = len(http.calls)
        app._metar_due()
        await settle(pilot, app)
        assert len(http.calls) == calls


async def test_explicit_airport_from_config(tmp_path):
    cfg = Config(path=tmp_path / "config.toml", location=CLT, desktop_notify=False, bell=False, animate=False,
                 aviation_station="KCLT")
    http = FakeHttp()
    app = TermWx(cfg, http=http)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        assert app.aviation.choice.source == "explicit" and app.aviation.station.icao == "KCLT"
        assert [p for u, p in http.calls if "stationinfo" in u] == [{"ids": "KCLT", "format": "json"}]
    assert Config.load(tmp_path / "config.toml").aviation_station == "KCLT"


async def test_location_change_reresolves_station(tmp_path):
    http = FakeHttp()
    app = make_app(tmp_path, http=http)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        before = sum("stationinfo" in u for u, _ in http.calls)
        app.set_location(Location("Denver", 39.74, -104.98, "Colorado", country_code="US"))
        await settle(pilot, app)
        assert sum("stationinfo" in u for u, _ in http.calls) > before
        # The Charlotte fixture stations are all far from Denver, so nothing is picked silently.
        assert app.aviation.station is None and "no METAR station" in app.aviation.choice.error


async def test_narrow_aviation_layouts(tmp_path):
    from termwx.screens_aviation import AviationScreen

    app = make_app(tmp_path)
    async with app.run_test(size=(70, 40)) as pilot:
        await settle(pilot, app)
        assert app.query_one("#aviation").display  # summary panel stays on tiny layouts
        await pilot.press("v")
        await pilot.pause()
        assert isinstance(app.screen, AviationScreen) and app.screen.has_class("-narrow")
        assert app.screen.query_one("#av-metar").size.width >= 60  # stacked, full width
    app = make_app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        await pilot.press("v")
        await pilot.pause()
        assert not app.screen.has_class("-narrow")


# ── drone go/no-go ───────────────────────────────────────────────────────────
async def test_drone_screen_ticks_and_window(tmp_path):
    from termwx import drone as D
    from termwx.screens_drone import DroneScreen

    app = make_app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        panel = app.query_one("#drone", DataPanel)
        assert app.drone is not None and panel.data is app.drone
        assert "CAUTION" in shown(panel) and "preflight 0/10" in shown(panel)  # fixture has a Heat Advisory
        await pilot.press("g")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DroneScreen)
        assert "Daylight" in shown(screen.query_one("#drone-detail"))  # first check is highlighted
        await pilot.press("space")  # on an automatic check: nothing to tick
        assert app.drone_ticks == set()
        await pilot.press("end", "space")
        await pilot.pause()
        assert app.drone_ticks == {"pilot"} and app.drone.manual_done == 1
        assert "DONE" in shown(screen.query_one("#drone-detail"))
        await pilot.press("enter")
        assert app.drone_ticks == set()
        await pilot.press("plus")
        assert app.config.drone.window_hours == 3
        assert app.drone.window_end - app.drone.window_start == timedelta(hours=3)
        for _ in range(20):
            await pilot.press("plus")
        assert app.config.drone.window_hours == D.MAX_WINDOW_H
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DroneScreen)
        assert "preflight 0/10" in shown(panel)


async def test_drone_ticks_reset_on_location_change(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        app.toggle_drone_tick("airspace")
        assert app.drone.manual_done == 1
        app.set_location(Location("Denver", 39.74, -104.99, "Colorado", "United States", "US", "America/Denver"))
        assert app.drone_ticks == set()


async def test_drone_panel_can_be_hidden(tmp_path):
    from textual.css.query import NoMatches

    app = make_app(tmp_path)
    app.config.drone.panel = False
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        with pytest.raises(NoMatches):
            app.query_one("#drone")
        assert app.drone is not None  # still scored for the g screen


async def test_drone_with_aviation_offline_is_not_go(tmp_path):
    from termwx import drone as D

    http = FakeHttp({"aviationweather.gov": FetchError("offline")})
    app = make_app(tmp_path, http=http)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        station = next(i for i in app.drone.items if i.id == "station")
        assert station.status == D.UNKNOWN
        assert app.drone.verdict in (D.CAUTION, D.NOGO)
