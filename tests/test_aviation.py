from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from rich.console import Console

from conftest import FIXTURE_NOW, FakeHttp, FetchError, load, load_text
from termwx import aviation as A, render_aviation as RA
from termwx.api import awc, faa_notam, faa_tfr, nws
from termwx.api.http import Http
from termwx.aviation_models import (
    AviationHazard,
    AviationResult,
    AviationStation,
    CloudLayer,
    Feed,
    HazardSet,
    IcingTurbulence,
    Pirep,
    Runway,
    StationChoice,
)
from termwx.config import Config

CLT_LAT, CLT_LON = 35.2271, -80.8431
KCLT = (35.22254, -80.95432)
NOW = FIXTURE_NOW


def text(renderable, width: int = 120) -> str:
    console = Console(width=width, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


# ── METAR ────────────────────────────────────────────────────────────────────
def test_metar_normalization():
    ms = awc.parse_metars(load("awc_metar.json"))
    assert len(ms) == 6
    assert ms == sorted(ms, key=lambda m: m.obs_time, reverse=True)
    m = ms[0]
    assert m.station == "KCLT" and m.report_type == "METAR" and not m.is_speci
    assert m.obs_time == datetime(2026, 10, 7, 15, 52, tzinfo=timezone.utc)
    assert m.raw.startswith("METAR KCLT 071552Z 00000KT 10SM CLR")
    assert (m.temp_c, m.dewp_c) == (21.1, 11.7)
    assert (m.wind_dir, m.wind_speed_kt, m.wind_gust_kt) == (0, 0, None)
    assert m.visibility_sm == 10 and m.visibility_plus
    assert m.altimeter_inhg == pytest.approx(30.08, abs=0.01)
    assert m.ceiling_ft is None and m.flight_category == "VFR"
    assert ms[1].clouds == [CloudLayer("FEW", 25000)] and ms[1].pressure_tendency_hpa == 0.7


def test_speci_and_ceiling():
    ms = awc.parse_metars(load("awc_metar_speci.json"))
    speci = [m for m in ms if m.is_speci]
    assert speci and all(m.raw.startswith("SPECI") for m in speci)
    tpa = next(m for m in speci if m.station == "KTPA" and m.obs_time.hour == 15)
    assert tpa.ceiling_ft == 700 and A.metar_category(tpa) == "IFR"
    # A SPECI and a routine METAR at different times are both kept, newest first.
    assert ms[0].obs_time >= ms[-1].obs_time


def test_metar_field_variants():
    m = awc.parse_metar({"icaoId": "KXXX", "obsTime": 1791388320, "rawOb": "SPECI KXXX ...", "wdir": "VRB",
                         "wspd": 4, "visib": "1 1/2", "vertVis": 300, "clouds": None})
    assert m.report_type == "SPECI" and m.wind_variable and m.wind_dir is None
    assert m.visibility_sm == 1.5 and m.ceiling_ft == 300
    assert A.metar_category(m) == "LIFR"  # no fltCat supplied, so derived
    assert awc.parse_metar({"icaoId": "K", "rawOb": ""}) is None


@pytest.mark.parametrize(
    "ceiling,vis,cat",
    [(None, 10, "VFR"), (3100, 6, "VFR"), (3000, 10, "MVFR"), (5000, 5, "MVFR"), (900, 10, "IFR"),
     (5000, 2.5, "IFR"), (400, 10, "LIFR"), (2000, 0.5, "LIFR"), (None, None, "")],
)
def test_flight_categories(ceiling, vis, cat):
    assert A.flight_category(ceiling, vis) == cat


def test_category_badges_render():
    for cat in ("VFR", "MVFR", "IFR", "LIFR"):
        badge = RA.cat_badge(cat)
        assert badge.plain.strip() == cat and RA.CATEGORY_COLORS[cat] in str(badge.style)
    assert RA.cat_badge("").plain.strip() == "----"


def test_formatters():
    assert RA.fmt_vis(0.5) == "1/2SM" and RA.fmt_vis(1.25) == "1 1/4SM" and RA.fmt_vis(10, True) == "10SM"
    assert RA.fmt_vis(6, True) == "P6SM" and RA.fmt_vis(3) == "3SM"
    assert RA.wind_code(220, False, 8, 14) == "22008G14KT" and RA.wind_code(None, True, 3, None) == "VRB03KT"
    assert RA.fmt_wind(220, False, 8, 14) == "220° 8G14kt" and RA.fmt_wind(0, False, 0, None) == "calm"
    assert RA.fmt_age(NOW - timedelta(minutes=72), NOW) == "1h12m"
    assert RA.fmt_alt(24000) == "FL240" and RA.fmt_alt(8000) == "080" and RA.fmt_alt(0) == "SFC"


# ── TAF ──────────────────────────────────────────────────────────────────────
def test_taf_normalization():
    t = awc.parse_taf(load("awc_taf.json"), "KCLT")
    assert t.station == "KCLT" and t.amended
    assert t.issue_time == datetime(2026, 10, 7, 15, 52, tzinfo=timezone.utc)
    assert (t.valid_from.hour, t.valid_to.day, t.valid_to.hour) == (16, 8, 18)
    assert t.raw.startswith("TAF KCLT 071552Z 0716/0818")
    (p,) = t.periods
    assert p.change == "BASE" and p.wind_dir == 220 and p.wind_speed_kt == 3
    assert p.visibility_sm == 6 and p.visibility_plus and A.period_category(p) == "VFR"


def test_taf_groups_and_ordering():
    t = awc.parse_taf(load("awc_taf_multi.json"), "KTPA")
    assert [p.label or "BASE" for p in t.periods] == ["BASE", "PROB30", "FM", "TEMPO", "FM"]
    assert all(a.start <= b.start for a, b in zip(t.periods, t.periods[1:]))
    rows = A.taf_rows(t)
    cats = [A.period_category(eff) for _, eff in rows]
    assert cats == ["VFR", "IFR", "VFR", "MVFR", "MVFR"]
    prob = rows[1][0]
    assert prob.probability == 30 and prob.wx == "+TSRA" and prob.wind_gust_kt == 25
    # TEMPO gives only vis/wx/clouds; wind is carried from the prevailing FM group.
    tempo, eff = rows[3]
    assert tempo.wind_speed_kt is None and eff.wind_dir == 240 and eff.visibility_sm == 3


def test_taf_outlook_and_summary():
    t = awc.parse_taf(load("awc_taf_multi.json"), "KTPA")
    cur, changes, temps = A.taf_outlook(t, NOW)
    assert cur == "VFR"
    assert changes == [("MVFR", datetime(2026, 10, 8, 0, tzinfo=timezone.utc))]
    assert {cat for _, cat in temps} == {"IFR", "MVFR"}
    s = RA.taf_summary(t, NOW).plain
    assert "VFR → MVFR 00Z" in s and "PROB30 IFR 16Z–20Z" in s
    assert RA.taf_summary(t, NOW + timedelta(days=3)).plain == "TAF expired"


def test_taf_becmg_merges_into_prevailing():
    base = {"timeFrom": 1791388800, "timeTo": 1791482400, "wdir": 180, "wspd": 10, "visib": "6+",
            "clouds": [{"cover": "BKN", "base": 4000}]}
    becmg = {"timeFrom": 1791400000, "timeTo": 1791482400, "timeBec": 1791407200, "fcstChange": "BECMG",
             "clouds": [{"cover": "OVC", "base": 800}]}
    t = awc.parse_taf([{"icaoId": "KX", "issueTime": "2026-10-07T15:52:00Z", "validTimeFrom": 1791388800,
                        "validTimeTo": 1791482400, "rawTAF": "TAF KX", "fcsts": [becmg, base]}])
    rows = A.taf_rows(t)
    assert [p.change for p, _ in rows] == ["BASE", "BECMG"]
    eff = rows[1][1]
    assert eff.wind_speed_kt == 10 and eff.ceiling_ft == 800 and A.period_category(eff) == "IFR"
    assert rows[1][0].becmg_end is not None


def test_taf_panel_renders_timeline_and_raw():
    t = awc.parse_taf(load("awc_taf_multi.json"), "KTPA")
    out = text(RA.taf_panel(Feed(t, NOW, loaded=True), True, NOW), 140)
    assert "PROB30 -20Z" in out and "TEMPO -00Z" in out and "FM" in out
    assert "▼" in out  # VFR → MVFR at 00Z is a degradation
    assert "Raw TAF" in out and "\n     FM072000" in out
    assert "OVC080 " in out  # long cloud groups never run into the weather column


def test_raw_taf_line_breaks():
    raw = "TAF KX 071540Z 0716/0818 14007KT 6SM PROB30 TEMPO 0716/0720 2SM FM072000 24006KT BECMG 0800/0802 OVC010"
    lines = RA.format_raw_taf(raw).split("\n")
    assert lines[1].strip().startswith("PROB30 TEMPO") and lines[2].strip().startswith("FM072000")
    assert lines[3].strip().startswith("BECMG")


# ── HTTP 204 / stations ──────────────────────────────────────────────────────
async def test_awc_204_is_empty_not_failure():
    http = FakeHttp({"api/data/metar": None, "api/data/taf": None, "api/data/stationinfo": None})
    assert await awc.fetch_metars(http, "KZZZ") == ([], False)
    assert await awc.fetch_taf(http, "KZZZ") == (None, False)
    choice = await awc.choose_station(http, 30.5, -39.5)
    assert choice.station is None and "no METAR station" in choice.error


async def test_awc_204_through_real_http_client(tmp_path):
    http = Http(cache_dir=tmp_path)
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    assert await awc.fetch_metars(http, "KZZZ") == ([], False)
    assert await http.get_text_cached("https://x.test/windtemp") == ("", False)
    await http.aclose()


async def test_nearest_station_resolution():
    http = FakeHttp()
    choice = await awc.choose_station(http, CLT_LAT, CLT_LON)
    st = choice.station
    assert choice.source == "auto" and st.icao == "KCLT" and st.has_taf
    assert choice.distance_nm == pytest.approx(5.5, abs=0.3) and RA.U.compass(choice.bearing) == "W"
    # Airport details (FAA) add elevation, variation and runways.
    assert st.magdec == -7 and round(st.elev_ft) == 748
    assert [r.ident for r in st.runways] == ["18C/36C", "18L/36R", "18R/36L"]
    stationinfo = [p for u, p in http.calls if "stationinfo" in u]
    assert len(stationinfo) == 1 and "bbox" in stationinfo[0] and stationinfo[0]["format"] == "json"


def test_pick_station_prefers_taf_site_and_respects_limit():
    near_metar = AviationStation("KAAA", "A", 35.25, -80.85, has_taf=False)
    taf_site = AviationStation("KBBB", "B", 35.30, -80.95, has_taf=True)
    far = AviationStation("KCCC", "C", 40.0, -80.85, has_taf=True)
    no_metar = AviationStation("KDDD", "D", 35.2271, -80.8431, has_metar=False)
    st, d = awc.pick_station([near_metar, taf_site, far, no_metar], CLT_LAT, CLT_LON)
    assert st.icao == "KBBB" and d < 8
    assert awc.pick_station([far], CLT_LAT, CLT_LON) is None  # 285 NM is never "nearby"


async def test_station_override_and_bad_override():
    http = FakeHttp()
    choice = await awc.choose_station(http, 39.74, -104.98, explicit="kclt")
    assert choice.source == "explicit" and choice.station.icao == "KCLT"
    assert choice.distance_nm > 1000  # shown, with a warning colour, never hidden
    assert "your choice" in RA.station_header(AviationResult(choice=choice), "imperial", NOW).plain
    assert (await awc.choose_station(http, 0, 0, explicit="not an id!")).station is None
    missing = await awc.choose_station(FakeHttp({"stationinfo": None}), 0, 0, explicit="KZZZ")
    assert missing.station is None and "KZZZ not found" in missing.error


async def test_station_discovery_is_not_repeated_on_refresh(tmp_path):
    from test_app import make_app, settle

    http = FakeHttp()
    app = make_app(tmp_path, http=http)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        assert app.aviation.station.icao == "KCLT"
        app.refresh_aviation()
        app.refresh_aviation()
        await settle(pilot, app)
    assert sum("stationinfo" in u for u, _ in http.calls) == 1
    assert sum("api/data/airport" in u for u, _ in http.calls) == 1


# ── PIREPs ───────────────────────────────────────────────────────────────────
def test_pirep_parsing():
    ps = awc.parse_pireps(load("awc_pirep.json"), KCLT)
    assert len(ps) == 5
    ovc = next(p for p in ps if "OVC023" in p.raw)
    assert ovc.aircraft == "CRJ7" and ovc.altitude_ft == 2300 and ovc.wx == "-BR"
    assert ovc.clouds == [CloudLayer("OVC", 2300, 2900)]
    assert ovc.distance_nm == pytest.approx(80, abs=2) and RA.U.compass(ovc.bearing) == "W"
    airep = next(p for p in ps if p.report_type == "AIREP")
    assert airep.altitude_ft == 36000 and airep.wind_speed_kt == 86 and airep.turbulence[0].intensity == "NEG"
    mod = awc.parse_pireps(load("awc_pirep_mod.json"))
    ice = next(p for p in mod if p.icing and p.icing[0].intensity == "MOD")
    assert ice.icing[0].type == "RIME" and (ice.icing[0].base_ft, ice.icing[0].top_ft) == (19000, 21000)


def test_intensity_levels():
    assert [A.intensity_level(x) for x in ("NEG", "LGT", "LGT-MOD", "MOD", "MOD-SEV", "SEV-EXTM", "")] == [0, 1, 2, 2, 3, 4, -1]


def test_pirep_sorting():
    def p(minutes, dist, turb="", ice="", wx="", raw="X UA", clouds=()):
        return Pirep(
            obs_time=NOW - timedelta(minutes=minutes), raw=raw, distance_nm=dist, wx=wx, clouds=list(clouds),
            turbulence=[IcingTurbulence("TURB", turb)] if turb else [], icing=[IcingTurbulence("ICE", ice)] if ice else [],
        )

    routine_close = p(5, 3)
    low_cloud = p(30, 60, clouds=[CloudLayer("OVC", 800)])
    mod_turb = p(90, 80, turb="MOD")
    severe_ice = p(150, 95, ice="SEV")
    urgent = p(20, 40, raw="X UUA /OV ...")
    too_old = p(200, 5, turb="SEV")
    too_far = p(10, 140, turb="SEV")
    out = A.recent_pireps([routine_close, mod_turb, severe_ice, low_cloud, urgent, too_old, too_far], NOW)
    assert out == [urgent, severe_ice, mod_turb, low_cloud, routine_close]
    # Within a significance level, closer reports come first, then newer.
    near_new, near_old, far = p(10, 10, turb="MOD"), p(100, 12, turb="MOD"), p(1, 90, turb="MOD")
    assert A.sort_pireps([far, near_old, near_new], NOW) == [near_new, near_old, far]


# ── SIGMET / G-AIRMET / CWA ──────────────────────────────────────────────────
def test_sigmet_parsing():
    hs = awc.parse_airsigmets(load("awc_airsigmet.json"))
    assert len(hs) == 5 and all(h.kind == "CONVECTIVE SIGMET" and h.hazard == "TS" for h in hs)
    h = next(h for h in hs if h.name == "CONVECTIVE SIGMET 45E")
    assert h.top_ft == 45000 and h.valid_to == datetime(2026, 10, 7, 17, 55, tzinfo=timezone.utc)
    assert h.polygon[0] == (29.073, -84.452) and h.movement == "MOV FROM 270° 15KT"
    assert "TOPS ABV FL450" in h.raw
    nc = awc.parse_airsigmets([{"hazard": "TURB", "seriesId": "N3", "rawAirSigmet": "WSUS01 KKCI\nSIGMET NOVEMBER 3 VALID UNTIL 2100Z",
                                "altitudeLow1": 18000, "altitudeHi1": 34000, "coords": []}])[0]
    assert nc.name == "SIGMET NOVEMBER 3" and nc.kind == "SIGMET" and (nc.base_ft, nc.top_ft) == (18000, 34000)
    intl = awc.parse_isigmets(load("awc_isigmet.json"))
    assert any(h.hazard == "TC" for h in intl) and all(h.kind == "INTL SIGMET" for h in intl)


def test_gairmet_parsing():
    gs = awc.parse_gairmets(load("awc_gairmet.json"))
    assert len(gs) == 18 and {g.name for g in gs} == {"G-AIRMET SIERRA", "G-AIRMET TANGO", "G-AIRMET ZULU"}
    ice = next(g for g in gs if g.hazard == "ICE" and g.base_text)
    assert ice.base_text == "FZL 060-090" and ice.top_ft == 18000 and ice.severity == "MOD"
    turb = next(g for g in gs if g.hazard == "TURB" and g.top_ft == 18000)
    assert turb.base_ft == 6000 and not turb.is_line
    fzl = next(g for g in gs if g.hazard == "FZLVL")
    assert fzl.is_line and fzl.base_ft is not None
    ifr = next(g for g in gs if g.hazard == "IFR")
    assert "CIG BLW 010" in ifr.qualifier and "Graphical product" in ifr.raw
    assert ifr.valid_to == datetime(2026, 10, 7, 21, tzinfo=timezone.utc)
    cwa = awc.parse_cwas(load("awc_cwa.json"))[0]
    assert cwa.name == "CWA ZMA 104" and cwa.hazard == "TS" and len(cwa.polygon) == 4 and "MOV FROM 16005KT" in cwa.movement


def test_point_in_polygon_and_distance():
    square = [(35, -81), (35, -80), (36, -80), (36, -81), (35, -81)]
    assert A.point_in_polygon(35.5, -80.5, square) and not A.point_in_polygon(34.5, -80.5, square)
    assert A.distance_to_shape_nm(35.5, -80.5, square) == 0
    assert A.distance_to_shape_nm(34.5, -80.5, square) == pytest.approx(30, abs=0.5)
    # A line is never "inside"; distance is to the line itself.
    assert A.distance_to_shape_nm(35.5, -80.5, [(35.5, -82), (35.5, -79)], closed=False) == pytest.approx(0, abs=0.01)
    # Shapes crossing the antimeridian (AWC Alaska polygons use longitudes below -180).
    aleutian = [(53.1, -178), (50.4, -176), (50.4, -182), (52.1, -190.7), (54.3, -189.5), (53.2, -182), (53.1, -178)]
    assert A.point_in_polygon(52.0, 179.0, aleutian) and A.point_in_polygon(52.0, -181.0, aleutian)


def test_hazard_applicability():
    gs = awc.parse_gairmets(load("awc_gairmet.json"))
    turb = next(g for g in gs if g.hazard == "TURB" and g.top_ft == 18000)
    lat = sum(p[0] for p in turb.polygon) / len(turb.polygon)
    lon = sum(p[1] for p in turb.polygon) / len(turb.polygon)
    A.locate_hazards(gs, lat, lon)
    rel = A.relevant_hazards(gs, NOW)
    assert turb in rel and turb.contains_point and turb.distance_nm == 0
    assert all(not g.is_line or not g.contains_point for g in gs)
    assert rel[0].contains_point  # covering advisories first
    # Expired advisories are dropped, even when they come from a cache.
    assert A.relevant_hazards(gs, NOW + timedelta(hours=6)) == []
    # Around Charlotte nothing is within 50 NM in this sample.
    A.locate_hazards(gs, *KCLT)
    assert A.relevant_hazards(gs, NOW) == []
    card = RA.hazard_card(replace(turb, contains_point=True), NOW).plain
    assert "G-AIRMET TANGO · TURB · snapshot 1500Z · 4h50m left" in card and "060-FL180" in card
    assert "over station" in card
    conv = awc.parse_airsigmets(load("awc_airsigmet.json"))[0]
    assert RA.hazard_altitudes(conv) == "tops FL450"
    assert RA.hazard_altitudes(replace(turb, base_ft=None, top_ft=None)) == ""


def test_alaska_airmets_and_regions():
    ak = awc.parse_ak_airmets(load("awc_airmet_ak.geojson.json"))
    assert len(ak) == 33 and all(h.kind == "AIRMET" and h.polygon for h in ak)
    A.locate_hazards(ak, 61.17, -150.0)  # Anchorage
    assert any(h.contains_point for h in ak)
    assert [A.region_for(*p) for p in [(35.2, -80.8), (61.2, -150), (52, 178), (21.3, -157.9), (51.5, 0)]] == [
        "conus", "alaska", "alaska", "hawaii", "other"]


async def test_fetch_hazards_by_region_and_partial_failure():
    http = FakeHttp({"api/data/gairmet": FetchError("HTTP 503", 503)})
    hs, stale = await awc.fetch_hazards(http, *KCLT)
    assert set(hs.sources) == {"SIGMET", "CWA"} and hs.errors == ["G-AIRMET: HTTP 503"] and not stale
    urls = [u for u, _ in http.calls]
    assert not any("isigmet" in u or "api/data/airmet" in u for u in urls)  # CONUS uses domestic + G-AIRMET
    ak_http = FakeHttp()
    hs_ak, _ = await awc.fetch_hazards(ak_http, 61.17, -150.0)
    assert {"SIGMET", "AIRMET", "CWA"} == set(hs_ak.sources)
    assert any("format" in p and p["format"] == "geojson" for u, p in ak_http.calls if "api/data/airmet" in u)
    with pytest.raises(FetchError):
        await awc.fetch_hazards(FakeHttp({"aviationweather.gov": FetchError("down")}), *KCLT)


# ── derived tools ────────────────────────────────────────────────────────────
def test_density_altitude():
    # ISA sea level → ~0 ft
    assert A.density_altitude_ft(0, 15, 1013.25) == pytest.approx(0, abs=40)
    # Denver-ish, hot day: rule of thumb PA + 120 ft/°C above ISA ≈ 8,000 ft
    da = A.density_altitude_ft(5000, 30, 1013.25)
    assert 7700 < da < 8600
    # High pressure lowers it, humidity raises it.
    assert A.density_altitude_ft(5000, 30, 1030) < da < A.density_altitude_ft(5000, 30, 1013.25, dewpoint_c=20)
    assert A.pressure_altitude_ft(1000, 1013.25) == pytest.approx(1000, abs=30)
    av = AviationResult(choice=StationChoice(AviationStation("KCLT", "", 0, 0, elev_m=228), "auto"))
    av.metars.ok(awc.parse_metars(load("awc_metar.json")), False, NOW)
    assert RA.density_altitude_for(av) == pytest.approx(1645, abs=40)


def test_wind_components():
    head, cross = A.wind_components(180, 220, 14)
    assert round(head) == 11 and round(cross) == 9  # from the right
    head, cross = A.wind_components(360, 270, 10)
    assert round(head) == 0 and round(cross) == -10  # from the left
    head, _ = A.wind_components(90, 270, 10)
    assert round(head) == -10  # tailwind
    assert A.true_to_magnetic(220, -7) == 227 and A.parse_magdec("07W") == -7 and A.parse_magdec("02E") == 2


def test_crosswind_text():
    m = awc.parse_metar({"icaoId": "K", "obsTime": 1791388320, "rawOb": "METAR K", "wdir": 220, "wspd": 14, "wgst": 22})
    t = RA.crosswind_text(m, "18", 180, None).plain
    assert "RWY 18 · wind 220/14G22" in t and "Headwind 11 kt" in t and "Xwind 9G14 kt from right" in t
    t = RA.crosswind_text(m, "", 20, None).plain
    assert "HDG 020°" in t and "Tailwind 13 kt" in t and "Xwind 5G8 kt from left" in t
    t = RA.crosswind_text(m, "04", 40, None).plain
    assert "Tailwind 14 kt · Xwind 0 kt" in t and "G0" not in t
    assert "press h" in RA.crosswind_text(m, "", None, None).plain
    assert "M" in RA.crosswind_text(m, "18", 180, -7).plain.split("\n")[0]  # converted to magnetic


def test_runway_parsing():
    assert Runway("18C/36C").ends == [("18C", 180), ("36C", 360)]
    assert Runway("H1").ends == [] and Runway("09/27").ends == [("09", 90), ("27", 270)]
    assert Runway("NW/SE").ends == [] and Runway("4L/22R").ends == [("4L", 40), ("22R", 220)]
    assert [A.parse_runway_heading(x) for x in ("18", "RWY 36L", "175", "175°", "0", "37", "abc", "18X")] == [
        180, 360, 175, 175, None, None, None, None]


def test_condition_trend():
    ms = awc.parse_metars(load("awc_metar_speci.json"))
    tpa = [m for m in ms if m.station == "KTPA"]
    assert A.condition_trend(tpa) in ("deteriorating", "improving", "steady")
    base = awc.parse_metars(load("awc_metar.json"))
    assert A.condition_trend(base) == "steady"
    worse = [replace(base[0], flight_category="IFR")] + base[1:]
    better = [replace(base[0], flight_category="VFR")] + [replace(m, flight_category="IFR") for m in base[1:]]
    assert A.condition_trend(worse) == "deteriorating" and A.condition_trend(better) == "improving"
    lines = RA.trend_lines(worse)
    assert len(lines) == 7 and "deteriorating" in lines[-1].plain and lines[0].plain.startswith("10Z")


# ── winds aloft ──────────────────────────────────────────────────────────────
def test_fd_group_decoding():
    assert A.freezing_level_ft([]) is None
    lv = awc.decode_fd_group("2714+05", 9000)
    assert (lv.wind_dir, lv.wind_speed_kt, lv.temp_c) == (270, 14, 5)
    lv = awc.decode_fd_group("731960", 39000)  # 73 → 230°, 119 kt, -60 °C
    assert (lv.wind_dir, lv.wind_speed_kt, lv.temp_c) == (230, 119, -60)
    lv = awc.decode_fd_group("9900-02", 12000)
    assert lv.wind_dir is None and lv.wind_speed_kt == 0 and lv.temp_c == -2
    assert awc.decode_fd_group("", 3000) is None


def test_windtemp_parsing_and_site():
    sites = awc.parse_windtemp(load_text("awc_windtemp_us.txt"), NOW)
    gsp = sites["GSP"]
    assert [lv.altitude_ft for lv in gsp.levels][:6] == [3000, 6000, 9000, 12000, 18000, 24000]
    assert (gsp.levels[1].wind_dir, gsp.levels[1].wind_speed_kt, gsp.levels[1].temp_c) == (350, 11, 13)
    assert gsp.levels[-1].temp_c == -52 and gsp.valid == datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    assert gsp.use_period == "0800-1500Z"
    assert (gsp.use_from.hour, gsp.use_to.hour) == (8, 15) and not gsp.in_use(NOW)
    assert awc._use_window("1500-0000", datetime(2026, 10, 7, 18, tzinfo=timezone.utc))[1] == datetime(
        2026, 10, 8, 0, tzinfo=timezone.utc)
    # Levels below a high station are blank in the bulletin, not mis-shifted.
    assert [lv.altitude_ft for lv in sites["DEN"].levels][0] == 9000
    assert A.freezing_level_ft(gsp.levels) == 13200
    sid, _, dist = awc.nearest_fd_site(*KCLT)
    assert sid == "GSP" and dist < 80


async def test_winds_picks_bulletin_in_use():
    us = load_text("awc_windtemp_us.txt")
    later = us.replace("VALID 071200Z   FOR USE 0800-1500Z", "VALID 071800Z   FOR USE 1500-0000Z")
    http = FakeHttp()
    http_get = http.get_text_cached

    async def by_fcst(url, params=None, headers=None):  # route on the fcst parameter
        if params and params.get("fcst") == "12":
            return later, False
        return await http_get(url, params, headers)

    http.get_text_cached = by_fcst
    winds, _ = await awc.fetch_winds(http, *KCLT)
    assert winds.use_period == "1500-0000Z" and winds.in_use(NOW)
    # Nothing in use (all in the past): fall back to the first, and say so on screen.
    winds, _ = await awc.fetch_winds(FakeHttp(), *KCLT)
    assert winds.use_period == "0800-1500Z"
    assert "past its use period" in text(RA.winds_panel(Feed(winds, NOW, loaded=True), NOW))


async def test_winds_panel_highlights():
    winds, _ = await awc.fetch_winds(FakeHttp(), *KCLT)
    winds.levels[6] = replace(winds.levels[6], wind_speed_kt=85)
    out = text(RA.winds_panel(Feed(winds, NOW, loaded=True), NOW, RA.WINDS_LEVELS))
    assert "FD site GSP" in out and "Freezing level ≈ 13,200 ft" in out and "260/85" in out
    assert "not a substitute for formal flight planning" in out


# ── freshness, offline, partial failure ──────────────────────────────────────
async def test_stale_aviation_cache_is_marked(tmp_path):
    online = True

    def handler(request):
        if online:
            return httpx.Response(200, json=load("awc_metar.json"))
        raise httpx.ConnectError("offline")

    http = Http(cache_dir=tmp_path)
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fresh, stale = await awc.fetch_metars(http, "KCLT")
    assert not stale
    online = False
    cached, stale = await awc.fetch_metars(http, "KCLT")
    assert stale and cached == fresh
    await http.aclose()

    av = AviationResult(choice=StationChoice(AviationStation("KCLT", "Charlotte", *KCLT), "auto", 5.5, 270))
    av.metars.ok(cached, True, NOW - timedelta(hours=3))
    later = NOW + timedelta(hours=2)
    header = RA.station_header(av, "imperial", later).plain
    assert "offline · cached 5h00m ago" in header and "METAR 2h18m old" in header
    summary = text(RA.aviation_summary(av, 60, now=later))
    assert "offline" in summary and "2h18m" in summary


def test_feed_failure_keeps_data_but_marks_stale():
    f = Feed()
    f.fail("timeout")
    assert f.data is None and not f.stale and f.error == "timeout"
    f.ok([1], False, NOW)
    f.fail("timeout")
    assert f.data == [1] and f.stale


async def test_once_partial_failure_is_isolated():
    from termwx.once import fetch_aviation

    http = FakeHttp({"api/data/pirep": FetchError("HTTP 502", 502), "api/data/taf": FetchError("timeout")})
    av = await fetch_aviation(http, replace_loc(), None)
    assert av.metar and av.metars.error == ""
    assert av.pireps.error == "HTTP 502" and av.taf.error == "timeout"
    assert av.hazards.data and av.winds.data
    out = text(RA.once_section(av, "imperial", NOW))
    assert "KCLT" in out and "PIREPs unavailable" in out and "TAF --" in out


def replace_loc():
    from termwx.models import Location

    return Location("Charlotte", CLT_LAT, CLT_LON, "North Carolina", "United States", "US", "America/New_York")


def test_summary_counts_and_notam_status(monkeypatch):
    av = AviationResult(choice=StationChoice(AviationStation("KCLT", "C", *KCLT, has_taf=True), "auto", 5.5, 270))
    av.metars.ok(awc.parse_metars(load("awc_metar.json")), False, NOW)
    av.taf.ok(awc.parse_taf(load("awc_taf.json")), False, NOW)
    av.pireps.ok(awc.parse_pireps(load("awc_pirep.json"), KCLT), False, NOW)
    hz = AviationHazard("G-AIRMET", "G-AIRMET ZULU", "ICE", NOW, NOW + timedelta(hours=3), contains_point=True)
    sig = AviationHazard("SIGMET", "SIGMET NOVEMBER 3", "TURB", NOW, NOW + timedelta(minutes=42), distance_nm=20)
    av.hazards.ok(HazardSet([hz, sig]), False, NOW)
    lines = text(RA.aviation_summary(av, 60, now=NOW)).splitlines()
    assert lines[0].startswith("KCLT   VFR   METAR 18m old")
    assert lines[1].startswith("calm · 10SM · SKC")
    assert lines[2].startswith("TAF VFR")
    assert lines[3].startswith("1 SIGMET · 1 AIRMET · 5 PIREPs/100NM")
    assert len(lines) == 4  # compact: the full picture is on the Aviation screen
    monkeypatch.delenv(faa_notam.ENV_ID, raising=False)
    assert faa_notam.status().startswith("not configured") and "NOTAMS@faa.gov" in faa_notam.status()
    monkeypatch.setenv(faa_notam.ENV_ID, "x")
    monkeypatch.setenv(faa_notam.ENV_SECRET, "y")
    assert faa_notam.configured()


# ── forecast discussion ──────────────────────────────────────────────────────
def test_discussion_parsing_and_order():
    disc = nws.parse_discussion(load("nws_afd.json"), "GSP")
    assert disc.issued == datetime(2026, 10, 7, 10, 34, tzinfo=timezone.utc)
    assert disc.sections[0].key == "AVIATION" and disc.aviation.body.startswith("At KCLT and elsewhere")
    assert "Outlook: Mostly VFR" in disc.aviation.body
    assert disc.text.startswith("000\nFXUS62 KGSP") and disc.text.rstrip().endswith("JDL/PM")  # verbatim
    synthetic = (".SYNOPSIS...\nHigh pressure.\n&&\n.NEAR TERM /THROUGH TONIGHT/...\nDry.\n\n.SHORT TERM...\nWarm.\n"
                 "&&\n.LONG TERM /SATURDAY THROUGH TUESDAY/...\nRain.\n&&\n.AVIATION /18Z TAFS/...\nVFR.\n&&\n$$")
    keys = [s.key for s in nws.order_sections(nws.parse_discussion_sections(synthetic))]
    assert keys == ["AVIATION", "NEAR TERM", "SHORT TERM", "LONG TERM", "SYNOPSIS"]


async def test_discussion_fetch_and_reuse():
    from termwx.models import Location

    http = FakeHttp()
    office = await nws.fetch_office(http, Location("Charlotte", CLT_LAT, CLT_LON, country_code="US"))
    assert office == "GSP"
    disc = await nws.fetch_discussion(http, office)
    assert disc.office == "GSP" and disc.sections
    product_calls = sum(1 for u, _ in http.calls if "/products/" in u and "types" not in u)
    again = await nws.fetch_discussion(http, office, disc)
    assert again is disc  # unchanged product is not downloaded again
    assert sum(1 for u, _ in http.calls if "/products/" in u and "types" not in u) == product_calls
    assert await nws.fetch_office(FakeHttp(), Location("Tokyo", 35.68, 139.69, country_code="JP")) is None


def test_discussion_indicator():
    disc = nws.parse_discussion(load("nws_afd.json"), "GSP")
    assert RA.discussion_indicator(disc, now=NOW).plain.startswith("AFD GSP · issued 5h36m ago")
    assert RA.discussion_indicator(None, "").plain == ""


# ── config / CLI ─────────────────────────────────────────────────────────────
def test_config_aviation_station_roundtrip(tmp_path):
    path = tmp_path / "c.toml"
    Config(path=path).save()
    assert "aviation_station" not in path.read_text()
    Config(path=path, aviation_station="KCLT").save()
    assert Config.load(path).aviation_station == "KCLT"


def test_airport_cli_flag(monkeypatch, tmp_path):
    from termwx import __main__ as cli

    seen = {}

    async def fake_snapshot(cfg, query, units, demo=False, aviation_detail=False):
        seen.update(station=cfg.aviation_station, detail=aviation_detail)
        return 0

    monkeypatch.setattr("termwx.once.snapshot", fake_snapshot)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, path=None: cls(path=tmp_path / "c.toml", aviation_station="KJQF")))
    assert cli.main(["--once", "--airport", "kclt", "--aviation"]) == 0
    assert seen == {"station": "KCLT", "detail": True}
    cli.main(["--once", "--airport", "auto"])
    assert seen["station"] == ""
    cli.main(["--once"])
    assert seen["station"] == "KJQF"


async def test_once_snapshot_aviation_sections():
    from termwx.once import snapshot

    console = Console(width=110, record=True, color_system=None, file=open("/dev/null", "w"))
    cfg = Config(location=replace_loc(), aviation_station="KCLT")
    http = FakeHttp()
    assert await snapshot(cfg, None, "imperial", http=http, console=console) == 0
    out = console.export_text()
    assert "KCLT · Charlotte/Douglas Intl (6 mi W) · VFR" in out
    assert "METAR 18m old · 00000KT 10SM SKC" in out and "0 SIGMET · 0 AIRMET · 5 PIREPs/100NM" in out
    assert "Raw TAF" not in out and "AVL UA" not in out  # compact by default
    console = Console(width=110, record=True, color_system=None, file=open("/dev/null", "w"))
    await snapshot(cfg, None, "imperial", aviation_detail=True, http=FakeHttp(), console=console)
    out = console.export_text()
    assert "Raw TAF" in out and "Winds aloft" in out and "PIREPs · 100 NM" in out and "Area Forecast" not in out


# ── TFRs (tfr.faa.gov) ───────────────────────────────────────────────────────
TFR_NOW = datetime(2026, 10, 22, 16, 45, tzinfo=timezone.utc)  # when the TFR fixtures were recorded
RALEIGH_TFR = (35.79555, -78.71139)  # center of FDC 6/9169


def test_tfr_list_parsing_groups_areas():
    tfrs = {t.notam_id: t for t in faa_tfr.parse_tfr_list(load("faa_tfr_wfs.json"))}
    assert set(tfrs) == {"6/9169", "6/0675", "6/1679", "6/4348"}
    gf = tfrs["6/4348"]
    assert len(gf.polygons) == 2 and gf.legal == "SECURITY" and gf.state == "ND" and gf.facility
    lat, lon = gf.polygons[0][0]
    assert 47 < lat < 49 and -98 < lon < -97  # GeoJSON [lon, lat] swapped to (lat, lon)
    assert faa_tfr.detail_url("6/4348").endswith("/download/detail_6_4348.xml")


def test_tfr_detail_parsing():
    t = faa_tfr.parse_tfr_list(load("faa_tfr_wfs.json"))[0]
    faa_tfr.parse_tfr_detail(load_text("faa_tfr_detail_6_9169.xml"), t)
    assert t.valid_from == datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc)
    assert t.valid_to == datetime(2026, 10, 26, 3, 30, tzinfo=timezone.utc)
    assert t.rule == "99.7" and len(t.areas) == 1
    assert (t.areas[0].base_ft, t.areas[0].base_ref, t.areas[0].top_ft, t.areas[0].top_ref) == (0, "AGL", 400, "AGL")
    assert t.raw.startswith("!FDC 6/9169") and t.scheduled  # "DLY 1300-0330"
    gf = next(x for x in faa_tfr.parse_tfr_list(load("faa_tfr_wfs.json")) if x.notam_id == "6/4348")
    faa_tfr.parse_tfr_detail(load_text("faa_tfr_detail_6_4348.xml"), gf)
    assert [(a.base_ft, a.base_ref, a.top_ft, a.top_ref) for a in gf.areas] == [(0, "AGL", 2400, "MSL"), (2400, "MSL", 18000, "FL")]
    assert gf.floor is gf.areas[0]
    assert RA.tfr_altitudes(gf) == "SFC–FL180 · 2 areas" and RA.area_altitudes(gf.areas[1]) == "2,400 MSL–FL180"
    with pytest.raises(ValueError):
        faa_tfr.parse_tfr_detail("<XNOTAM-Update/>", gf)


async def test_fetch_tfrs_filters_by_distance_and_survives_detail_failures():
    http = FakeHttp()
    near, stale = await faa_tfr.fetch_tfrs(http, *RALEIGH_TFR)
    assert [t.notam_id for t in near] == ["6/9169"] and near[0].contains_point and not stale
    assert near[0].rule == "99.7" and near[0].raw  # detail fetched only for the nearby one
    assert sum("download/detail" in url for url, _ in http.calls) == 1
    # 20 NM east: outside, but within the 30 NM display radius
    near, _ = await faa_tfr.fetch_tfrs(FakeHttp(), RALEIGH_TFR[0], RALEIGH_TFR[1] + 0.4)
    assert near and not near[0].contains_point and 15 < near[0].distance_nm < 25
    assert (await faa_tfr.fetch_tfrs(FakeHttp(), CLT_LAT, CLT_LON))[0] == []
    failing = FakeHttp({"detail_6_9169": FetchError("HTTP 500 from tfr.faa.gov", 500)})
    near, _ = await faa_tfr.fetch_tfrs(failing, *RALEIGH_TFR)
    assert near[0].title.startswith("RALEIGH") and "500" in near[0].detail_error and not near[0].areas
    with pytest.raises(FetchError):
        await faa_tfr.fetch_tfrs(FakeHttp({"tfr.faa.gov/geoserver": FetchError("offline")}), *RALEIGH_TFR)


async def test_tfr_rendering_and_summary_count():
    near, _ = await faa_tfr.fetch_tfrs(FakeHttp(), *RALEIGH_TFR)
    card = text(RA.tfr_card(near[0], TFR_NOW))
    assert "FDC 6/9169 · SECURITY · §99.7" in card and "SFC–400 AGL" in card and "over your location" in card
    detail = text(RA.tfr_detail(near[0], TFR_NOW))
    assert "14 CFR §99.7" in detail and "!FDC 6/9169" in detail
    assert A.relevant_tfrs(near, TFR_NOW) == near
    assert A.relevant_tfrs(near, datetime(2026, 10, 27, tzinfo=timezone.utc)) == []  # expired
    av = AviationResult(choice=StationChoice(AviationStation("KRDU", "Raleigh-Durham", 35.88, -78.79), "auto", 6, 0))
    av.hazards.ok(HazardSet(), False, TFR_NOW)
    av.pireps.ok([], False, TFR_NOW)
    av.tfrs.ok(near, False, TFR_NOW)
    assert text(RA.hazards_line(av, TFR_NOW)).strip().endswith("· 1 TFR")


# ── NOTAMs (FAA NMS, opt-in) ─────────────────────────────────────────────────
NMS_RESPONSE = {
    "status": "Success",
    "data": {"geojson": [
        {"type": "Feature", "geometry": None, "properties": {"coreNOTAMData": {"notam": {
            "id": "NOTAM_1", "number": "10/123", "type": "N", "issued": "2026-10-06T12:00:00Z",
            "classification": "DOM", "location": "CLT", "icaoLocation": "KCLT", "featureType": "RWY",
            "effectiveStart": "2026-10-06T12:00:00Z", "effectiveEnd": "2026-10-09T22:00:00Z",
            "text": "RWY 18C/36C CLSD"}}}},
        {"type": "Feature", "geometry": None, "properties": {"coreNOTAMData": {"notam": {
            "number": "10/099", "icaoLocation": "KCLT", "featureType": "OBST",
            "effectiveStart": "2026-09-01T00:00:00Z", "effectiveEnd": "PERM", "text": "OBST TOWER LGT U/S"}}}},
    ]},
}


def nms_http(tmp_path, responses: list[httpx.Response] | None = None):
    calls: list[httpx.Request] = []
    queue = list(responses or [])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/auth/token":
            return httpx.Response(200, json={"access_token": f"tok{len(calls)}", "expires_in": 1799})
        return queue.pop(0) if queue else httpx.Response(200, json=NMS_RESPONSE)

    http = Http(cache_dir=tmp_path)
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return http, calls


def test_parse_nms_notams():
    notams = faa_notam.parse_notams(NMS_RESPONSE)
    assert [n.number for n in notams] == ["10/123", "10/099"]
    rwy, obst = notams
    assert rwy.location == "KCLT" and rwy.feature == "RWY" and rwy.text == "RWY 18C/36C CLSD"
    assert rwy.valid_to == datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc) and obst.valid_to is None  # PERM
    with pytest.raises(ValueError):
        faa_notam.parse_notams({"data": {}})
    assert "RWY" in text(RA.notam_line(rwy, NOW)) and "RWY 18C/36C CLSD" in text(RA.notam_detail(rwy, NOW))


async def test_nms_unconfigured_makes_no_request(tmp_path):
    http, calls = nms_http(tmp_path)
    assert not faa_notam.configured()
    with pytest.raises(FetchError):
        await faa_notam.fetch_notams(http, "KCLT")
    assert calls == []


async def test_nms_token_reuse_and_no_disk_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(faa_notam.ENV_ID, "my-id")
    monkeypatch.setenv(faa_notam.ENV_SECRET, "s3cret-value")
    http, calls = nms_http(tmp_path)
    notams, stale = await faa_notam.fetch_notams(http, "KCLT")
    assert [n.number for n in notams] == ["10/123", "10/099"] and not stale
    await faa_notam.fetch_notams(http, "KCLT")
    paths = [r.url.path for r in calls]
    assert paths == ["/v1/auth/token", "/nmsapi/v1/notams", "/nmsapi/v1/notams"]  # token reused
    auth = calls[0]
    assert auth.url.host == "api-nms.aim.faa.gov" and auth.headers["authorization"].startswith("Basic ")
    assert calls[1].headers["authorization"] == "Bearer tok1" and calls[1].headers["nmsresponseformat"] == "GEOJSON"
    assert calls[1].url.params["location"] == "KCLT"
    cached = "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    assert "s3cret" not in cached and "tok1" not in cached and "10/123" not in cached


async def test_nms_401_signs_in_again_and_bad_schema_fails_only_notams(tmp_path, monkeypatch):
    monkeypatch.setenv(faa_notam.ENV_ID, "id")
    monkeypatch.setenv(faa_notam.ENV_SECRET, "secret")
    monkeypatch.setenv(faa_notam.ENV_SERVER, "staging")
    http, calls = nms_http(tmp_path, [httpx.Response(401), httpx.Response(200, json=NMS_RESPONSE)])
    notams, _ = await faa_notam.fetch_notams(http, "KCLT")
    assert len(notams) == 2 and [r.url.path for r in calls].count("/v1/auth/token") == 2
    assert calls[0].url.host == "api-staging.cgifederal-aim.com"
    http, _ = nms_http(tmp_path, [httpx.Response(200, json={"unexpected": True})])
    with pytest.raises(FetchError, match="unexpected response format"):
        await faa_notam.fetch_notams(http, "KCLT")
    rejected = Http(cache_dir=tmp_path)
    rejected.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    faa_notam.forget_token()
    with pytest.raises(FetchError, match="sign-in failed"):
        await faa_notam.fetch_notams(rejected, "KCLT")
