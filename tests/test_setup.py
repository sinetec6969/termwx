from __future__ import annotations

import io

import pytest
from rich.console import Console

from conftest import FakeHttp, FetchError, load
from termwx import __main__ as cli, setup_wizard, units as U
from termwx.api import awc, geo
from termwx.config import Config
from termwx.models import Location

CLT = Location("Charlotte", 35.2271, -80.8431, "North Carolina", "United States", "US", "America/New_York")
CLT_GEOCODE = {"results": [{"name": "Charlotte", "latitude": 35.22709, "longitude": -80.84313, "country_code": "US",
                            "admin1": "North Carolina", "country": "United States", "timezone": "America/New_York"}]}


def script(*answers):
    """An `ask` that replays answers, then behaves like a closed stdin."""
    it = iter(answers)

    def ask(_prompt: str) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return ask


async def wizard(tmp_path, *answers, cfg: Config | None = None, http=None) -> tuple[int, Config, str]:
    cfg = cfg or Config(path=tmp_path / "config.toml")
    out = io.StringIO()
    code = await setup_wizard.run(cfg, http or FakeHttp({"geocoding-api": CLT_GEOCODE}), script(*answers),
                                  Console(file=out, width=120, color_system=None))
    return code, cfg, out.getvalue()


async def test_zip_code_auto_airport_and_defaults(tmp_path):
    #                               ZIP     confirm units airport  4 prefs
    code, _, out = await wizard(tmp_path, "28202", "", "", "", "", "", "", "")
    assert code == 0 and "← auto picks this" in out and "KCLT" in out
    back = Config.load(tmp_path / "config.toml")
    assert back.location.name == "Charlotte" and back.location.timezone == "America/New_York"
    assert back.units == U.IMPERIAL and back.aviation_station == ""
    assert "aviation_station" not in (tmp_path / "config.toml").read_text()
    assert back.desktop_notify and back.bell and back.animate and back.visitor


async def test_pinned_airport_by_number_and_by_icao(tmp_path):
    rows, auto = await awc.nearby_stations(FakeHttp(), CLT.lat, CLT.lon)
    _, cfg, _ = await wizard(tmp_path, "28202", "", "", "2", "n", "n", "n", "n")
    assert cfg.aviation_station == rows[1][0].icao
    assert (cfg.desktop_notify, cfg.bell, cfg.animate, cfg.visitor) == (False, False, False, False)
    _, cfg, out = await wizard(tmp_path, "28202", "", "", "KZZZ", "9", "kjqf", "", "", "", "")
    assert cfg.aviation_station == "KJQF" and "KZZZ isn't in" in out and "Pick a number" in out


async def test_coordinates_work_offline(tmp_path):
    offline = FakeHttp({"geocoding-api": FetchError("offline"), "stationinfo": FetchError("offline")})
    code, cfg, out = await wizard(tmp_path, "35.2,-80.8", "c", "", "", "", "", http=offline)
    assert code == 0 and (cfg.location.lat, cfg.location.lon) == (35.2, -80.8)
    assert cfg.units == U.METRIC and cfg.aviation_station == "" and "Couldn't look up airports" in out


async def test_search_with_several_matches_and_no_match(tmp_path):
    names = [loc.label for loc in await geo.search(FakeHttp(), "Denver")]  # the fixture has several matches
    assert len(names) > 1
    #                                 no match  search  pick  units airport 4 prefs
    _, cfg, out = await wizard(tmp_path, "Q", "Denver", "2", "", "", "", "", "", "", http=FakeHttp())
    assert cfg.location.label == names[1] and "No place matches “Q”" in out and "  2. " in out


async def test_rerun_keeps_everything(tmp_path):
    cfg = Config(path=tmp_path / "config.toml", location=CLT, units=U.METRIC, aviation_station="KJQF", bell=False)
    code, cfg, _ = await wizard(tmp_path, *[""] * 7, cfg=cfg)
    assert code == 0
    back = Config.load(tmp_path / "config.toml")
    assert back.location.same_place(CLT) and back.units == U.METRIC and back.aviation_station == "KJQF"
    assert back.bell is False and back.animate is True
    _, cfg, _ = await wizard(tmp_path, "", "", "auto", "", "", "", "", cfg=back)
    assert cfg.aviation_station == ""


async def test_quitting_saves_nothing(tmp_path):
    code, _, out = await wizard(tmp_path, "28202", "")  # input ends mid-way
    assert code == 1 and "nothing was saved" in out and not (tmp_path / "config.toml").exists()

    def interrupt(_prompt):
        raise KeyboardInterrupt

    out = io.StringIO()
    code = await setup_wizard.run(Config(path=tmp_path / "config.toml"), FakeHttp(), interrupt, Console(file=out))
    assert code == 130 and not (tmp_path / "config.toml").exists()


async def test_nearby_stations_sorted_metar_only_with_auto_pick():
    rows, auto = await awc.nearby_stations(FakeHttp(), CLT.lat, CLT.lon)
    assert len(rows) == 5 and auto == "KCLT"
    assert [r[1] for r in rows] == sorted(r[1] for r in rows)
    assert all(st.has_metar for st, _, _ in rows) and "KPYG" not in [st.icao for st, _, _ in rows]
    assert awc.pick_station(awc.parse_stations(load("awc_stationinfo.json")), CLT.lat, CLT.lon)[0].icao == auto


def test_cli_dispatches_setup_and_version(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(setup_wizard, "main", lambda argv: seen.append(argv) or 7)
    assert cli.main(["setup"]) == 7 and seen == [[]]
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.startswith("termwx ")
