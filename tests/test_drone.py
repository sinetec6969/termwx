from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import FIXTURE_NOW, load
from termwx import drone as D, units as U
from termwx.api import openmeteo
from termwx.aviation_models import (
    AviationHazard,
    AviationResult,
    AviationStation,
    CloudLayer,
    Feed,
    HazardSet,
    MetarReport,
    StationChoice,
    TafForecast,
    TafPeriod,
)
from termwx.models import Alert, AlertsResult, Current, Hour, KpPoint, Location, SpaceWeather, Weather

CLT = Location("Charlotte", 35.2271, -80.8431, "North Carolina", "United States", "US", "America/New_York")
NOW = FIXTURE_NOW  # 12:10 EDT, broad daylight


def weather(units=U.IMPERIAL, hours=None, **cur) -> Weather:
    base = dict(time=datetime(2026, 10, 7, 12, 10), temp=68, feels_like=68, humidity=50, dew_point=48, code=1,
                is_day=True, wind_speed=5, wind_dir=200, wind_gust=8, pressure=1018, cloud_cover=10, precip=0,
                visibility=30000, uv=4)
    base.update(cur)
    c = Current(**base)
    if hours is None:
        hours = [Hour(c.time.replace(minute=0) + timedelta(hours=i), c.temp, 0, c.code, 1018, wind_speed=c.wind_speed,
                      wind_gust=c.wind_gust, wind_dir=200, wind_120m=10, visibility=30000, precip=0) for i in range(6)]
    return Weather(CLT, units, c, hours, [])


def metar(**kw) -> MetarReport:
    base = dict(station="KCLT", obs_time=NOW - timedelta(minutes=20), raw="KCLT ...", temp_c=20, dewp_c=8,
                wind_dir=200, wind_speed_kt=5, visibility_sm=10, visibility_plus=True, altimeter_hpa=1018,
                clouds=[CloudLayer("FEW", 5000)])
    base.update(kw)
    return MetarReport(**base)


def aviation(m: MetarReport | None = None, distance=5.0, taf=None, hazards=None) -> AviationResult:
    st = AviationStation("KCLT", "Charlotte/Douglas Intl", 35.21, -80.94, elev_m=228, has_taf=True)
    av = AviationResult(choice=StationChoice(st, "auto", distance_nm=distance, bearing=270))
    av.metars.ok([m or metar()], False, NOW)
    av.taf.ok(taf, False, NOW)
    av.hazards.ok(HazardSet(hazards or []), False, NOW)
    return av


def space(kp=2.0) -> SpaceWeather:
    return SpaceWeather(kp=[KpPoint(NOW - timedelta(hours=1), kp, "observed")])


def run(w="default", alerts="none", av="default", sw="default", limits=None, ticks=(), now=NOW) -> D.GoNoGo:
    return D.evaluate(
        CLT,
        weather() if w == "default" else w,
        AlertsResult([]) if alerts == "none" else alerts,
        aviation() if av == "default" else av,
        space() if sw == "default" else sw,
        limits or D.DroneLimits(),
        set(ticks),
        now,
    )


def item(r: D.GoNoGo, item_id: str) -> D.CheckItem:
    return next(i for i in r.items if i.id == item_id)


# ── verdict ──────────────────────────────────────────────────────────────────
def test_good_day_is_not_ready_until_every_preflight_item_is_ticked():
    r = run()
    assert [i.label for i in r.items if i.status != D.GO] == []
    assert r.weather_verdict == D.GO and r.verdict == D.NOT_READY
    r = run(ticks=[m.id for m in D.MANUAL_ITEMS])
    assert r.manual_done == r.manual_total == len(D.MANUAL_ITEMS)
    assert r.verdict == D.GO


def test_missing_data_is_never_go():
    r = run(w=None, alerts=None, av=None, sw=None, ticks=[m.id for m in D.MANUAL_ITEMS])
    assert item(r, "wind").status == D.UNKNOWN and item(r, "station").status == D.UNKNOWN
    assert r.verdict == D.CAUTION


def test_no_go_beats_incomplete_checklist():
    r = run(w=weather(code=63, precip=0.1))
    assert r.verdict == D.NOGO and r.problems[0].id == "precip"


# ── individual checks ────────────────────────────────────────────────────────
@pytest.mark.parametrize(("vis", "status"), [(2.5, D.NOGO), (4, D.CAUTION), (5, D.GO)])
def test_visibility_floor(vis, status):
    r = run(av=aviation(metar(visibility_sm=vis, visibility_plus=False)))
    assert item(r, "vis").status == status and item(r, "vis").source == "KCLT"


def test_visibility_personal_minimum_and_tempo_group():
    r = run(limits=D.DroneLimits(min_visibility_sm=8), av=aviation(metar(visibility_sm=9, visibility_plus=False)))
    assert item(r, "vis").status == D.CAUTION
    taf = TafForecast("KCLT", NOW, NOW - timedelta(hours=2), NOW + timedelta(hours=22), "TAF ...", [
        TafPeriod(NOW - timedelta(hours=2), NOW + timedelta(hours=22), visibility_sm=6, visibility_plus=True),
        TafPeriod(NOW, NOW + timedelta(hours=3), change="TEMPO", visibility_sm=1, wx="BR"),
    ])
    vis = item(run(av=aviation(taf=taf)), "vis")
    assert vis.status == D.CAUTION and vis.source == "TAF TEMPO/PROB"  # temporary groups never make it NO-GO


@pytest.mark.parametrize(("clouds", "altitude", "status"), [
    ([CloudLayer("BKN", 800)], 400, D.NOGO),
    ([CloudLayer("FEW", 800)], 400, D.CAUTION),
    ([CloudLayer("BKN", 800)], 250, D.GO),
    ([CloudLayer("CLR")], 400, D.GO),
])
def test_cloud_clearance(clouds, altitude, status):
    r = run(av=aviation(metar(clouds=clouds)), limits=D.DroneLimits(altitude_ft=altitude))
    assert item(r, "clouds").status == status
    assert item(r, "clouds").limit == f"≥ {altitude + 500} ft AGL"


def test_wind_takes_the_worst_source():
    r = run(av=aviation(metar(wind_speed_kt=20)))  # 23 mph at the airport, 5 mph in the model
    wind = item(r, "wind")
    assert wind.status == D.NOGO and wind.source == "KCLT" and wind.value == "23 mph"


def test_metric_weather_is_converted_to_mph():
    w = weather(units=U.METRIC, temp=20, wind_speed=30, wind_gust=35)  # km/h
    r = run(w=w, av=aviation(metar(wind_speed_kt=2)))
    assert item(r, "wind").value == "19 mph" and item(r, "wind").status == D.CAUTION
    assert item(r, "temp").value == "68°F"


def test_gust_spread_and_wind_shear_are_cautions():
    r = run(av=aviation(metar(wind_speed_kt=6, wind_gust_kt=17)), limits=D.DroneLimits(max_gust_mph=40))
    assert item(r, "gust").status == D.CAUTION and "spread" in item(r, "gust").notes[0]
    taf = TafForecast("KCLT", NOW, NOW - timedelta(hours=1), NOW + timedelta(hours=23), "TAF ...", [
        TafPeriod(NOW - timedelta(hours=1), NOW + timedelta(hours=23), wind_speed_kt=5, wind_shear="WS015/27040KT"),
    ])
    assert item(run(av=aviation(taf=taf)), "gust").status == D.CAUTION


def test_wind_aloft_uses_the_120m_level():
    w = weather()
    for h in w.hours:
        h.wind_120m = 30
    assert item(run(w=w), "aloft").status == D.NOGO
    for h in w.hours:
        h.wind_120m = None
    assert item(run(w=w), "aloft").status == D.UNKNOWN


def test_precipitation():
    assert item(run(w=weather(code=63, precip=0.1)), "precip").status == D.NOGO
    assert item(run(w=weather(code=63, precip=0.1), limits=D.DroneLimits(allow_precip=True)), "precip").status == D.CAUTION
    w = weather()
    w.hours[1].precip_prob = 50
    p = item(run(w=w), "precip")
    assert p.status == D.CAUTION and p.value.startswith("50% at 1:00p")
    assert item(run(av=aviation(metar(wx="-RA"))), "precip").status == D.NOGO
    assert item(run(av=aviation(metar(wx="VCSH"))), "precip").status == D.GO  # in the vicinity, not here


def square(lat, lon, half=0.5):
    return [(lat - half, lon - half), (lat - half, lon + half), (lat + half, lon + half), (lat + half, lon - half)]


def test_thunderstorms():
    assert item(run(av=aviation(metar(wx="VCTS"))), "ts").status == D.NOGO
    over = AviationHazard("CONVECTIVE SIGMET", "CONVECTIVE SIGMET 12E", "TS", NOW, NOW + timedelta(hours=1),
                          polygon=square(CLT.lat, CLT.lon))
    assert item(run(av=aviation(hazards=[over])), "ts").status == D.NOGO
    near = AviationHazard("CONVECTIVE SIGMET", "CONVECTIVE SIGMET 13E", "TS", NOW, NOW + timedelta(hours=1),
                          polygon=square(CLT.lat + 1.0, CLT.lon))  # southern edge ~30 NM north
    ts = item(run(av=aviation(hazards=[near])), "ts")
    assert ts.status == D.CAUTION and "NM away" in ts.notes[0]
    expired = AviationHazard("CONVECTIVE SIGMET", "CONVECTIVE SIGMET 11E", "TS", NOW - timedelta(hours=2),
                             NOW - timedelta(minutes=1), polygon=square(CLT.lat, CLT.lon))
    assert item(run(av=aviation(hazards=[expired])), "ts").status == D.GO


def test_icing():
    assert item(run(av=aviation(metar(wx="-FZRA", temp_c=-1))), "ice").status == D.NOGO
    assert item(run(w=weather(temp=34, code=45)), "ice").status == D.CAUTION  # fog near freezing
    assert item(run(w=weather(temp=34, code=0), av=aviation(metar(temp_c=1, dewp_c=-10))), "ice").status == D.GO


@pytest.mark.parametrize(("temp", "status"), [(28, D.NOGO), (40, D.CAUTION), (68, D.GO), (101, D.CAUTION), (106, D.NOGO)])
def test_temperature(temp, status):
    w = weather(temp=temp)
    for h in w.hours:
        h.temp = temp
    r = run(w=w, av=aviation(metar(temp_c=(temp - 32) * 5 / 9)))
    assert item(r, "temp").status == status


def test_daylight_and_lighting():
    night = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)  # 11 pm EDT
    assert item(run(now=night), "daylight").status == D.NOGO
    assert item(run(now=night, limits=D.DroneLimits(anticollision_lighting=True)), "daylight").status == D.NOGO
    ok = D.DroneLimits(anticollision_lighting=True, night_qualified=True)
    assert item(run(now=night, limits=ok), "daylight").status == D.GO
    _, dusk = D.civil_twilight(CLT.lat, CLT.lon, date(2026, 10, 7))
    late = item(run(now=dusk - timedelta(hours=1)), "daylight")
    assert late.status == D.CAUTION and "land by then" in late.notes[-1]


@pytest.mark.parametrize(("kp", "status"), [(3.7, D.GO), (5.0, D.CAUTION), (6.3, D.NOGO)])
def test_kp(kp, status):
    assert item(run(sw=space(kp)), "kp").status == status


def test_kp_forecast_inside_the_window_counts():
    sw = SpaceWeather(kp=[KpPoint(NOW - timedelta(hours=1), 2.0, "observed"),
                          KpPoint(NOW + timedelta(hours=1), 5.7, "predicted"),
                          KpPoint(NOW + timedelta(hours=10), 8.0, "predicted")])
    kp = item(run(sw=sw), "kp")
    assert kp.status == D.CAUTION and kp.value == "5.7 forecast"


def test_alerts():
    severe = Alert("1", "Severe Thunderstorm Warning", "Severe", "SVR until 3 pm")
    heat = Alert("2", "Heat Advisory", "Moderate", "Heat until 7 pm")
    assert item(run(alerts=AlertsResult([heat])), "alerts").status == D.CAUTION
    a = item(run(alerts=AlertsResult([severe, heat])), "alerts")
    assert a.status == D.NOGO and a.value == "Severe Thunderstorm Warning +1"
    assert item(run(alerts=None), "alerts").status == D.UNKNOWN


def test_low_level_advisories_only_over_the_location():
    ifr = AviationHazard("G-AIRMET", "G-AIRMET SIERRA", "IFR", NOW, NOW + timedelta(hours=3), base_ft=0,
                         polygon=square(CLT.lat, CLT.lon))
    high_turb = AviationHazard("G-AIRMET", "G-AIRMET TANGO", "TURB", NOW, NOW + timedelta(hours=3), base_ft=18000,
                               polygon=square(CLT.lat, CLT.lon))
    adv = item(run(av=aviation(hazards=[ifr, high_turb])), "advisories")
    assert adv.status == D.CAUTION and adv.value == "IFR"
    elsewhere = AviationHazard("G-AIRMET", "G-AIRMET SIERRA", "IFR", NOW, NOW + timedelta(hours=3),
                               polygon=square(CLT.lat + 3, CLT.lon))
    assert item(run(av=aviation(hazards=[elsewhere])), "advisories").status == D.GO


def test_station_distance_and_age():
    assert item(run(av=aviation(distance=28)), "station").status == D.CAUTION
    old = metar(obs_time=NOW - timedelta(hours=2))
    assert item(run(av=aviation(old)), "station").status == D.CAUTION
    av = aviation()
    av.metars.stale = True
    assert item(run(av=av), "station").status == D.CAUTION


def test_density_altitude():
    assert item(run(), "da").status == D.GO
    hot_high = aviation(metar(temp_c=35, altimeter_hpa=1005))
    hot_high.station.elev_m = 1600
    assert item(run(av=hot_high), "da").status == D.CAUTION


# ── sun ──────────────────────────────────────────────────────────────────────
def test_sunrise_matches_open_meteo():
    w = openmeteo.parse_forecast(load("openmeteo.json"), CLT, U.IMPERIAL)
    day = w.days[0]
    rise, sett = D.sun_crossings(CLT.lat, CLT.lon, day.date, D.SUN_ALT_SET)
    tz = ZoneInfo("America/New_York")
    assert abs((rise.astimezone(tz).replace(tzinfo=None) - day.sunrise).total_seconds()) < 120
    assert abs((sett.astimezone(tz).replace(tzinfo=None) - day.sunset).total_seconds()) < 120
    dawn, dusk = D.civil_twilight(CLT.lat, CLT.lon, day.date)
    assert timedelta(minutes=20) < rise - dawn < timedelta(minutes=35)
    assert timedelta(minutes=20) < dusk - sett < timedelta(minutes=35)


def test_sun_phase():
    assert D.sun_phase(CLT.lat, CLT.lon, NOW) == "day"
    assert D.sun_phase(CLT.lat, CLT.lon, datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)) == "night"
    _, sett = D.sun_crossings(CLT.lat, CLT.lon, date(2026, 10, 7), D.SUN_ALT_SET)
    assert D.sun_phase(CLT.lat, CLT.lon, sett + timedelta(minutes=10)) == "twilight"


# ── limits ───────────────────────────────────────────────────────────────────
def test_limits_from_dict_validates_and_clamps():
    lim = D.DroneLimits.from_dict({
        "max_wind_mph": 15, "panel": "yes", "min_visibility_sm": 1, "altitude_ft": 600, "window_hours": 0,
        "night_qualified": True, "max_kp": "high",
    })
    assert lim.max_wind_mph == 15.0 and isinstance(lim.max_wind_mph, float)
    assert lim.panel is True  # wrong type keeps the default
    assert lim.min_visibility_sm == D.MIN_VIS_SM and lim.altitude_ft == D.MAX_ALTITUDE_FT and lim.window_hours == 1
    assert lim.night_qualified is True and lim.max_kp == 5.0

