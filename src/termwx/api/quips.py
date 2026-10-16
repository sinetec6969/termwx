"""Random sign content for the sky visitor, from free no-key APIs.

Every source returns a `Quip` or raises; `fetch_quip` tries a few random
categories and falls back to a built-in line, so it never fails.
"""

from __future__ import annotations

import html
import random
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date

from rich.cells import get_character_cell_size

from ..models import Quip
from .http import FetchError, Http

MAX_CHARS = 240
ATTEMPTS = 3
JSON = {"Accept": "application/json"}


def _single_cell(ch: str) -> bool:
    if ch.isspace():
        return True
    # emoji planes are often drawn double-width even when wcwidth says 1
    if ord(ch) >= 0x1F000 or unicodedata.category(ch) in ("Cf", "Cs", "Co", "Mn", "Me"):
        return False
    return get_character_cell_size(ch) == 1


def clean(s: object) -> str:
    """Unescape HTML, drop tags and collapse whitespace. Characters that are not
    exactly one cell wide (emoji, CJK) are dropped so the sign grid stays aligned."""
    s = unicodedata.normalize("NFC", html.unescape(re.sub(r"<[^>]+>", "", str(s or ""))))
    return " ".join("".join(ch for ch in s if _single_cell(ch)).split())


async def _json(http: Http, url: str, **params):
    data, _ = await http.get_json(url, params=params or None, headers=JSON, cache=False)
    return data


# ── inspiration ──────────────────────────────────────────────────────────────
async def zenquotes(http: Http) -> Quip:
    q = (await _json(http, "https://zenquotes.io/api/random"))[0]
    return Quip("words to live by", clean(q["q"]), credit=clean(q["a"]))


async def dummyjson_quote(http: Http) -> Quip:
    q = await _json(http, "https://dummyjson.com/quotes/random")
    return Quip("words to live by", clean(q["quote"]), credit=clean(q["author"]))


async def advice(http: Http) -> Quip:
    q = await _json(http, "https://api.adviceslip.com/advice")
    return Quip("free advice", clean(q["slip"]["advice"]))


# ── facts & trivia ───────────────────────────────────────────────────────────
async def useless_fact(http: Http) -> Quip:
    q = await _json(http, "https://uselessfacts.jsph.pl/api/v2/facts/random", language="en")
    return Quip("fun fact", clean(q["text"]))


async def cat_fact(http: Http) -> Quip:
    q = await _json(http, "https://catfact.ninja/fact")
    return Quip("cat fact", clean(q["fact"]))


async def dog_fact(http: Http) -> Quip:
    q = await _json(http, "https://dogapi.dog/api/v2/facts", limit=1)
    return Quip("dog fact", clean(q["data"][0]["attributes"]["body"]))


async def on_this_day(http: Http, today: date | None = None) -> Quip:
    d = today or date.today()
    q = await _json(http, f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{d:%m}/{d:%d}")
    event = random.choice(q["selected"])
    return Quip(f"on this day in {event['year']}", clean(event["text"]), credit="Wikipedia")


async def trivia(http: Http) -> Quip:
    q = (await _json(http, "https://opentdb.com/api.php", amount=1, type="boolean"))["results"][0]
    return Quip("true or false?", clean(q["question"]), reveal=clean(q["correct_answer"]) + "!")


# ── food ─────────────────────────────────────────────────────────────────────
async def recipe(http: Http) -> Quip:
    meal = (await _json(http, "https://www.themealdb.com/api/json/v1/1/random.php"))["meals"][0]
    ingredients = [clean(meal.get(f"strIngredient{i}")).lower() for i in range(1, 21)]
    ingredients = [i for i in ingredients if i]
    shown = ", ".join(ingredients[:6]) + (" & more" if len(ingredients) > 6 else "")
    where = clean(meal.get("strArea") or meal.get("strCountry"))
    return Quip("dinner idea", f"{clean(meal['strMeal'])}: {shown}", credit=f"{where} · TheMealDB" if where else "TheMealDB")


# ── news ─────────────────────────────────────────────────────────────────────
def parse_rss_titles(xml: str) -> list[str]:
    root = ET.fromstring(xml)
    return [t for t in (clean(item.findtext("title")) for item in root.iter("item")) if t]


async def _headline(http: Http, url: str, credit: str) -> Quip:
    titles = parse_rss_titles(await http.get_text(url))
    return Quip("breaking news", random.choice(titles[:5]), credit=credit)


async def npr_news(http: Http) -> Quip:
    return await _headline(http, "https://feeds.npr.org/1001/rss.xml", "NPR")


async def bbc_news(http: Http) -> Quip:
    return await _headline(http, "https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World")


# ── jokes ────────────────────────────────────────────────────────────────────
async def dad_joke(http: Http) -> Quip:
    return Quip("dad joke", clean((await _json(http, "https://icanhazdadjoke.com/"))["joke"]))


async def joke_api(http: Http) -> Quip:
    q = await _json(http, "https://v2.jokeapi.dev/joke/Misc,Programming,Pun,Spooky,Christmas", blacklistFlags="racist,sexist")
    if q.get("error"):
        raise ValueError("jokeapi error")
    if q["type"] == "twopart":
        return Quip("joke", clean(q["setup"]), reveal=clean(q["delivery"]))
    return Quip("joke", clean(q["joke"]))


async def official_joke(http: Http) -> Quip:
    q = await _json(http, "https://official-joke-api.appspot.com/random_joke")
    return Quip("joke", clean(q["setup"]), reveal=clean(q["punchline"]))


async def chuck_norris(http: Http) -> Quip:
    q = await _json(http, "https://api.chucknorris.io/jokes/random")
    if "explicit" in q.get("categories", []):
        raise ValueError("explicit")
    return Quip("chuck norris fact", clean(q["value"]))


async def kanye(http: Http) -> Quip:
    return Quip("kanye says", clean((await _json(http, "https://api.kanye.rest"))["quote"]), credit="Kanye West")


SOURCES = {
    "inspiration": [zenquotes, dummyjson_quote, advice],
    "facts": [useless_fact, cat_fact, dog_fact, on_this_day, trivia],
    "food": [recipe],
    "news": [npr_news, bbc_news],
    "jokes": [dad_joke, joke_api, official_joke, chuck_norris, kanye],
}

FALLBACK = [
    Quip("fun fact", "A cloud can weigh more than a million pounds, and it still just floats there."),
    Quip("fun fact", "Lightning is about five times hotter than the surface of the sun."),
    Quip("words to live by", "There is no such thing as bad weather, only unsuitable clothing.", credit="Alfred Wainwright"),
    Quip("joke", "What did one raindrop say to the other?", reveal="Two's company, three's a cloud."),
    Quip("free advice", "The internet is down. Go outside and check the weather the old-fashioned way."),
]


async def fetch_quip(http: Http, rng: random.Random | None = None) -> Quip:
    rng = rng or random.Random()
    categories = rng.sample(list(SOURCES), k=min(ATTEMPTS, len(SOURCES)))
    for category in categories:
        source = rng.choice(SOURCES[category])
        try:
            q = await source(http)
        except (FetchError, LookupError, TypeError, ValueError, AttributeError, ET.ParseError):
            continue
        if q.text and len(q.text) + len(q.reveal) + len(q.credit) <= MAX_CHARS:
            return q
    return rng.choice(FALLBACK)
