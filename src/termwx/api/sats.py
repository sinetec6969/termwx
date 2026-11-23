"""Active amateur FM repeater and APRS digipeater satellites, with current TLEs.

The satellite list comes from the SatNOGS DB (as gpredict does): every
transmitter marked alive that is an FM voice repeater, or an AFSK APRS
digipeater, with an uplink. SatNOGS also supplies the names hams use (AO-91,
PO-101). TLEs come from CelesTrak — the `amateur` group in one request, then
CATNR lookups for anything not in that group, then SatNOGS' own TLE copy for
anything CelesTrak doesn't carry (e.g. new six-digit, Alpha-5 catalog numbers).

`[sats] track` in the config narrows the tracker to a list of satellites.

CelesTrak updates GP data about every 2 hours and blocks clients that keep
re-downloading unchanged data, so startup only fetches when the saved copy is
at least MIN_AGE old; after that the app refetches every `tle_refresh_hours`.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..sat_models import APRS, FM, Catalog, Satellite, Transmitter
from .http import FetchError, Http, default_cache_dir

SATNOGS_TX_URL = "https://db.satnogs.org/api/transmitters/"
SATNOGS_SATS_URL = "https://db.satnogs.org/api/satellites/"
SATNOGS_TLE_URL = "https://db.satnogs.org/api/tle/"
CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"

MIN_AGE = timedelta(hours=2)  # CelesTrak's update cadence: never fetch more often automatically
TX_MAX_AGE = timedelta(hours=24)  # the SatNOGS lists change rarely and are a few MB
RETRY_AFTER = timedelta(minutes=15)  # after a failed automatic update
MIN_HOURS, MAX_HOURS = 2.0, 168.0
SATNOGS_TEMP_IDS = range(90000, 100000)  # SatNOGS assigns 9xxxx before a real NORAD number exists
GEO_MEAN_MOTION = 1.5  # rev/day; below this it never makes a "pass"
CATNR_PARALLEL = 4

# FM-mode entries that aren't voice repeaters (linear transponders, command channels, …)
_NOT_REPEATER = re.compile(r"ssb|linear|digimode|mixed mode|dtmf|\bdsb\b|digipeater|command|telemetry", re.I)
_APRS = re.compile(r"aprs|digipeater|\bdigi\b", re.I)
_CTCSS = re.compile(r"(?:ctcss|pl)\s*(\d{2,3}(?:\.\d)?)|(\d{2,3}\.\d)\s*(?:hz\s*)?(?:pl|ctcss)\b", re.I)
_OSCAR_NAME = re.compile(r"^[A-Z]{1,3}-\d{1,3}$")
_DESIGNATOR = re.compile(r"^[A-Z]{1,6}-\d{1,4}[A-Z]?$")  # SO-50, CAS-3H, XW-2H


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def cache_path() -> Path:
    return default_cache_dir() / "sats.json"


def clamp_hours(h: float) -> float:
    return max(MIN_HOURS, min(MAX_HOURS, float(h)))


# ── SatNOGS ───────────────────────────────────────────────────────────────────
_FM = ("FM", "FMN")  # SatNOGS lists some repeaters as narrow FM


def classify(tx: dict) -> str | None:
    """FM, APRS or None for one SatNOGS transmitter record."""
    if not tx.get("alive") or not tx.get("uplink_low") or not tx.get("downlink_low"):
        return None
    mode = (tx.get("mode") or "").upper()
    desc = tx.get("description") or ""
    if mode.startswith("AFSK") and _APRS.search(desc):
        return APRS
    if mode in _FM and (tx.get("uplink_mode") or "FM").upper() in _FM and not _NOT_REPEATER.search(desc):
        return FM
    return None


def norad_of(tx: dict) -> int | None:
    """The real NORAD number, following SatNOGS temporary IDs to their assigned one."""
    n = tx.get("norad_cat_id")
    if n in SATNOGS_TEMP_IDS:
        n = tx.get("norad_follow_id")
    return n if isinstance(n, int) and n > 0 and n not in SATNOGS_TEMP_IDS else None


def display_names(sat_rows: list[dict]) -> dict[str, str]:
    """SatNOGS satellite records → {sat_id: name}, preferring an OSCAR number (AO-91, PO-101)."""
    out: dict[str, str] = {}
    for row in sat_rows:
        sid, name = row.get("sat_id"), (row.get("name") or "").strip()
        if not sid or not name:
            continue
        aliases = [a.strip() for a in (row.get("names") or "").split(",")]
        out[sid] = next((a for a in [name, *aliases] if _OSCAR_NAME.match(a)), name)
    return out


def all_names(sat_rows: list[dict]) -> dict[str, list[str]]:
    """SatNOGS satellite records → {sat_id: [name, *aliases]}."""
    out: dict[str, list[str]] = {}
    for row in sat_rows:
        sid = row.get("sat_id")
        names = [(row.get("name") or "").strip(), *(a.strip() for a in re.split(r"[,\r\n]+", row.get("names") or ""))]
        # aliases are sometimes space-separated ("CAS-3H XW-2H"): pull out designator-looking words too
        names += [w for n in names if " " in n for w in n.split() if _DESIGNATOR.match(w)]
        if sid:
            out[sid] = [n for n in dict.fromkeys(names) if n]
    return out


def ctcss_of(desc: str) -> float | None:
    m = _CTCSS.search(desc)
    return float(m.group(1) or m.group(2)) if m else None


@dataclass
class Wanted:
    """A satellite worth tracking, before its TLE is known."""

    alias: str
    transmitters: list[Transmitter] = field(default_factory=list)
    sat_id: str = ""
    names: list[str] = field(default_factory=list)


def select(
    tx_rows: list[dict], names: dict[str, str], aliases: dict[str, list[str]] | None = None
) -> dict[int, Wanted]:
    """{norad: Wanted} for every satellite with an FM repeater or APRS digipeater."""
    out: dict[int, Wanted] = {}
    for tx in tx_rows:
        kind, norad = classify(tx), norad_of(tx)
        if kind is None or norad is None:
            continue
        desc = (tx.get("description") or "").strip()
        sid = tx.get("sat_id") or ""
        w = out.setdefault(norad, Wanted(names.get(sid, ""), [], sid, list((aliases or {}).get(sid, []))))
        w.transmitters.append(
            Transmitter(kind, desc, tx["uplink_low"], tx["downlink_low"], tx.get("mode") or "", ctcss_of(desc))
        )
    return out


async def _satnogs(http: Http) -> dict[int, Wanted]:
    tx_rows, _ = await http.get_json(SATNOGS_TX_URL, params={"format": "json", "status": "active"}, cache=False)
    if not isinstance(tx_rows, list) or not tx_rows:
        raise FetchError("SatNOGS returned no transmitters")
    try:
        sat_rows, _ = await http.get_json(SATNOGS_SATS_URL, params={"format": "json"}, cache=False)
    except FetchError:
        sat_rows = None  # names are a nicety; CelesTrak names still work
    if not isinstance(sat_rows, list):
        return select(tx_rows, {})
    return select(tx_rows, display_names(sat_rows), all_names(sat_rows))


# ── CelesTrak ─────────────────────────────────────────────────────────────────
_ALPHA5 = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # A=10 … Z=33; I and O are skipped


def catalog_number(field: str) -> int | None:
    """TLE catalog field → number, including Alpha-5 (`A1101` = 101101) for numbers past 99999."""
    field = field.strip()
    try:
        if field[:1] in _ALPHA5:
            return (_ALPHA5.index(field[0]) + 10) * 10000 + int(field[1:])
        return int(field)
    except (ValueError, IndexError):
        return None


def parse_tles(text: str) -> dict[int, tuple[str, str, str]]:
    """Three-line element text → {norad: (name, line1, line2)}."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out: dict[int, tuple[str, str, str]] = {}
    for i, ln in enumerate(lines):
        if ln.startswith("1 ") and len(ln) >= 69 and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            norad = catalog_number(ln[2:7])
            if norad is None:
                continue
            prev = lines[i - 1].strip() if i > 0 else ""
            name = prev if prev and not prev.startswith(("1 ", "2 ")) else str(norad)
            out[norad] = (name, ln, lines[i + 1])
    return out


async def _satnogs_tle(http: Http, norad: int, sat_id: str) -> dict[int, tuple[str, str, str]]:
    """SatNOGS' copy of a TLE (from Space-Track), for satellites CelesTrak doesn't carry."""
    try:
        rows, _ = await http.get_json(SATNOGS_TLE_URL, params={"format": "json", "sat_id": sat_id}, cache=False)
    except FetchError:
        return {}
    for row in rows if isinstance(rows, list) else []:
        name = (row.get("tle0") or "").removeprefix("0 ").strip()
        found = parse_tles(f"{name}\n{row.get('tle1', '')}\n{row.get('tle2', '')}")
        if found:  # keyed by the real number even if SatNOGS files it under a temporary ID
            return {norad: next(iter(found.values()))}
    return {}


async def _tles(http: Http, wanted: dict[int, str]) -> dict[int, tuple[str, str, str]]:
    """TLEs for {norad: SatNOGS sat_id}."""
    found = parse_tles(await http.get_text(CELESTRAK_URL, params={"GROUP": "amateur", "FORMAT": "tle"}))
    if not found:
        raise FetchError("CelesTrak returned no TLEs")
    gate = asyncio.Semaphore(CATNR_PARALLEL)

    async def one(norad: int) -> dict[int, tuple[str, str, str]]:
        async with gate:
            try:
                return parse_tles(await http.get_text(CELESTRAK_URL, params={"CATNR": str(norad), "FORMAT": "tle"}))
            except FetchError:
                return {}  # decayed or not catalogued yet

    for extra in await asyncio.gather(*(one(n) for n in sorted(wanted.keys() - found.keys()))):
        found.update(extra)

    async def fallback(norad: int) -> dict[int, tuple[str, str, str]]:
        async with gate:
            return await _satnogs_tle(http, norad, wanted[norad])

    missing = sorted(n for n in wanted.keys() - found.keys() if wanted[n])
    for extra in await asyncio.gather(*(fallback(n) for n in missing)):
        found.update(extra)
    return found


# ── catalog ───────────────────────────────────────────────────────────────────
def is_due(cat: Catalog | None, now: datetime, refresh_hours: float, startup: bool = False) -> bool:
    """Whether an automatic update should run now (forced updates skip this check).

    At startup anything at least MIN_AGE old is refreshed; afterwards, every
    `refresh_hours` (never more often than MIN_AGE).
    """
    if cat is None or cat.fetched_at is None:
        return True
    age = now - cat.fetched_at
    return age >= (MIN_AGE if startup else max(MIN_AGE, timedelta(hours=refresh_hours)))


async def fetch_catalog(http: Http, previous: Catalog | None = None, force: bool = False) -> Catalog:
    """Download a fresh catalog. Raises FetchError when CelesTrak can't be reached.

    The SatNOGS lists are reused from `previous` while under a day old (unless
    forced), and also when SatNOGS is down.
    """
    now = utcnow()
    wanted: dict[int, Wanted] | None = None
    tx_at = previous.tx_fetched_at if previous else None
    have_prev = bool(previous and previous.satellites)
    # saved by an older termwx without SatNOGS ids and aliases: refresh the list now
    old_format = have_prev and not any(s.sat_id for s in previous.satellites)
    if force or not have_prev or old_format or tx_at is None or now - tx_at >= TX_MAX_AGE:
        try:
            wanted, tx_at = await _satnogs(http), now
        except FetchError:
            if not have_prev:
                raise
    if wanted is None:
        wanted = {s.norad: Wanted(s.alias, s.transmitters, s.sat_id, s.names) for s in previous.satellites}

    tles = await _tles(http, {n: w.sat_id for n, w in wanted.items()})
    sats = []
    for norad, w in wanted.items():
        if norad not in tles:
            continue
        name, l1, l2 = tles[norad]
        sat = Satellite(norad, name, l1, l2, w.transmitters, w.alias, w.sat_id, w.names)
        try:
            if sat.mean_motion < GEO_MEAN_MOTION:
                continue
        except ValueError:
            continue
        sats.append(sat)
    sats.sort(key=lambda s: s.short_name.upper())
    return Catalog(sats, now, tx_at)


# ── tracking list ─────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", s.upper())


def matches(sat: Satellite, entry: str) -> bool:
    """`entry` is a NORAD number or any of the satellite's names (case, spaces and dashes ignored)."""
    key = _norm(entry)
    if not key:
        return False
    if key.isdigit():
        return int(key) == sat.norad
    return any(_norm(n) == key for n in (sat.short_name, sat.alias, sat.name, *sat.names) if n)


def tracked(cat: Catalog, track: list[str]) -> Catalog:
    """The catalog narrowed to the `track` list, in that order (an empty list keeps everything)."""
    if not track:
        return cat
    sats: list[Satellite] = []
    seen: set[int] = set()
    for entry in track:
        for sat in cat.satellites:
            if sat.norad in seen or not matches(sat, entry):
                continue
            seen.add(sat.norad)
            # tracked as "CAS-3H": show it as CAS-3H rather than SatNOGS' "LILACSAT 2"
            named = next((n for n in sat.names if _norm(n) == _norm(entry)), "")
            sats.append(replace(sat, alias=named) if named and _norm(named) != _norm(sat.short_name) else sat)
    return Catalog(sats, cat.fetched_at, cat.tx_fetched_at, cat.stale, cat.error)


def not_found(cat: Catalog, track: list[str]) -> list[str]:
    """Entries in `track` that match nothing in the catalog."""
    return [e for e in track if not any(matches(s, e) for s in cat.satellites)]


def load_cache(path: Path | None = None) -> Catalog | None:
    path = path or cache_path()
    try:
        d = json.loads(path.read_text())
        return Catalog(
            [Satellite.from_dict(s) for s in d["satellites"]],
            datetime.fromisoformat(d["fetched_at"]),
            datetime.fromisoformat(d["tx_fetched_at"]) if d.get("tx_fetched_at") else None,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cache(cat: Catalog, path: Path | None = None) -> None:
    path = path or cache_path()
    data = {
        "fetched_at": cat.fetched_at.isoformat() if cat.fetched_at else None,
        "tx_fetched_at": cat.tx_fetched_at.isoformat() if cat.tx_fetched_at else None,
        "satellites": [s.to_dict() for s in cat.satellites],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        pass
