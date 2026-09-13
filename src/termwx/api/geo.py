"""Location: IP auto-detect and Open-Meteo geocoding search."""

from __future__ import annotations

import re

from ..models import US_STATE_ABBR, Location
from .http import FetchError, Http

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

US_STATES = {abbr: name for name, abbr in US_STATE_ABBR.items()} | {"PR": "Puerto Rico"}

_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$")


def parse_coords(text: str) -> Location | None:
    m = _COORDS.match(text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return Location(name=f"{lat:.3f},{lon:.3f}", lat=lat, lon=lon)


def parse_geocode(data: dict) -> list[Location]:
    out = []
    for r in data.get("results") or []:
        out.append(
            Location(
                name=r.get("name", ""),
                lat=float(r["latitude"]),
                lon=float(r["longitude"]),
                region=r.get("admin1", "") or "",
                country=r.get("country", "") or "",
                country_code=r.get("country_code", "") or "",
                timezone=r.get("timezone", "") or "",
            )
        )
    return out


def _split_query(text: str) -> tuple[str, str]:
    """'Charlotte, NC' -> ('Charlotte', 'NC'). Open-Meteo only matches the name part."""
    if "," in text:
        name, _, qual = text.partition(",")
        return name.strip(), qual.strip()
    return text.strip(), ""


def _matches_qualifier(loc: Location, qual: str) -> bool:
    q = qual.lower()
    region = loc.region.lower()
    if len(q) <= 2:  # state or country code: "NC", "FR"
        return US_STATES.get(qual.upper(), "").lower() == region or q == loc.country_code.lower()
    return q in region or q in loc.country.lower()


async def search(http: Http, text: str, count: int = 10) -> list[Location]:
    coords = parse_coords(text)
    if coords:
        return [coords]
    name, qual = _split_query(text)
    if len(name) < 2:
        return []
    data, _ = await http.get_json(
        GEOCODE_URL, {"name": name, "count": count, "language": "en", "format": "json"}, cache=False
    )
    results = parse_geocode(data)
    if qual:
        filtered = [r for r in results if _matches_qualifier(r, qual)]
        results = filtered or results
    return results


async def autolocate(http: Http) -> Location:
    try:
        d, _ = await http.get_json("https://ipinfo.io/json", cache=False)
        lat, lon = (float(x) for x in d["loc"].split(","))
        return Location(
            name=d.get("city") or "Here",
            lat=lat,
            lon=lon,
            region=d.get("region", ""),
            country_code=d.get("country", ""),
            timezone=d.get("timezone", ""),
        )
    except (FetchError, KeyError, ValueError):
        d, _ = await http.get_json("https://ipapi.co/json/", cache=False)
        return Location(
            name=d.get("city") or "Here",
            lat=float(d["latitude"]),
            lon=float(d["longitude"]),
            region=d.get("region", ""),
            country=d.get("country_name", ""),
            country_code=d.get("country_code", ""),
            timezone=d.get("timezone", ""),
        )
