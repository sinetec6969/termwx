from __future__ import annotations

import random
from datetime import date

import httpx
import pytest

from conftest import FakeHttp, FetchError
from termwx.api import quips
from termwx.api.http import Http
from termwx.app import TermWx
from termwx.config import Config
from termwx.models import Location, Quip
from termwx.scene import RUN_IN, SceneState, Visitor, render_scene
from termwx.widgets import ScenePanel

CLT = Location("Charlotte", 35.2271, -80.8431, "North Carolina", "United States", "US", "America/New_York")
JOKE = Quip("joke", "Why don't you find hippopotamuses hiding in trees?", reveal="They're really good at it.")
LONG = Quip("words to live by", " ".join(["plant a tree today"] * 10), credit="Someone Wise")


def plain(state, w, h, tick, visitor):
    text = render_scene(state, w, h, tick, visitor).plain
    lines = text.split("\n")
    assert len(lines) == h and all(len(line) == w for line in lines)
    return text


# ── scene ────────────────────────────────────────────────────────────────────
def test_visitor_runs_out_holds_sign_and_leaves():
    st = SceneState("clear", 1, True)
    v = Visitor(JOKE, start=100)
    running = plain(st, 44, 10, 100 + RUN_IN // 2, v)
    assert "o" in running.split("\n")[7] and "╭" not in running

    setup = plain(st, 44, 10, 100 + RUN_IN + v.reveal_at - 1, v)
    assert "hippopotamuses" in setup and "joke" in setup and "really good" not in setup
    assert "\\o/" in setup

    punch = plain(st, 44, 10, 100 + RUN_IN + v.reveal_at + 1, v)
    assert "hippopotamuses" in punch and "really good at it" in punch

    assert not v.done(100 + v.duration - 1) and v.done(100 + v.duration)
    after = plain(st, 44, 10, 100 + v.duration, v)
    assert after == plain(st, 44, 10, 100 + v.duration, None)


def test_long_sign_scrolls_to_the_end():
    st = SceneState("rain", 2, False, 30, 200, 100)
    v = Visitor(LONG)
    start = plain(st, 26, 10, RUN_IN + 5, v)
    end = plain(st, 26, 10, RUN_IN + v.hold - 1, v)
    assert "words to live by" in start and "Someone Wise" not in start
    assert "Someone Wise" in end


@pytest.mark.parametrize("quip", [JOKE, LONG, Quip("x" * 30, "y" * 200)])
def test_visitor_fits_any_size(quip):
    v = Visitor(quip)
    st = SceneState("snow", 3, True, 20, 90, 90)
    for w in (10, 16, 23, 24, 30, 60, 120):
        for h in (5, 6, 7, 10, 14):
            for tick in range(0, v.duration + 2, 5):
                plain(st, w, h, tick, v)


# ── sources ──────────────────────────────────────────────────────────────────
RSS = """<?xml version="1.0"?><rss><channel><title>News</title>
<item><title><![CDATA[Big storm &amp; more 🌧 rain]]></title></item>
<item><title>Second story</title></item></channel></rss>"""


def test_clean():
    assert quips.clean("<b>Tom &amp; Jerry</b>\n  rock 🎸 on 漢") == "Tom & Jerry rock on"
    assert quips.clean("café at 72°F ❤️ 🌧 👍🏽") == "café at 72°F ❤"
    assert quips.clean(None) == ""
    assert quips.parse_rss_titles(RSS) == ["Big storm & more rain", "Second story"]


async def test_parse_sources():
    http = FakeHttp({
        "zenquotes": [{"q": "Be kind.", "a": "Anon", "h": "<b>x</b>"}],
        "opentdb": {"results": [{"question": "The sun is a star &quot;G&quot; type.", "correct_answer": "True"}]},
        "themealdb": {"meals": [{"strMeal": "Stew", "strArea": "Irish", **{f"strIngredient{i}": f"Thing{i}" for i in range(1, 9)}, "strIngredient9": ""}]},
        "jokeapi": {"error": False, "type": "twopart", "setup": "Knock knock", "delivery": "Who's there?"},
        "onthisday": {"selected": [{"year": 1814, "text": "A song was written."}]},
        "npr.org": RSS,
        "chucknorris": {"categories": ["explicit"], "value": "nope"},
    })
    assert await quips.zenquotes(http) == Quip("words to live by", "Be kind.", credit="Anon")
    t = await quips.trivia(http)
    assert t.text == 'The sun is a star "G" type.' and t.reveal == "True!"
    r = await quips.recipe(http)
    assert r.text == "Stew: thing1, thing2, thing3, thing4, thing5, thing6 & more" and r.credit.startswith("Irish")
    j = await quips.joke_api(http)
    assert (j.text, j.reveal) == ("Knock knock", "Who's there?")
    d = await quips.on_this_day(http, date(2026, 9, 13))
    assert d.kind == "on this day in 1814" and "/09/13" in http.calls[-1][0]
    n = await quips.npr_news(http)
    assert n.text in ("Big storm & more rain", "Second story") and n.credit == "NPR"
    with pytest.raises(ValueError):
        await quips.chuck_norris(http)


async def test_fetch_quip_skips_failures_and_long_text():
    http = FakeHttp({"": FetchError("offline")})
    q = await quips.fetch_quip(http, random.Random(1))
    assert q in quips.FALLBACK

    long = FakeHttp({"": {"quote": "z" * 500, "author": "A", "slip": {"advice": "z" * 500}}})
    assert await quips.fetch_quip(long, random.Random(2)) in quips.FALLBACK


async def test_fetch_quip_returns_live_content(monkeypatch):
    monkeypatch.setattr(quips, "SOURCES", {"only": [quips.dad_joke]})
    http = FakeHttp({"icanhazdadjoke": {"joke": "Velcro… what a rip-off."}})
    assert await quips.fetch_quip(http) == Quip("dad joke", "Velcro… what a rip-off.")
    assert http.calls[0][0] == "https://icanhazdadjoke.com/"


async def test_http_get_text(tmp_path):
    http = Http(cache_dir=tmp_path)
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="<rss/>") if r.url.path == "/ok" else httpx.Response(503)
    ))
    assert await http.get_text("https://x.test/ok") == "<rss/>"
    with pytest.raises(FetchError):
        await http.get_text("https://x.test/down")
    await http.aclose()


# ── app ──────────────────────────────────────────────────────────────────────
async def test_v_key_sends_visitor(tmp_path, monkeypatch):
    monkeypatch.setattr(quips, "SOURCES", {"only": [quips.dad_joke]})
    http = FakeHttp({"icanhazdadjoke": {"joke": "I'm reading a book about anti-gravity."}})
    cfg = Config(path=tmp_path / "config.toml", location=CLT, desktop_notify=False, bell=False, animate=True)
    app = TermWx(cfg, http=http)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        scene = app.query_one(ScenePanel)
        await pilot.press("V")  # v opens the Aviation screen
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert scene.visitor and "anti-gravity" in scene.visitor.quip.text
        assert not scene.can_host_visitor  # one at a time
        await pilot.press("p")  # pausing the animation sends the visitor home
        assert scene.visitor is None


def test_config_visitor_roundtrip(tmp_path):
    cfg = Config(path=tmp_path / "c.toml", visitor=False)
    cfg.save()
    assert Config.load(tmp_path / "c.toml").visitor is False
    assert Config.load(tmp_path / "missing.toml").visitor is True
