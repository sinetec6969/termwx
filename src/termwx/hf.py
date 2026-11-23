"""HF propagation at the user's location: MUF from nearby ionosondes, LUF from D-layer absorption.

MUF(3000)F2 and foF2 come from the GIRO ionosonde network as republished by
prop.kc2g.com. The stations are sparse, so values are an inverse-distance
blend of recent, confidently-scaled soundings within ``MAX_STATION_KM``.

The LUF is an estimate: real LUF depends on power, antennas and noise. It
combines a quiet-day absorption model (sun angle and solar activity) with
NOAA's D-RAP "highest affected frequency", which captures flare (SWF) and
polar cap (PCA) absorption that the quiet model can't.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .drone import solar_elevation
from .models import HfConditions

MAX_STATION_KM = 2000.0
MAX_AGE = timedelta(minutes=90)
MIN_CONFIDENCE = 25.0  # KC2G "cs": 0–100 autoscaling confidence, -1 = manually scaled

# Quiet-day LUF: f ∝ sqrt(absorption), absorption ∝ (1 + 0.0037·R)·cos(0.881χ)^1.3
LUF_NOON = 4.3  # MHz with the sun overhead and R = 0
LUF_NIGHT = 1.8  # MHz floor once the D layer has gone

BANDS = [  # (label, lowest frequency in MHz)
    ("160", 1.8), ("80", 3.5), ("60", 5.3), ("40", 7.0), ("30", 10.1), ("20", 14.0),
    ("17", 18.068), ("15", 21.0), ("12", 24.89), ("10", 28.0), ("6", 50.0),
]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, a)))


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _time(s: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat((s or "").replace("Z", ""))
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def ionosonde_muf(stations: list, lat: float, lon: float, hf: HfConditions, now: datetime) -> None:
    """Fill MUF/foF2 from KC2G's stations.json, blending usable stations within range."""
    usable = []  # (distance km, weight, row, time)
    nearest = None
    for row in stations:
        st = row.get("station") or {}
        slat, slon = _float(st.get("latitude")), _float(st.get("longitude"))
        muf, t = _float(row.get("mufd")), _time(row.get("time"))
        cs = _float(row.get("cs"))
        if slat is None or slon is None or muf is None or t is None or cs is None:
            continue
        if not timedelta(minutes=-10) <= now - t <= MAX_AGE:
            continue
        if cs != -1 and cs < MIN_CONFIDENCE:
            continue
        slon = (slon + 180) % 360 - 180
        d = haversine_km(lat, lon, slat, slon)
        nearest = d if nearest is None else min(nearest, d)
        if d <= MAX_STATION_KM:
            conf = 0.8 if cs == -1 else cs / 100
            usable.append((d, conf / max(d, 100.0) ** 2, row, t))
    hf.nearest_station_km = nearest
    if not usable:
        return
    usable.sort(key=lambda u: u[0])
    total = sum(w for _, w, _, _ in usable)
    hf.muf = sum(w * float(r["mufd"]) for _, w, r, _ in usable) / total
    fof2 = [(w, _float(r.get("fof2"))) for _, w, r, _ in usable]
    fof2 = [(w, v) for w, v in fof2 if v is not None]
    if fof2:
        hf.fof2 = sum(w * v for w, v in fof2) / sum(w for w, _ in fof2)
    d, _, row, t = usable[0]
    hf.station = (row["station"].get("name") or row["station"].get("code") or "").split(",")[0].strip()
    hf.station_km = d
    hf.station_time = t
    hf.station_count = len(usable)


def parse_drap(text: str) -> tuple[dict[int, list[float]], list[int], dict[str, str]]:
    """SWPC drap_global_frequencies.txt → ({lat: [MHz per lon]}, lons, header fields)."""
    rows: dict[int, list[float]] = {}
    lons: list[int] = []
    meta: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            key, sep, val = line[1:].partition(":")
            if sep:
                meta[key.strip()] = val.strip()
        elif "|" in line:
            lat, vals = line.split("|", 1)
            rows[int(lat)] = [float(v) for v in vals.split()]
        elif line.strip() and not lons and set(line.strip()) != {"-"}:
            lons = [int(v) for v in line.split()]
    return rows, lons, meta


def drap_lookup(text: str, lat: float, lon: float, hf: HfConditions) -> None:
    rows, lons, meta = parse_drap(text)
    if not rows or not lons:
        raise ValueError("no D-RAP grid")
    glat = min(rows, key=lambda r: abs(r - lat))
    lon = (lon + 180) % 360 - 180
    i = min(range(len(lons)), key=lambda k: min(abs(lons[k] - lon), 360 - abs(lons[k] - lon)))
    hf.haf = rows[glat][i]
    xray = meta.get("X-RAY Message", "")
    proton = meta.get("Proton Message", "")
    hf.swf = bool(xray) and not xray.lower().startswith("normal")
    hf.pca = bool(proton) and not proton.lower().startswith("normal")


def sunspots_from_f107(f107: float) -> float:
    """Invert F10.7 = 63.7 + 0.728·R + 0.00089·R² (Tapping & Valdés)."""
    a, b, c = 0.00089, 0.728, 63.7 - f107
    disc = b * b - 4 * a * c
    return max(0.0, (-b + math.sqrt(disc)) / (2 * a)) if disc > 0 else 0.0


def quiet_luf(sun_elev: float, f107: float | None) -> float:
    r = sunspots_from_f107(f107) if f107 is not None else 100.0
    chi = math.radians(90 - sun_elev)
    c = math.cos(0.881 * chi) if chi < math.pi / 2 / 0.881 else 0.0
    return max(LUF_NIGHT, LUF_NOON * math.sqrt(1 + 0.0037 * r) * max(0.0, c) ** 0.65)


def finish(hf: HfConditions, lat: float, lon: float, f107: float | None, now: datetime) -> None:
    """Combine the quiet-day absorption estimate with D-RAP into the LUF."""
    base = quiet_luf(solar_elevation(lat, lon, now), f107)
    if hf.haf is not None and hf.haf > base:
        hf.luf, hf.luf_source = hf.haf, "PCA" if hf.pca else "SWF" if hf.swf else "D-RAP"
    else:
        hf.luf, hf.luf_source = base, ""


def open_bands(hf: HfConditions) -> list[tuple[str, bool | None]]:
    """Each amateur band and whether skywave is likely open (None: MUF unknown and above the LUF)."""
    out = []
    for label, f in BANDS:
        if hf.luf is not None and f < hf.luf:
            out.append((label, False))
        elif hf.muf is None:
            out.append((label, None))
        else:
            out.append((label, f <= hf.muf))
    return out
