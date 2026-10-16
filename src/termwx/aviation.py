"""Pure aviation calculations: geometry, flight categories, density altitude,
wind components, trends and hazard applicability. No I/O, no UI."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone

from .aviation_models import (
    CATEGORY_RANK,
    AviationHazard,
    IcingTurbulence,
    MetarReport,
    Pirep,
    TafForecast,
    TafPeriod,
    WindsAloftLevel,
)

EARTH_RADIUS_NM = 3440.065
MAX_STATION_NM = 75  # never auto-select an airport farther than this from the weather location
HAZARD_NEAR_NM = 50  # show advisories whose area comes within this distance
PIREP_RADIUS_NM = 100
PIREP_MAX_AGE_H = 3


def utcnow() -> datetime:
    """Current UTC time. A single seam so tests can pin the clock."""
    return datetime.now(timezone.utc)


# ── geometry ─────────────────────────────────────────────────────────────────
def _norm_lon(lon: float, ref: float) -> float:
    """Longitude shifted to within ±180° of `ref`, so shapes crossing the antimeridian stay contiguous."""
    return (lon - ref + 540) % 360 - 180 + ref


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def point_in_polygon(lat: float, lon: float, poly: list[tuple[float, float]]) -> bool:
    """Ray casting in lat/lon space. Fine for advisory areas a few hundred miles across."""
    if len(poly) < 3:
        return False
    pts = [(la, _norm_lon(lo, lon)) for la, lo in poly]
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        yi, xi = pts[i]
        yj, xj = pts[j]
        if (yi > lat) != (yj > lat):
            x_cross = xi + (lat - yi) * (xj - xi) / (yj - yi)
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def distance_to_shape_nm(lat: float, lon: float, poly: list[tuple[float, float]], closed: bool = True) -> float | None:
    """Shortest distance from the point to the shape's outline (0 inside a closed polygon)."""
    if not poly:
        return None
    if closed and point_in_polygon(lat, lon, poly):
        return 0.0
    # Local equirectangular projection around the point, in NM.
    k = math.cos(math.radians(lat))

    def xy(p: tuple[float, float]) -> tuple[float, float]:
        return ((_norm_lon(p[1], lon) - lon) * 60 * k, (p[0] - lat) * 60)

    pts = [xy(p) for p in poly]
    if len(pts) == 1:
        return math.hypot(*pts[0])
    segs = list(zip(pts, pts[1:]))
    if closed and pts[0] != pts[-1]:
        segs.append((pts[-1], pts[0]))
    best = math.inf
    for (x1, y1), (x2, y2) in segs:
        dx, dy = x2 - x1, y2 - y1
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / L))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


def region_for(lat: float, lon: float) -> str:
    """Which AWC advisory family covers a point: conus | alaska | hawaii | other."""
    if 51 <= lat <= 72 and (lon <= -129 or lon >= 170):
        return "alaska"
    if 18 <= lat <= 23 and -161 <= lon <= -154:
        return "hawaii"
    if 24 <= lat <= 50 and -126 <= lon <= -66:
        return "conus"
    return "other"


# ── flight category ──────────────────────────────────────────────────────────
def flight_category(ceiling_ft: int | None, visibility_sm: float | None) -> str:
    """FAA categories: LIFR <500 ft or <1 SM, IFR <1000 ft or <3 SM, MVFR ≤3000 ft or ≤5 SM."""
    if ceiling_ft is None and visibility_sm is None:
        return ""
    c = math.inf if ceiling_ft is None else ceiling_ft
    v = math.inf if visibility_sm is None else visibility_sm
    if c < 500 or v < 1:
        return "LIFR"
    if c < 1000 or v < 3:
        return "IFR"
    if c <= 3000 or v <= 5:
        return "MVFR"
    return "VFR"


def category_rank(cat: str) -> int:
    return CATEGORY_RANK.get(cat, -1)


def metar_category(m: MetarReport) -> str:
    """AWC's own category when supplied, otherwise derived from ceiling and visibility."""
    return m.flight_category or flight_category(m.ceiling_ft, m.visibility_sm)


# ── density altitude ─────────────────────────────────────────────────────────
def station_pressure_hpa(altimeter_hpa: float, elev_m: float) -> float:
    """Invert the altimeter-setting definition to get pressure at field elevation."""
    n = 0.190263
    return (altimeter_hpa**n - 8.417286e-5 * elev_m) ** (1 / n)


def pressure_altitude_ft(elev_ft: float, altimeter_hpa: float) -> float:
    pstn = station_pressure_hpa(altimeter_hpa, elev_ft / 3.28084)
    return 145366.45 * (1 - (pstn / 1013.25) ** 0.190284)


def density_altitude_ft(
    elev_ft: float, temp_c: float, altimeter_hpa: float, dewpoint_c: float | None = None
) -> float:
    """Density altitude from field elevation, temperature and altimeter setting.

    Uses the NWS formulation (station pressure + virtual temperature when the
    dew point is known, so humidity is accounted for).
    """
    p = station_pressure_hpa(altimeter_hpa, elev_ft / 3.28084)
    tk = temp_c + 273.15
    if dewpoint_c is not None:
        e = 6.1078 * 10 ** (7.5 * dewpoint_c / (237.3 + dewpoint_c))  # vapour pressure, hPa
        tk = tk / (1 - (e / p) * (1 - 0.622))
    tv_r = tk * 9 / 5  # Rankine
    p_inhg = p * 0.0295300
    return 145442.16 * (1 - (17.326 * p_inhg / tv_r) ** 0.235)


# ── wind ─────────────────────────────────────────────────────────────────────
def parse_magdec(text: str | None) -> float | None:
    """'07W' → -7.0, '02E' → 2.0 (east positive)."""
    if not text:
        return None
    s = str(text).strip().upper()
    try:
        if s[-1] in "EW":
            v = float(s[:-1])
            return -v if s[-1] == "W" else v
        return float(s)
    except (ValueError, IndexError):
        return None


def true_to_magnetic(deg_true: float, magdec: float | None) -> float:
    return deg_true if magdec is None else (deg_true - magdec) % 360


def wind_components(runway_heading: float, wind_dir: float, speed: float) -> tuple[float, float]:
    """(headwind, crosswind) in the wind's units.

    Headwind is negative for a tailwind; crosswind is positive when the wind
    comes from the right of the runway heading. Both angles must use the same
    reference (magnetic for runway numbers).
    """
    angle = math.radians((wind_dir - runway_heading + 540) % 360 - 180)
    return speed * math.cos(angle), speed * math.sin(angle)


def parse_runway_heading(text: str) -> int | None:
    """'18', 'RWY 18L', '36' → 180/360 (designators); '175' or '175°' → 175 (exact heading)."""
    s = text.upper().replace("RWY", "").replace("RUNWAY", "").replace("°", "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    rest = s[len(digits) :].strip()
    if not digits or rest not in ("", "L", "R", "C"):
        return None
    v = int(digits)
    if len(digits) <= 2:
        return v * 10 if 1 <= v <= 36 else None
    return v if 1 <= v <= 360 else None


# ── trends ───────────────────────────────────────────────────────────────────
def condition_trend(metars: list[MetarReport], count: int = 6) -> str:
    """'improving' | 'steady' | 'deteriorating' over the last few observations (newest first input)."""
    obs = [m for m in metars[:count] if metar_category(m)]
    if len(obs) < 2:
        return ""
    newest, oldest = obs[0], obs[-1]
    delta = category_rank(metar_category(newest)) - category_rank(metar_category(oldest))
    if delta > 0:
        return "deteriorating"
    if delta < 0:
        return "improving"
    # Same category: look for a large move in ceiling or visibility within it.
    c0, c1 = oldest.ceiling_ft, newest.ceiling_ft
    if c0 is not None and c1 is not None and c0 <= 5000:
        if c1 <= c0 * 0.6:
            return "deteriorating"
        if c1 >= c0 * 1.6:
            return "improving"
    v0, v1 = oldest.visibility_sm, newest.visibility_sm
    if v0 is not None and v1 is not None and min(v0, v1) < 6:
        if v1 <= v0 * 0.5:
            return "deteriorating"
        if v1 >= v0 * 2:
            return "improving"
    return "steady"


# ── TAF ──────────────────────────────────────────────────────────────────────
_MERGE_FIELDS = (
    "wind_dir", "wind_variable", "wind_speed_kt", "wind_gust_kt", "visibility_sm",
    "visibility_plus", "vert_vis_ft", "wx", "wind_shear",
)


def _merge(base: TafPeriod, change: TafPeriod) -> TafPeriod:
    """Apply a BECMG/TEMPO/PROB group's elements on top of the prevailing conditions."""
    merged = replace(base, start=change.start, end=change.end, change=change.change,
                     probability=change.probability, becmg_end=change.becmg_end)
    for name in _MERGE_FIELDS:
        v = getattr(change, name)
        if v not in (None, "", False):
            setattr(merged, name, v)
    if change.wind_speed_kt is not None:
        merged.wind_gust_kt = change.wind_gust_kt
        merged.wind_dir, merged.wind_variable = change.wind_dir, change.wind_variable
    if change.clouds or change.vert_vis_ft is not None:
        merged.clouds, merged.vert_vis_ft = change.clouds, change.vert_vis_ft
    if change.visibility_sm is not None:
        merged.visibility_plus = change.visibility_plus
    return merged


def taf_rows(taf: TafForecast) -> list[tuple[TafPeriod, TafPeriod]]:
    """[(as issued, effective conditions)] in time order.

    FM and the base period replace everything; BECMG, TEMPO and PROB only
    change the elements they mention, so the effective conditions (used for
    flight category) fill the rest from the prevailing forecast.
    """
    rows: list[tuple[TafPeriod, TafPeriod]] = []
    prevailing: TafPeriod | None = None
    for p in taf.periods:
        if p.change in ("BASE", "FM") or prevailing is None:
            eff = p
            prevailing = p
        else:
            eff = _merge(prevailing, p)
            if p.change == "BECMG":
                prevailing = replace(eff, change=prevailing.change)
        rows.append((p, eff))
    return rows


def period_category(p: TafPeriod) -> str:
    vis = p.visibility_sm
    if vis is None and p.visibility_plus:
        vis = 6.0
    return flight_category(p.ceiling_ft, vis)


def taf_outlook(taf: TafForecast, now: datetime) -> tuple[str, list[tuple[str, datetime]], list[tuple[TafPeriod, str]]]:
    """(category now, later prevailing category changes, worse temporary groups) for summaries."""
    rows = taf_rows(taf)
    prevailing = [(p, eff) for p, eff in rows if not p.is_temporary]
    current = ""
    for p, eff in prevailing:
        if p.start <= now:
            current = period_category(eff)
    if not current and prevailing:
        current = period_category(prevailing[0][1])
    changes: list[tuple[str, datetime]] = []
    last = current
    for p, eff in prevailing:
        if p.start > now:
            cat = period_category(eff)
            if cat and cat != last:
                changes.append((cat, p.start))
                last = cat
    temps = []
    for p, eff in rows:
        if p.is_temporary and p.end > now:
            cat = period_category(eff)
            base = next((period_category(e) for q, e in reversed(prevailing) if q.start <= p.start), current)
            if category_rank(cat) > category_rank(base):
                temps.append((p, cat))
    return current, changes, temps


# ── PIREPs ───────────────────────────────────────────────────────────────────
_INTENSITY = [
    ("EXTM", 4), ("SEV", 3), ("HVY", 3), ("MOD", 2), ("LGT", 1), ("TRC", 1), ("SMTH", 0), ("NEG", 0),
]


def intensity_level(code: str) -> int:
    """Strongest level mentioned in a range like 'LGT-MOD': NEG 0, LGT 1, MOD 2, SEV 3, EXTM 4."""
    c = (code or "").upper()
    best = -1
    for part in c.replace("/", "-").split("-"):
        for key, lvl in _INTENSITY:
            if part.startswith(key):
                best = max(best, lvl)
                break
    return best


def worst(conds: list[IcingTurbulence]) -> IcingTurbulence | None:
    return max(conds, key=lambda c: intensity_level(c.intensity), default=None)


def pirep_significance(p: Pirep) -> int:
    """3 severe/extreme turb or ice (or urgent), 2 moderate, 1 low ceilings/weather, 0 routine."""
    level = max((intensity_level(c.intensity) for c in p.turbulence + p.icing), default=-1)
    if level >= 3 or p.urgent:
        return 3
    if level == 2:
        return 2
    low_cloud = any(
        c.cover in ("BKN", "OVC", "OVX") and c.base_ft is not None and c.base_ft < 3000 for c in p.clouds
    )
    if low_cloud or (p.visibility_sm is not None and p.visibility_sm < 5) or p.wx.strip():
        return 1
    return 0


def sort_pireps(pireps: list[Pirep], now: datetime) -> list[Pirep]:
    def key(p: Pirep):
        age_min = (now - p.obs_time).total_seconds() / 60
        dist = p.distance_nm if p.distance_nm is not None else 999
        return (-pirep_significance(p), round(dist / 25), age_min)

    return sorted(pireps, key=key)


def recent_pireps(pireps: list[Pirep], now: datetime, max_age_h: float = PIREP_MAX_AGE_H,
                  radius_nm: float = PIREP_RADIUS_NM) -> list[Pirep]:
    keep = [
        p for p in pireps
        if (now - p.obs_time).total_seconds() <= max_age_h * 3600
        and (p.distance_nm is None or p.distance_nm <= radius_nm)
    ]
    return sort_pireps(keep, now)


# ── hazards ──────────────────────────────────────────────────────────────────
def locate_hazards(hazards: list[AviationHazard], lat: float, lon: float) -> None:
    for h in hazards:
        if not h.polygon:
            h.contains_point, h.distance_nm = False, None
            continue
        closed = not h.is_line
        h.contains_point = closed and point_in_polygon(lat, lon, h.polygon)
        h.distance_nm = distance_to_shape_nm(lat, lon, h.polygon, closed=closed)


_KIND_ORDER = {"CONVECTIVE SIGMET": 0, "SIGMET": 0, "INTL SIGMET": 0, "CWA": 1, "AIRMET": 2, "G-AIRMET": 2}


def relevant_hazards(hazards: list[AviationHazard], now: datetime, near_nm: float = HAZARD_NEAR_NM) -> list[AviationHazard]:
    """Advisories in effect that cover, or come within `near_nm` of, the located point.

    Expired products are always dropped, even from an offline cache.
    """
    out = []
    for h in hazards:
        if h.valid_to is not None and h.valid_to <= now:
            continue
        if h.contains_point or (h.distance_nm is not None and h.distance_nm <= near_nm):
            out.append(h)
    return sorted(out, key=lambda h: (not h.contains_point, _KIND_ORDER.get(h.kind, 3), h.distance_nm or 0))


# ── winds aloft ──────────────────────────────────────────────────────────────
def freezing_level_ft(levels: list[WindsAloftLevel]) -> int | None:
    """Interpolated altitude where the forecast temperature crosses 0 °C, if bracketed."""
    pts = [(lv.altitude_ft, lv.temp_c) for lv in sorted(levels, key=lambda lv: lv.altitude_ft) if lv.temp_c is not None]
    for (a0, t0), (a1, t1) in zip(pts, pts[1:]):
        if t0 > 0 >= t1:
            return round(a0 + (a1 - a0) * t0 / (t0 - t1), -2)
    return None


def direction_change(a: int | None, b: int | None) -> int:
    if a is None or b is None:
        return 0
    return abs((b - a + 540) % 360 - 180)
