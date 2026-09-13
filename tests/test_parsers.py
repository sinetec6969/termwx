from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from rich.console import Console

from conftest import FakeHttp, load
from termwx import moon, render as R, units as U, wmo
from termwx.api import geo, nws, openmeteo, swpc
from termwx.api.http import FetchError, Http
from termwx.config import Config
from termwx.models import Location, SpaceWeather, SwpcMessage
from termwx.scene import SceneState, render_scene

CLT = Location("Charlotte", 35.2271, -80.8431, "North Carolina", "United States", "US", "America/New_York")


# ── open-meteo ───────────────────────────────────────────────────────────────
def test_parse_forecast():
    w = openmeteo.parse_forecast(load("openmeteo.json"), CLT, U.IMPERIAL, load("aqi.json"))
    assert w.current.temp == pytest.approx(78.3)
    assert w.current.code == 2 and w.current.is_day
    assert w.aqi == 25
    assert len(w.days) == 4
    assert w.days[1].date.isoformat() == "2026-09-14"
    # hourly starts at the current hour
    assert w.hours[0].time == w.current.time.replace(minute=0)
    assert all(b.time > a.time for a, b in zip(w.hours, w.hours[1:]))
    assert w.today.sunrise.hour == 7


def test_forecast_params_units():
    p = openmeteo.forecast_params(CLT, U.METRIC)
    assert p["temperature_unit"] == "celsius" and p["wind_speed_unit"] == "kmh"
    assert "weather_code" in p["current"]


# ── nws ──────────────────────────────────────────────────────────────────────
def test_parse_nws_alerts_sorted():
    alerts = nws.parse_alerts(load("nws_alerts.json"))
    assert len(alerts) == 7
    assert {a.event for a in alerts} == {"Heat Advisory", "Special Weather Statement"}
    assert all(a.headline and a.onset for a in alerts)
    assert alerts == nws.sort_alerts(alerts)


def test_sort_alerts_by_severity():
    base = nws.parse_alerts(load("nws_alerts.json"))[0]
    from dataclasses import replace

    minor = replace(base, id="a", severity="Minor")
    extreme = replace(base, id="b", severity="Extreme")
    assert [a.id for a in nws.sort_alerts([minor, extreme])] == ["b", "a"]


async def test_nws_non_us_skips_request():
    http = FakeHttp()
    res = await nws.fetch_alerts(http, Location("Tokyo", 35.68, 139.69, country_code="JP"))
    assert not res.supported and http.calls == []


async def test_nws_400_means_unsupported():
    http = FakeHttp({"api.weather.gov": FetchError("HTTP 400", 400)})
    res = await nws.fetch_alerts(http, Location("x", 35.68, 139.69))
    assert not res.supported


async def test_nws_outage_is_error_not_unsupported():
    http = FakeHttp({"api.weather.gov": FetchError("timeout")})
    res = await nws.fetch_alerts(http, CLT)
    assert res.supported and res.error


# ── swpc ─────────────────────────────────────────────────────────────────────
async def test_fetch_space_from_fixtures():
    sw = await swpc.fetch_space(FakeHttp(), CLT.lat, CLT.lon)
    assert sw.errors == []
    assert (sw.g_now, sw.s_now, sw.r_now) == (0, 0, 0)
    assert len(sw.outlook) == 3 and sw.outlook[0].r_minor_prob == 10
    assert len(sw.kp) == 81 and {p.kind for p in sw.kp} == {"observed", "estimated", "predicted"}
    assert sw.kp == sorted(sw.kp, key=lambda p: p.time)
    assert sw.wind_speed and 200 < sw.wind_speed < 1500
    mag = load("mag.json")[0]
    assert (sw.bt, sw.bz) == (mag["bt"], mag["bz_gsm"])
    assert sw.bz_history and sw.speed_history
    assert sw.flare_current == "B3.4" and sw.flare_max == "B5.5"
    assert sw.f107 == 109
    assert sw.messages and sw.messages == sorted(sw.messages, key=lambda m: m.issued, reverse=True)
    assert sw.aurora_overhead == 0


async def test_fetch_space_partial_failure():
    sw = await swpc.fetch_space(FakeHttp({"rtsw_wind": FetchError("down")}), 0, 0)
    assert sw.wind_speed is None and any("wind" in e for e in sw.errors)
    assert sw.kp


def test_parse_kp_legacy_array_format():
    sw = SpaceWeather()
    swpc.parse_kp([["time_tag", "kp", "observed", "noaa_scale"], ["2026-09-13 00:00:00", "5.33", "observed", "G1"]], sw)
    assert sw.kp[0].kp == pytest.approx(5.33)


def test_parse_message_scale_and_title():
    msgs = swpc.parse_messages(
        [
            {
                "product_id": "K07W",
                "issue_datetime": "2026-09-13 12:00:00.000",
                "message": "Space Weather Message Code: WARK07\r\nSerial Number: 1\r\n\r\n"
                "WARNING: Geomagnetic K-index of 7 expected\r\nNOAA Scale: G3 - Strong\r\n",
            }
        ]
    )
    m = msgs[0]
    assert m.code == "WARK07" and m.scale == "G3" and m.level == 3
    assert m.title == "Warning: Geomagnetic K-index of 7 expected"


def test_recent_major_messages_filters():
    now = datetime.now(timezone.utc)
    sw = SpaceWeather(
        messages=[
            SwpcMessage("a", now, "WARK07", "Warning: K7", "", "G3"),
            SwpcMessage("b", now, "ALTK05", "Alert: K5", "", "G1"),
            SwpcMessage("c", now - timedelta(days=3), "WARK08", "Warning: K8", "", "G4"),
            SwpcMessage("d", now, "SUMX01", "Summary: X-ray", "", "R3"),
        ]
    )
    assert [m.product_id for m in swpc.recent_major_messages(sw)] == ["a"]


def test_aurora_lookup():
    grid = {"Forecast Time": "2026-09-13T15:43:00Z", "coordinates": [[0, 60, 5], [0, 64, 40], [350, 60, 12], [10, 10, 99]]}
    overhead, nearby, when = swpc.aurora_lookup(grid, 60.2, -0.4)
    assert overhead == 5 and nearby == 40 and when.hour == 15
    # southern hemisphere looks toward the south pole, not north
    overhead, nearby, _ = swpc.aurora_lookup({"coordinates": [[0, -60, 7], [0, -64, 30], [0, -56, 90]]}, -60, 0)
    assert (overhead, nearby) == (7, 30)


# ── geo ──────────────────────────────────────────────────────────────────────
def test_parse_coords():
    loc = geo.parse_coords(" 35.2, -80.84 ")
    assert loc and loc.lat == 35.2 and loc.lon == -80.84
    assert geo.parse_coords("91,0") is None
    assert geo.parse_coords("Denver") is None


def test_qualifier_matching():
    nc = Location("Charlotte", 0, 0, "North Carolina", "United States", "US")
    fr = Location("Charlotte", 0, 0, "Provence-Alpes-Côte d'Azur", "France", "FR")
    assert geo._matches_qualifier(nc, "NC")
    assert not geo._matches_qualifier(fr, "NC")  # "nc" is inside "Provence"
    assert geo._matches_qualifier(fr, "France")


async def test_search_uses_name_part_and_filters():
    http = FakeHttp()
    results = await geo.search(http, "Denver, CO")
    assert http.calls[0][1]["name"] == "Denver"
    assert results and all(r.region == "Colorado" for r in results)
    assert results[0].label == "Denver, CO"


# ── http cache ───────────────────────────────────────────────────────────────
async def test_http_falls_back_to_cache(tmp_path):
    ok = True

    def handler(request):
        if ok:
            return httpx.Response(200, json={"v": 1})
        raise httpx.ConnectError("offline")

    http = Http(cache_dir=tmp_path)
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await http.get_json("https://x.test/a") == ({"v": 1}, False)
    ok = False
    assert await http.get_json("https://x.test/a") == ({"v": 1}, True)
    with pytest.raises(FetchError):
        await http.get_json("https://x.test/never-cached")
    await http.aclose()


async def test_http_4xx_raises_with_status(tmp_path):
    http = Http(cache_dir=tmp_path)
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(FetchError) as e:
        await http.get_json("https://x.test/a")
    assert e.value.status == 404
    await http.aclose()


# ── units, wmo, moon ─────────────────────────────────────────────────────────
def test_units():
    assert U.compass(0) == "N" and U.compass(225) == "SW" and U.compass(359) == "N"
    assert U.wind_arrow(270) == "→"  # wind from the west blows east
    assert U.pressure(1013.25, U.IMPERIAL) == "29.92 inHg"
    assert U.temp(71.6, U.IMPERIAL, suffix=True) == "72°F"
    assert U.visibility(50000, U.IMPERIAL) == "10+ mi"
    assert U.temp_color(-40, U.IMPERIAL).startswith("#") and U.temp_color(130, U.IMPERIAL).startswith("#")
    assert U.temp_color(0, U.METRIC) == U.temp_color(32, U.IMPERIAL)


def test_wmo():
    assert wmo.condition(95).scene == "thunder"
    assert wmo.condition(12345) is wmo.UNKNOWN
    assert all(len(line) == 5 for line in [wmo.icon(c) for c in wmo.CODES])


def test_moon_known_dates():
    # 2026-08-12 total solar eclipse (new), 2026-08-28 partial lunar eclipse (full)
    assert moon.phase(datetime(2026, 8, 12, 17, 46, tzinfo=timezone.utc))[0] == "New Moon"
    name, _, illum = moon.phase(datetime(2026, 8, 28, 4, 13, tzinfo=timezone.utc))
    assert name == "Full Moon" and illum > 0.99
    assert moon.phase(datetime(2026, 8, 20, tzinfo=timezone.utc))[0] == "First Quarter"


# ── config ───────────────────────────────────────────────────────────────────
def test_config_roundtrip(tmp_path):
    path = tmp_path / "termwx" / "config.toml"
    cfg = Config(path=path, units=U.METRIC, desktop_notify=False)
    cfg.location = Location('Coeur d"Alene \\ test', 47.67, -116.78, "Idaho", "United States", "US", "America/Los_Angeles")
    cfg.toggle_favorite(CLT)
    cfg.save()
    back = Config.load(path)
    assert back.units == U.METRIC and back.desktop_notify is False
    assert back.location == cfg.location
    assert back.favorites == [CLT]
    assert back.toggle_favorite(CLT) is False and back.favorites == []


def test_config_missing_or_corrupt(tmp_path):
    assert Config.load(tmp_path / "nope.toml").location is None
    bad = tmp_path / "bad.toml"
    bad.write_text("units = [[[")
    assert Config.load(bad).units == U.IMPERIAL


# ── rendering ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scene", ["clear", "partly", "cloudy", "fog", "drizzle", "rain", "freezing", "snow", "thunder"])
@pytest.mark.parametrize("is_day", [True, False])
def test_scene_dimensions(scene, is_day):
    for tick in (0, 1, 44, 1000):
        text = render_scene(SceneState(scene, 3, is_day, 40, 90, 80), 30, 9, tick)
        lines = text.plain.split("\n")
        assert len(lines) == 9 and all(len(line) == 30 for line in lines)


@pytest.mark.parametrize("width", [30, 50, 80, 140])
async def test_renderers_at_widths(width):
    w = openmeteo.parse_forecast(load("openmeteo.json"), CLT, U.IMPERIAL, load("aqi.json"))
    sw = await swpc.fetch_space(FakeHttp(), CLT.lat, CLT.lon)
    res = await nws.fetch_alerts(FakeHttp(), CLT)
    console = Console(width=width, record=True, color_system=None)
    for r in (
        R.current_panel(w, width),
        R.hourly_panel(w, width),
        R.forecast_panel(w, width),
        R.space_panel(sw, width),
        R.alerts_summary(res),
        R.banner_text(res.alerts),
    ):
        console.print(r)
    out = console.export_text()
    assert "Partly Cloudy" in out and "Heat Advisory" in out and "NOAA" in out
