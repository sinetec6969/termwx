"""Active amateur FM repeater and APRS digipeater satellites, with current TLEs.

The satellite list comes from the SatNOGS DB (as gpredict does): every
transmitter marked alive that is an FM voice repeater, or an AFSK APRS
digipeater, with an uplink. SatNOGS also supplies the names hams use (AO-91,
PO-101). TLEs come from CelesTrak — the `amateur` group in one request, then
CATNR lookups for anything not in that group.

CelesTrak updates GP data about every 2 hours and blocks clients that keep
re-downloading unchanged data, so startup only fetches when the saved copy is
at least MIN_AGE old; after that the app refetches every `tle_refresh_hours`.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..sat_models import APRS, FM, Catalog, Satellite, Transmitter
from .http import FetchError, Http, default_cache_dir

SATNOGS_TX_URL = "https://db.satnogs.org/api/transmitters/"
SATNOGS_SATS_URL = "https://db.satnogs.org/api/satellites/"
CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"

MIN_AGE = timedelta(hours=2)  # CelesTrak's update cadence: never fetch more often automatically
TX_MAX_AGE = timedelta(hours=24)  # the SatNOGS lists change rarely and are a few MB
RETRY_AFTER = timedelta(minutes=15)  # after a failed automatic update
MIN_HOURS, MAX_HOURS = 2.0, 168.0
SATNOGS_TEMP_IDS = 90000  # SatNOGS assigns 9xxxx before a real NORAD number exists
GEO_MEAN_MOTION = 1.5  # rev/day; below this it never makes a "pass"
CATNR_PARALLEL = 4

# FM-mode entries that aren't voice repeaters (linear transponders, command channels, …)
_NOT_REPEATER = re.compile(r"ssb|linear|digimode|mixed mode|dtmf|\bdsb\b|digipeater|command|telemetry", re.I)
_APRS = re.compile(r"aprs|digipeater|\bdigi\b", re.I)
_CTCSS = re.compile(r"(?:ctcss|pl)\s*(\d{2,3}(?:\.\d)?)|(\d{2,3}\.\d)\s*(?:hz\s*)?(?:pl|ctcss)\b", re.I)
_OSCAR_NAME = re.compile(r"^[A-Z]{1,3}-\d{1,3}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def cache_path() -> Path:
    return default_cache_dir() / "sats.json"


def clamp_hours(h: float) -> float:
    return max(MIN_HOURS, min(MAX_HOURS, float(h)))


# ── SatNOGS ───────────────────────────────────────────────────────────────────
def classify(tx: dict) -> str | None:
    """FM, APRS or None for one SatNOGS transmitter record."""
    if not tx.get("alive") or not tx.get("uplink_low") or not tx.get("downlink_low"):
        return None
    mode = (tx.get("mode") or "").upper()
    desc = tx.get("description") or ""
    if mode.startswith("AFSK") and _APRS.search(desc):
        return APRS
    if mode == "FM" and (tx.get("uplink_mode") or "FM").upper() == "FM" and not _NOT_REPEATER.search(desc):
        return FM
    return None


def norad_of(tx: dict) -> int | None:
    """The real NORAD number, following SatNOGS temporary IDs to their assigned one."""
    n = tx.get("norad_cat_id")
    if isinstance(n, int) and n >= SATNOGS_TEMP_IDS:
        n = tx.get("norad_follow_id")
    return n if isinstance(n, int) and 0 < n < SATNOGS_TEMP_IDS else None


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


def ctcss_of(desc: str) -> float | None:
    m = _CTCSS.search(desc)
    return float(m.group(1) or m.group(2)) if m else None


def select(tx_rows: list[dict], names: dict[str, str]) -> dict[int, tuple[str, list[Transmitter]]]:
    """{norad: (alias, transmitters)} for every satellite with an FM repeater or APRS digipeater."""
    out: dict[int, tuple[str, list[Transmitter]]] = {}
    for tx in tx_rows:
        kind, norad = classify(tx), norad_of(tx)
        if kind is None or norad is None:
            continue
        desc = (tx.get("description") or "").strip()
        alias, txs = out.setdefault(norad, (names.get(tx.get("sat_id") or "", ""), []))
        txs.append(Transmitter(kind, desc, tx["uplink_low"], tx["downlink_low"], tx.get("mode") or "", ctcss_of(desc)))
    return out


async def _satnogs(http: Http) -> dict[int, tuple[str, list[Transmitter]]]:
    tx_rows, _ = await http.get_json(SATNOGS_TX_URL, params={"format": "json", "status": "active"}, cache=False)
    if not isinstance(tx_rows, list) or not tx_rows:
        raise FetchError("SatNOGS returned no transmitters")
    try:
        sat_rows, _ = await http.get_json(SATNOGS_SATS_URL, params={"format": "json"}, cache=False)
    except FetchError:
        sat_rows = None  # names are a nicety; CelesTrak names still work
    return select(tx_rows, display_names(sat_rows) if isinstance(sat_rows, list) else {})


# ── CelesTrak ─────────────────────────────────────────────────────────────────
def parse_tles(text: str) -> dict[int, tuple[str, str, str]]:
    """Three-line element text → {norad: (name, line1, line2)}."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out: dict[int, tuple[str, str, str]] = {}
    for i, ln in enumerate(lines):
        if ln.startswith("1 ") and len(ln) >= 69 and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            try:
                norad = int(ln[2:7])
            except ValueError:
                continue
            prev = lines[i - 1].strip() if i > 0 else ""
            name = prev if prev and not prev.startswith(("1 ", "2 ")) else str(norad)
            out[norad] = (name, ln, lines[i + 1])
    return out


async def _tles(http: Http, wanted: set[int]) -> dict[int, tuple[str, str, str]]:
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

    for extra in await asyncio.gather(*(one(n) for n in sorted(wanted - found.keys()))):
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
    wanted: dict[int, tuple[str, list[Transmitter]]] | None = None
    tx_at = previous.tx_fetched_at if previous else None
    have_prev = bool(previous and previous.satellites)
    if force or not have_prev or tx_at is None or now - tx_at >= TX_MAX_AGE:
        try:
            wanted, tx_at = await _satnogs(http), now
        except FetchError:
            if not have_prev:
                raise
    if wanted is None:
        wanted = {s.norad: (s.alias, s.transmitters) for s in previous.satellites}

    tles = await _tles(http, set(wanted))
    sats = []
    for norad, (alias, txs) in wanted.items():
        if norad not in tles:
            continue
        name, l1, l2 = tles[norad]
        sat = Satellite(norad, name, l1, l2, txs, alias)
        try:
            if sat.mean_motion < GEO_MEAN_MOTION:
                continue
        except ValueError:
            continue
        sats.append(sat)
    sats.sort(key=lambda s: s.short_name.upper())
    return Catalog(sats, now, tx_at)


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
