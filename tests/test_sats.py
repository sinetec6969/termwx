from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeHttp, FetchError, load, load_text
from termwx import orbit, render_sats as RS
from termwx.api import sats as S
from termwx.config import Config, SatSettings
from termwx.sat_models import APRS, FM, Catalog, SatState
from termwx.screens_sats import SatScreen

from test_app import CLT, make_app, settle, shown

# wheretheiss.at's ISS position at this instant, computed from the same TLE as the fixture
ISS_REF_T = datetime.fromtimestamp(1795288782, timezone.utc)
ISS_REF = (45.213050680272, 16.875216319032, 420.43438767551)


def fixture_sats():
    tles = S.parse_tles(load_text("celestrak_amateur.tle"))
    return {n: S.Satellite(n, *tles[n]) for n in tles}


# ── SatNOGS selection ─────────────────────────────────────────────────────────
def test_select_keeps_fm_repeaters_and_aprs_digipeaters():
    names = S.display_names(load("satnogs_satellites.json"))
    wanted = S.select(load("satnogs_transmitters.json"), names)
    assert set(wanted) == {25544, 27607, 43017, 43775, 40931}
    # QO-100 transponders, a DTMF command channel, a temporary ID with no real NORAD,
    # an AFSK downlink with no uplink and a dead repeater are all left out
    assert not {43700, 36799, 98248, 99914, 43770, 7530} & set(wanted)
    iss = wanted[25544]
    assert iss.alias == "ISS" and {t.kind for t in iss.transmitters} == {FM, APRS}
    assert wanted[43775].alias == "VO-96"  # SatNOGS temp ID 99914 followed to its NORAD number
    assert wanted[43017].alias == "AO-91"  # OSCAR number preferred over FOX-1B
    so50 = wanted[27607].transmitters[0]
    assert so50.kind == FM and so50.uplink == 145850000 and so50.downlink == 436795000 and so50.ctcss == 67.0


@pytest.mark.parametrize("desc, tone", [
    ("Mode V/U FM Voice CTCSS 67.0 Hz", 67.0),
    ("V/U FM Voice (PL 88.5Hz)", 88.5),
    ("Mode V/U FM 67.0 PL", 67.0),
    ("Mode U/V FM Voice (no CTCSS any longer)", None),
])
def test_ctcss(desc, tone):
    assert S.ctcss_of(desc) == tone


def test_parse_tles_crlf_and_nameless():
    text = load_text("celestrak_amateur.tle")
    tles = S.parse_tles(text)
    assert tles[25544][0] == "ISS (ZARYA)" and len(tles[25544][1]) == 69
    l1, l2 = tles[27607][1:]
    assert S.parse_tles(f"{l1}\n{l2}\n")[27607][0] == "27607"
    assert S.parse_tles("No GP data found") == {}


# ── catalog fetch and refresh policy ──────────────────────────────────────────
async def test_fetch_catalog():
    http = FakeHttp()
    cat = await S.fetch_catalog(http)
    assert [s.short_name for s in cat.satellites] == ["AO-91", "ISS", "SO-50", "VO-96"]
    catnr = sorted(p["CATNR"] for url, p in http.calls if p and "CATNR" in p)
    assert catnr == ["40931", "43775"]  # not in the amateur group; 40931 has decayed ("No GP data found")
    assert cat.fetched_at and cat.tx_fetched_at == cat.fetched_at and not cat.stale


async def test_satnogs_reused_for_a_day_unless_forced():
    prev = await S.fetch_catalog(FakeHttp())
    prev.tx_fetched_at = S.utcnow() - timedelta(hours=5)
    http = FakeHttp()
    cat = await S.fetch_catalog(http, prev)
    assert not any("satnogs.org/api/trans" in url for url, _ in http.calls) and len(cat.satellites) == 4
    assert cat.tx_fetched_at == prev.tx_fetched_at
    http = FakeHttp()
    await S.fetch_catalog(http, prev, force=True)
    assert any("satnogs.org/api/trans" in url for url, _ in http.calls)


async def test_fetch_failures():
    prev = await S.fetch_catalog(FakeHttp())
    prev.tx_fetched_at = None
    # SatNOGS down: keep the previous transmitter list
    cat = await S.fetch_catalog(FakeHttp({"db.satnogs.org": FetchError("down")}), prev)
    assert len(cat.satellites) == 4
    # ...but with nothing to fall back on it's an error
    with pytest.raises(FetchError):
        await S.fetch_catalog(FakeHttp({"db.satnogs.org": FetchError("down")}))
    # CelesTrak down is always an error (the caller keeps its saved copy)
    with pytest.raises(FetchError):
        await S.fetch_catalog(FakeHttp({"celestrak.org": FetchError("down")}), prev)


# SO-131: narrow-FM repeater, six-digit catalog number, TLE only on SatNOGS (Alpha-5)
SO131_TX = {"sat_id": "AZNB-1972-1511-6151-8557", "norad_cat_id": 98205, "norad_follow_id": 101101, "alive": True,
            "mode": "FMN", "uplink_mode": "FMN", "uplink_low": 145925000, "downlink_low": 436888000,
            "description": "Mode V/U - FM Repeater (Voice)"}
SO131_SAT = {"sat_id": "AZNB-1972-1511-6151-8557", "name": "UNNE-1B", "names": "HADES-E2, SO-131"}
SO131_TLE = [{"tle0": "0 UNNE-1B", "sat_id": "AZNB-1972-1511-6151-8557", "norad_cat_id": 98205,
              "tle1": "1 A1101U 26227CP  26327.13631616  .00009579  00000-0  45653-3 0  9995",
              "tle2": "2 A1101  97.4371  35.8054 0002264  66.6484 293.4989 15.19317022  7955"}]
CAS3H_SAT = {"sat_id": "AHVS-7983-8710-8819-8034", "name": "LILACSAT 2", "names": "CAS-3H XW-2H"}


def test_catalog_number_alpha5():
    assert S.catalog_number("25544") == 25544 and S.catalog_number("A1101") == 101101
    assert S.catalog_number("J0000") == 180000 and S.catalog_number("Z9999") == 339999  # I and O skipped
    assert S.catalog_number("I0000") is None and S.catalog_number("") is None
    assert list(S.parse_tles("UNNE-1B\n" + SO131_TLE[0]["tle1"] + "\n" + SO131_TLE[0]["tle2"])) == [101101]


def test_select_narrow_fm_and_six_digit_norad():
    sats = [SO131_SAT]
    wanted = S.select([SO131_TX], S.display_names(sats), S.all_names(sats))
    w = wanted[101101]
    assert w.alias == "SO-131" and w.transmitters[0].kind == FM and w.names == ["UNNE-1B", "HADES-E2", "SO-131"]
    assert S.norad_of({"norad_cat_id": 98205, "norad_follow_id": None}) is None  # still a temporary ID
    assert S.all_names([CAS3H_SAT])[CAS3H_SAT["sat_id"]] == ["LILACSAT 2", "CAS-3H XW-2H", "CAS-3H", "XW-2H"]


async def test_satnogs_tle_fallback():
    tx = load("satnogs_transmitters.json") + [SO131_TX]
    sats = load("satnogs_satellites.json") + [SO131_SAT]
    http = FakeHttp({"api/transmitters": tx, "api/satellites": sats, "api/tle": SO131_TLE})
    cat = await S.fetch_catalog(http)
    so131 = next(s for s in cat.satellites if s.norad == 101101)
    assert so131.short_name == "SO-131" and so131.sat_id == SO131_TX["sat_id"]
    obs = orbit.Observer(CLT.lat, CLT.lon)
    assert -90 <= orbit.look(so131, obs, datetime(2026, 11, 23, 12, tzinfo=timezone.utc)).el <= 90
    assert any(p and p.get("sat_id") == SO131_TX["sat_id"] for url, p in http.calls if "api/tle" in url)


def test_tracked_filter():
    sats = fixture_sats()
    sats[40908] = S.Satellite(40908, "LILACSAT-2", "", "", names=["LILACSAT 2", "CAS-3H XW-2H", "CAS-3H"])
    cat = Catalog(list(sats.values()))
    assert S.tracked(cat, []) is cat
    out = S.tracked(cat, ["so 50", "iss", "cas-3h", "43017", "NOPE-1"])
    assert [s.norad for s in out.satellites] == [27607, 25544, 40908, 43017]  # in the order given
    assert out.satellites[2].short_name == "CAS-3H"  # shown by the name it's tracked as
    assert cat.satellites[-1].short_name == "LILACSAT-2"  # ...without touching the full catalog
    assert S.not_found(cat, ["ISS", "NOPE-1"]) == ["NOPE-1"]


def test_is_due():
    now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)

    def cat(age_h):
        return Catalog([], now - timedelta(hours=age_h))

    assert S.is_due(None, now, 3) and S.is_due(Catalog(), now, 3)
    # startup: refresh anything CelesTrak has had time to update
    assert not S.is_due(cat(1.5), now, 3, startup=True) and S.is_due(cat(2.5), now, 3, startup=True)
    # afterwards: every refresh_hours, never more often than 2 h
    assert not S.is_due(cat(2.5), now, 3) and S.is_due(cat(3), now, 3)
    assert not S.is_due(cat(1.9), now, 0.5)


def test_cache_round_trip(tmp_path):
    sats = list(fixture_sats().values())
    sats[0].alias = "TEST-1"
    now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
    S.save_cache(Catalog(sats, now, now - timedelta(hours=1)), tmp_path / "sats.json")
    back = S.load_cache(tmp_path / "sats.json")
    assert back.fetched_at == now and [s.to_dict() for s in back.satellites] == [s.to_dict() for s in sats]
    assert S.load_cache(tmp_path / "missing.json") is None


# ── orbit ─────────────────────────────────────────────────────────────────────
def test_iss_matches_independent_position():
    iss = fixture_sats()[25544]
    lk = orbit.look(iss, orbit.Observer(0, 0), ISS_REF_T)
    assert lk.lat == pytest.approx(ISS_REF[0], abs=0.05)
    assert lk.lon == pytest.approx(ISS_REF[1], abs=0.05)
    assert lk.alt_km == pytest.approx(ISS_REF[2], abs=1)


def test_gmst_at_j2000():
    assert orbit.gmst(2451545.0) == pytest.approx(4.894961, abs=1e-5)  # 280.46062°


def test_passes_are_consistent():
    iss = fixture_sats()[25544]
    obs = orbit.Observer(CLT.lat, CLT.lon)
    start = iss.epoch
    passes = orbit.find_passes(iss, obs, start, 24, min_el=10)
    assert passes
    for p in passes:
        assert p.aos < p.tca < p.los and timedelta(minutes=3) < p.duration < timedelta(minutes=15)
        assert abs(orbit.elevation(iss, obs, p.aos)) < 0.1 and abs(orbit.elevation(iss, obs, p.los)) < 0.1
        assert p.max_el >= 10 and orbit.elevation(iss, obs, p.tca) == pytest.approx(p.max_el, abs=0.01)
        assert len(p.track) == orbit.TRACK_POINTS and p.track[0][0] == pytest.approx(p.aos_az, abs=0.5)
    assert [p.aos for p in passes] == sorted(p.aos for p in passes)
    low = orbit.find_passes(iss, obs, start, 24, min_el=0)
    assert len(low) >= len(passes)


def test_pass_in_progress_has_its_real_aos():
    iss = fixture_sats()[25544]
    obs = orbit.Observer(CLT.lat, CLT.lon)
    first = orbit.find_passes(iss, obs, iss.epoch, 24, min_el=0)[0]
    mid = orbit.find_passes(iss, obs, first.tca, 24, min_el=0)[0]
    assert abs((mid.aos - first.aos).total_seconds()) < 2 and mid.active(first.tca)


def test_geostationary_never_makes_a_pass():
    qo100 = fixture_sats()[43700]
    assert orbit.find_passes(qo100, orbit.Observer(25.3, 51.5), qo100.epoch, 24, 0) == []


def test_doppler_signs():
    # approaching (negative range rate): hear it high, transmit low
    assert orbit.doppler_downlink(436_795_000, -7.0) > 436_795_000
    assert orbit.doppler_uplink(145_850_000, -7.0) < 145_850_000
    assert orbit.doppler_downlink(436_795_000, 7.0) == pytest.approx(436_795_000 - 10_199, abs=2)


# ── rendering ─────────────────────────────────────────────────────────────────
def test_short_names():
    sats = fixture_sats()
    assert sats[27607].short_name == "SO-50" and sats[25544].short_name == "ISS"
    assert sats[43017].short_name == "RADFXSAT (FOX-1B)"  # no alias: the CelesTrak name


def test_polar_plot_marks_aos_los_and_peak():
    iss = fixture_sats()[25544]
    p = orbit.find_passes(iss, orbit.Observer(CLT.lat, CLT.lon), iss.epoch, 24, 10)[0]
    grid, legend = RS.polar(p).plain.split("\nA AOS")
    assert all(ch in grid for ch in "NESWAL✱") and grid.count("A") == 1 and grid.count("L") == 1


# ── config ────────────────────────────────────────────────────────────────────
def test_sat_settings_round_trip_and_clamp(tmp_path):
    cfg = Config(path=tmp_path / "c.toml")
    cfg.sats = SatSettings(panel=False, tle_refresh_hours=6, min_elevation=15)
    cfg.save()
    assert Config.load(tmp_path / "c.toml").sats == SatSettings(False, 6.0, 15.0)
    (tmp_path / "c.toml").write_text('[sats]\ntle_refresh_hours = 0.5\nmin_elevation = -5\npanel = "yes"\n')
    assert Config.load(tmp_path / "c.toml").sats == SatSettings(True, 2.0, 0.0)


def test_sat_track_list_round_trip(tmp_path):
    cfg = Config(path=tmp_path / "c.toml")
    cfg.sats.track = ["ISS", "SO-50", "CAS-3H", 'odd "name"']
    cfg.save()
    assert Config.load(tmp_path / "c.toml").sats.track == ["ISS", "SO-50", "CAS-3H", 'odd "name"']
    (tmp_path / "c.toml").write_text('[sats]\ntrack = ["ISS", 27607, "", true]\n')
    assert Config.load(tmp_path / "c.toml").sats.track == ["ISS", "27607"]


# ── app ───────────────────────────────────────────────────────────────────────
async def test_app_shows_only_tracked_satellites(tmp_path):
    app = make_app(tmp_path)
    app.config.sats.track = ["SO-50", "ISS", "AO-123"]
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        assert [s.short_name for s in app.sats.catalog.satellites] == ["SO-50", "ISS"]
        assert set(app.sats.passes) <= {27607, 25544}
        assert app._sat_missing == ["AO-123"]  # not in the fixture catalog: the user is told
    # the saved copy keeps everything, so changing the list later needs no download
    assert len(S.load_cache(tmp_path / "sats.json").satellites) == 4


async def test_app_loads_satellites_and_tracker(tmp_path):
    http = FakeHttp()
    app = make_app(tmp_path, http=http)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        assert len(app.sats.catalog.satellites) == 4 and app.sats.passes
        assert (tmp_path / "sats.json").exists()
        panel = shown(app.query_one("#sats"))
        assert "TLEs" in panel and "old" in panel

        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, SatScreen)
        before = sum("satnogs" in url for url, _ in http.calls)
        await pilot.press("U")
        await settle(pilot, app)
        assert sum("satnogs" in url for url, _ in http.calls) > before

        await pilot.press("i")
        await pilot.pause()
        await pilot.press("end", *["backspace"] * 5, "6", "enter")
        await settle(pilot, app)
        assert app.sats.refresh_hours == 6 and Config.load(tmp_path / "config.toml").sats.tle_refresh_hours == 6

        await pilot.press("e")
        await pilot.pause()
        await pilot.press("end", *["backspace"] * 5, "0", "enter")
        await settle(pilot, app)
        assert app.config.sats.min_elevation == 0 and app.sats.min_el == 0


async def test_fresh_cache_skips_startup_fetch(tmp_path):
    seeded = await S.fetch_catalog(FakeHttp())
    seeded.fetched_at = S.utcnow() - timedelta(minutes=30)
    S.save_cache(seeded, tmp_path / "sats.json")
    http = FakeHttp()
    app = make_app(tmp_path, http=http)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        assert not any("celestrak" in url for url, _ in http.calls)
        assert len(app.sats.catalog.satellites) == 4


async def test_offline_keeps_saved_tles(tmp_path):
    seeded = await S.fetch_catalog(FakeHttp())
    seeded.fetched_at = S.utcnow() - timedelta(hours=5)
    S.save_cache(seeded, tmp_path / "sats.json")
    app = make_app(tmp_path, http=FakeHttp({"celestrak.org": FetchError("offline")}))
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        cat = app.sats.catalog
        assert cat.stale and cat.error == "offline" and len(cat.satellites) == 4
        assert "update failed" in shown(app.query_one("#sats"))


def test_sat_state_next_update():
    now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
    st = SatState(Catalog([], now), refresh_hours=3)
    assert st.next_update() == now + timedelta(hours=3)
