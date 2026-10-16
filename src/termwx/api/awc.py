"""Aviation Weather Center Data API (https://aviationweather.gov/data/api/).

Only documented query parameters are used (the API rejects anything else),
JSON is requested wherever the endpoint offers it, and AWC's own decoding is
normalized into `aviation_models` here, at the API boundary. HTTP 204 means a
valid request with no data and is treated as an empty result, not a failure.

Please keep request volume low: AWC allows 100 requests/minute in total.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from importlib import resources

from .. import aviation as A
from ..aviation_models import (
    AviationHazard,
    AviationStation,
    CloudLayer,
    HazardSet,
    IcingTurbulence,
    MetarReport,
    Pirep,
    Runway,
    StationChoice,
    TafForecast,
    TafPeriod,
    WindsAloft,
    WindsAloftLevel,
)
from .http import FetchError, Http

BASE = "https://aviationweather.gov/api/data"
URLS = {
    name: f"{BASE}/{name}"
    for name in ("stationinfo", "airport", "metar", "taf", "pirep", "airsigmet", "isigmet", "gairmet", "airmet", "cwa", "windtemp")
}
METAR_HOURS = 6  # one request gives the latest report plus the trend history
_ICAO = re.compile(r"^[A-Z0-9]{3,4}$")


# ── field helpers ────────────────────────────────────────────────────────────
def _ts(v) -> datetime | None:
    """Unix seconds or an ISO-ish string → aware UTC datetime."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc)
    s = str(v).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(s), timezone.utc)
        except ValueError:
            return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _num(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    n = _num(v)
    return None if n is None else int(round(n))


def _vis(v) -> tuple[float | None, bool]:
    """AWC visibility: 3, 0.25, "10+", "6+", "1/2", "1 1/2", "P6SM" → (statute miles, greater-than)."""
    if v is None or v == "":
        return None, False
    if isinstance(v, (int, float)):
        return float(v), False
    s = str(v).strip().upper().replace("SM", "")
    plus = s.endswith("+") or s.startswith("P")
    s = s.strip("+P").strip()
    try:
        total = 0.0
        for part in s.split():
            if "/" in part:
                n, d = part.split("/", 1)
                total += float(n) / float(d)
            else:
                total += float(part)
        return total, plus
    except (ValueError, ZeroDivisionError):
        return None, False


def _wind(wdir) -> tuple[int | None, bool]:
    if isinstance(wdir, str):
        if wdir.strip().upper() == "VRB":
            return None, True
        wdir = _num(wdir)
    return (None, False) if wdir is None else (int(wdir), False)


def _clouds(layers) -> list[CloudLayer]:
    out = []
    for c in layers or []:
        cover = (c.get("cover") or "").upper()
        if not cover:
            continue
        out.append(CloudLayer(cover, _int(c.get("base")), _int(c.get("top")), (c.get("type") or "").upper()))
    return out


def _coords(points) -> list[tuple[float, float]]:
    out = []
    for p in points or []:
        lat, lon = _num(p.get("lat")), _num(p.get("lon"))
        if lat is not None and lon is not None:
            out.append((lat, lon))
    return out


def _clean_ident(ident: str) -> str:
    return ident.strip().upper()


# ── stations ─────────────────────────────────────────────────────────────────
def parse_station(d: dict) -> AviationStation:
    sites = [str(s).upper() for s in d.get("siteType") or []]
    return AviationStation(
        icao=d.get("icaoId") or d.get("id") or "",
        name=(d.get("site") or d.get("name") or "").strip(),
        lat=float(d["lat"]),
        lon=float(d["lon"]),
        elev_m=_num(d.get("elev")),
        state=d.get("state") or "",
        country=d.get("country") or "",
        has_metar="METAR" in sites,
        has_taf="TAF" in sites,
    )


def parse_stations(data) -> list[AviationStation]:
    out = []
    for d in data or []:
        try:
            st = parse_station(d)
        except (KeyError, TypeError, ValueError):
            continue
        if st.icao:
            out.append(st)
    return out


def pick_station(stations: list[AviationStation], lat: float, lon: float,
                 max_nm: float = A.MAX_STATION_NM) -> tuple[AviationStation, float] | None:
    """Nearest METAR site within `max_nm`. A site that also issues a TAF wins
    unless it is more than 8 NM farther away, since it gives a complete picture."""
    best = None
    for st in stations:
        if not st.has_metar:
            continue
        d = A.distance_nm(lat, lon, st.lat, st.lon)
        if d > max_nm:
            continue
        score = d - (8 if st.has_taf else 0)
        if best is None or score < best[0]:
            best = (score, st, d)
    return (best[1], best[2]) if best else None


def _bbox(lat: float, lon: float, radius_nm: float) -> str:
    dlat = radius_nm / 60
    dlon = radius_nm / (60 * max(0.2, math.cos(math.radians(lat))))
    la0, la1 = max(-90.0, lat - dlat), min(90.0, lat + dlat)
    lo0, lo1 = max(-180.0, lon - dlon), min(180.0, lon + dlon)
    return f"{la0:.2f},{lo0:.2f},{la1:.2f},{lo1:.2f}"


def parse_airport(data, station: AviationStation) -> None:
    """Add magnetic variation and runways from AWC airport data (FAA source) to the station."""
    for d in data or []:
        if (d.get("icaoId") or "").upper() != station.icao.upper():
            continue
        station.magdec = A.parse_magdec(d.get("magdec"))
        if _num(d.get("elev")) is not None:  # airport reference point elevation (FAA), metres
            station.elev_m = _num(d.get("elev"))
        rwys = []
        for r in d.get("runways") or []:
            ident = (r.get("id") or "").strip()
            if not ident:
                continue
            length = width = None
            m = re.match(r"^(\d+)x(\d+)$", str(r.get("dimension") or ""))
            if m:
                length, width = int(m.group(1)), int(m.group(2))
            rwys.append(Runway(ident, length, width, r.get("surface") or ""))
        station.runways = rwys
        return


async def fetch_airport_details(http: Http, station: AviationStation) -> None:
    """Best effort: runways and magnetic variation. Never raises."""
    try:
        data, _ = await http.get_json(URLS["airport"], {"ids": station.icao, "format": "json"})
        parse_airport(data, station)
    except (FetchError, KeyError, TypeError, ValueError):
        pass


async def lookup_station(http: Http, ident: str) -> AviationStation | None:
    data, _ = await http.get_json(URLS["stationinfo"], {"ids": _clean_ident(ident), "format": "json"})
    return next((s for s in parse_stations(data) if s.icao.upper() == _clean_ident(ident)), None)


async def choose_station(http: Http, lat: float, lon: float, explicit: str | None = None) -> StationChoice:
    """Resolve the aviation station for a location: the explicit override, or the
    nearest suitable METAR site. Run on location/station change only."""
    if explicit:
        ident = _clean_ident(explicit)
        if not _ICAO.match(ident):
            return StationChoice(None, "explicit", error=f"“{explicit}” is not an ICAO identifier")
        try:
            st = await lookup_station(http, ident)
        except FetchError as e:
            return StationChoice(None, "explicit", error=str(e), retryable=True)
        if st is None:
            return StationChoice(None, "explicit", error=f"{ident} not found in AWC station data")
        await fetch_airport_details(http, st)
        return StationChoice(st, "explicit", A.distance_nm(lat, lon, st.lat, st.lon), A.bearing_deg(lat, lon, st.lat, st.lon))

    picked = None
    for radius in (30, A.MAX_STATION_NM):
        try:
            data, _ = await http.get_json(URLS["stationinfo"], {"bbox": _bbox(lat, lon, radius), "format": "json"})
        except FetchError as e:
            return StationChoice(None, "auto", error=str(e), retryable=True)
        picked = pick_station(parse_stations(data), lat, lon)
        if picked:
            break
    if not picked:
        return StationChoice(None, "auto", error=f"no METAR station within {A.MAX_STATION_NM} NM")
    st, dist = picked
    await fetch_airport_details(http, st)
    return StationChoice(st, "auto", dist, A.bearing_deg(lat, lon, st.lat, st.lon))


# ── METAR ────────────────────────────────────────────────────────────────────
def parse_metar(d: dict) -> MetarReport | None:
    obs = _ts(d.get("obsTime")) or _ts(d.get("reportTime"))
    raw = (d.get("rawOb") or "").strip()
    if obs is None or not raw:
        return None
    wdir, variable = _wind(d.get("wdir"))
    vis, plus = _vis(d.get("visib"))
    rtype = (d.get("metarType") or "").upper() or ("SPECI" if raw.startswith("SPECI") else "METAR")
    return MetarReport(
        station=d.get("icaoId") or "",
        obs_time=obs,
        raw=raw,
        report_type=rtype,
        temp_c=_num(d.get("temp")),
        dewp_c=_num(d.get("dewp")),
        wind_dir=wdir,
        wind_variable=variable,
        wind_speed_kt=_int(d.get("wspd")),
        wind_gust_kt=_int(d.get("wgst")),
        visibility_sm=vis,
        visibility_plus=plus,
        altimeter_hpa=_num(d.get("altim")),
        slp_hpa=_num(d.get("slp")),
        pressure_tendency_hpa=_num(d.get("presTend")),
        clouds=_clouds(d.get("clouds")),
        vert_vis_ft=_int(d.get("vertVis")),
        wx=(d.get("wxString") or "").strip(),
        flight_category=(d.get("fltCat") or "").upper(),
        name=d.get("name") or "",
        lat=_num(d.get("lat")),
        lon=_num(d.get("lon")),
        elev_m=_num(d.get("elev")),
    )


def parse_metars(data) -> list[MetarReport]:
    """Newest first, duplicates (same time and type) removed."""
    seen, out = set(), []
    for d in data or []:
        m = parse_metar(d)
        if m and (m.obs_time, m.report_type, m.station) not in seen:
            seen.add((m.obs_time, m.report_type, m.station))
            out.append(m)
    out.sort(key=lambda m: m.obs_time, reverse=True)
    return out


async def fetch_metars(http: Http, icao: str, hours: float = METAR_HOURS) -> tuple[list[MetarReport], bool]:
    data, stale = await http.get_json(URLS["metar"], {"ids": icao, "format": "json", "hours": hours})
    return parse_metars(data), stale


# ── TAF ──────────────────────────────────────────────────────────────────────
def _icg_turb(items) -> list[IcingTurbulence]:
    out = []
    for it in items or []:
        kind = (it.get("var") or "").upper()
        lvl = _int(it.get("intensity"))
        out.append(IcingTurbulence(kind, str(lvl) if lvl is not None else "", _int(it.get("minAlt")), _int(it.get("maxAlt"))))
    return out


def parse_taf_period(f: dict) -> TafPeriod | None:
    start, end = _ts(f.get("timeFrom")), _ts(f.get("timeTo"))
    if start is None or end is None:
        return None
    change = (f.get("fcstChange") or "").upper() or "BASE"
    prob = _int(f.get("probability"))
    if change == "PROB" and prob is None:
        prob = 30
    wdir, variable = _wind(f.get("wdir"))
    vis, plus = _vis(f.get("visib"))
    ws = ""
    if f.get("wshearHgt") is not None:
        ws = f"WS{int(f['wshearHgt']) // 100:03d}/{_int(f.get('wshearDir')) or 0:03d}{_int(f.get('wshearSpd')) or 0:02d}KT"
    return TafPeriod(
        start=start,
        end=end,
        change=change,
        probability=prob,
        becmg_end=_ts(f.get("timeBec")),
        wind_dir=wdir,
        wind_variable=variable,
        wind_speed_kt=_int(f.get("wspd")),
        wind_gust_kt=_int(f.get("wgst")),
        visibility_sm=vis,
        visibility_plus=plus,
        clouds=_clouds(f.get("clouds")),
        vert_vis_ft=_int(f.get("vertVis")),
        wx=(f.get("wxString") or "").strip(),
        wind_shear=ws,
        icing_turbulence=_icg_turb(f.get("icgTurb")),
        not_decoded=(f.get("notDecoded") or "").strip(),
    )


_ORDER = {"BASE": 0, "FM": 1, "BECMG": 2, "TEMPO": 3, "PROB": 3}


def parse_taf(data, icao: str | None = None) -> TafForecast | None:
    """The most recent TAF for the station; periods in time order."""
    cands = [d for d in data or [] if not icao or (d.get("icaoId") or "").upper() == icao.upper()]
    if not cands:
        return None
    cands.sort(key=lambda d: (int(d.get("mostRecent") or 0), _ts(d.get("issueTime")) or datetime.min.replace(tzinfo=timezone.utc)))
    d = cands[-1]
    issue = _ts(d.get("issueTime")) or _ts(d.get("bulletinTime"))
    vf, vt = _ts(d.get("validTimeFrom")), _ts(d.get("validTimeTo"))
    if issue is None or vf is None or vt is None:
        return None
    periods = [p for p in (parse_taf_period(f) for f in d.get("fcsts") or []) if p]
    periods.sort(key=lambda p: (p.start, _ORDER.get(p.change, 4)))
    raw = (d.get("rawTAF") or "").strip()
    remarks = (d.get("remarks") or "").upper()
    head = raw.split(" ", 3)[:3]
    return TafForecast(
        station=d.get("icaoId") or "",
        issue_time=issue,
        valid_from=vf,
        valid_to=vt,
        raw=raw,
        periods=periods,
        amended="AMD" in remarks or "AMD" in head,
        corrected="COR" in remarks or "COR" in head,
        name=d.get("name") or "",
    )


async def fetch_taf(http: Http, icao: str) -> tuple[TafForecast | None, bool]:
    data, stale = await http.get_json(URLS["taf"], {"ids": icao, "format": "json"})
    return parse_taf(data, icao), stale


# ── PIREPs ───────────────────────────────────────────────────────────────────
def _conditions(d: dict, prefix: str, kind: str) -> list[IcingTurbulence]:
    out = []
    for n in (1, 2):
        intensity = (d.get(f"{prefix}Int{n}") or "").strip()
        if not intensity:
            continue
        out.append(
            IcingTurbulence(
                kind=kind,
                intensity=intensity.upper(),
                base_ft=_hundreds(d.get(f"{prefix}Bas{n}")),
                top_ft=_hundreds(d.get(f"{prefix}Top{n}")),
                type=(d.get(f"{prefix}Type{n}") or "").upper(),
                freq=(d.get(f"{prefix}Freq{n}") or "").upper(),
            )
        )
    return out


def _hundreds(v) -> int | None:
    """PIREP layer heights are in hundreds of feet."""
    n = _int(v)
    return None if n is None else n * 100


def parse_pirep(d: dict, ref: tuple[float, float] | None = None) -> Pirep | None:
    obs = _ts(d.get("obsTime")) or _ts(d.get("receiptTime"))
    raw = (d.get("rawOb") or "").strip()
    if obs is None or not raw:
        return None
    lat, lon = _num(d.get("lat")), _num(d.get("lon"))
    vis, _ = _vis(d.get("visib"))
    clouds = _clouds(d.get("clouds"))  # feet MSL
    for c in clouds:
        if c.cover in ("CLR", "IMC") and not c.base_ft:
            c.base_ft = c.top_ft = None
    p = Pirep(
        obs_time=obs,
        raw=raw,
        report_type=d.get("pirepType") or ("Urgent PIREP" if " UUA " in f" {raw} " else "PIREP"),
        aircraft=(d.get("acType") or "").strip(),
        lat=lat,
        lon=lon,
        altitude_ft=_hundreds(d.get("fltLvl")),
        phase=(d.get("fltLvlType") or "").upper(),
        turbulence=_conditions(d, "tb", "TURB"),
        icing=_conditions(d, "icg", "ICE"),
        clouds=clouds,
        visibility_sm=vis,
        wx=(d.get("wxString") or "").strip(),
        temp_c=_num(d.get("temp")),
        wind_dir=_int(d.get("wdir")),
        wind_speed_kt=_int(d.get("wspd")),
        braking=(d.get("brkAction") or "").strip(),
    )
    if ref and lat is not None and lon is not None:
        p.distance_nm = A.distance_nm(ref[0], ref[1], lat, lon)
        p.bearing = A.bearing_deg(ref[0], ref[1], lat, lon)
    return p


def parse_pireps(data, ref: tuple[float, float] | None = None) -> list[Pirep]:
    return [p for p in (parse_pirep(d, ref) for d in data or []) if p]


async def fetch_pireps(http: Http, station: AviationStation) -> tuple[list[Pirep], bool]:
    # The API documents `distance` without a unit; asking for a little more than
    # 100 covers 100 NM either way and the list is trimmed locally.
    params = {"id": station.icao, "distance": round(A.PIREP_RADIUS_NM * 1.16), "age": A.PIREP_MAX_AGE_H, "format": "json"}
    data, stale = await http.get_json(URLS["pirep"], params)
    return parse_pireps(data, (station.lat, station.lon)), stale


# ── SIGMET / AIRMET / CWA ────────────────────────────────────────────────────
_HAZARD_NAMES = {
    "CONVECTIVE": "TS", "CONV": "TS", "TURB": "TURB", "ICE": "ICE", "IFR": "IFR", "MTN OBSCN": "MTN OBSC",
    "MT_OBSC": "MTN OBSC", "MTN_OBSC": "MTN OBSC", "MT-OBSC": "MTN OBSC", "MTN-OBSC": "MTN OBSC", "TURB-HI": "TURB", "TURB-LO": "TURB", "LLWS": "LLWS",
    "SFC_WND": "SFC WIND", "SFC-WIND": "SFC WIND", "FZLVL": "FZLVL", "M_FZLVL": "FZLVL", "PCPN": "PCPN",
}
_SIGMET_NAME = re.compile(r"\bSIGMET\s+([A-Z]+\s+\d+)\b")


def _hazard_code(h: str) -> str:
    h = (h or "").upper().strip()
    return _HAZARD_NAMES.get(h, h)


def _move(direction, speed) -> str:
    d, s = _int(direction), _int(speed)
    if d is not None and s:
        return f"MOV FROM {d:03d}° {s}KT"
    if isinstance(direction, str) and direction and s:
        return f"MOV {direction} {s}KT"
    return ""


def parse_airsigmets(data) -> list[AviationHazard]:
    """Domestic (CONUS) SIGMETs, convective and non-convective."""
    out = []
    for d in data or []:
        raw = (d.get("rawAirSigmet") or "").strip()
        hazard = (d.get("hazard") or "").upper()
        convective = hazard.startswith("CONV")
        if convective:
            name, kind = f"CONVECTIVE SIGMET {d.get('seriesId') or ''}".strip(), "CONVECTIVE SIGMET"
        else:
            m = _SIGMET_NAME.search(raw)
            name, kind = f"SIGMET {m.group(1) if m else d.get('seriesId') or ''}".strip(), "SIGMET"
        out.append(
            AviationHazard(
                kind=kind,
                name=name,
                hazard=_hazard_code(hazard),
                valid_from=_ts(d.get("validTimeFrom")),
                valid_to=_ts(d.get("validTimeTo")),
                raw=raw,
                polygon=_coords(d.get("coords")),
                base_ft=_int(d.get("altitudeLow1") if d.get("altitudeLow1") is not None else d.get("altitudeLo1")),
                top_ft=_int(d.get("altitudeHi1")),
                severity=str(d.get("severity") or ""),
                qualifier=d.get("qualifier") or "",
                movement=_move(d.get("movementDir"), d.get("movementSpd")),
                issuer=d.get("icaoId") or "",
            )
        )
    return out


def parse_isigmets(data) -> list[AviationHazard]:
    """International-format SIGMETs (Alaska, Hawaii, oceanic and foreign FIRs)."""
    out = []
    for d in data or []:
        out.append(
            AviationHazard(
                kind="INTL SIGMET",
                name=f"SIGMET {d.get('firId') or d.get('icaoId') or ''} {d.get('seriesId') or ''}".strip(),
                hazard=_hazard_code(d.get("hazard")),
                valid_from=_ts(d.get("validTimeFrom")),
                valid_to=_ts(d.get("validTimeTo")),
                raw=(d.get("rawSigmet") or "").strip(),
                polygon=_coords(d.get("coords")),
                is_line=(d.get("geom") or "").upper() == "LINE",
                base_ft=_int(d.get("base")),
                top_ft=_int(d.get("top")),
                qualifier=d.get("qualifier") or "",
                movement=_move(d.get("dir"), d.get("spd")),
                issuer=d.get("firName") or d.get("icaoId") or "",
            )
        )
    return out


def _fl(v) -> tuple[int | None, str]:
    """G-AIRMET levels are hundreds of feet ("080") or "SFC"/"FZL"."""
    if v is None or v == "":
        return None, ""
    s = str(v).strip().upper()
    if s.isdigit():
        return int(s) * 100, ""
    return (0, "SFC") if s == "SFC" else (None, s)


def parse_gairmets(data) -> list[AviationHazard]:
    """G-AIRMETs for the lower 48 (the text AIRMETs they replaced are obsolete)."""
    out = []
    for d in data or []:
        geom = (d.get("geometryType") or d.get("geom") or "").upper()
        base, base_text = _fl(d.get("base"))
        if base_text == "FZL" and (d.get("fzlbase") or d.get("fzltop")):
            base_text = f"FZL {d.get('fzlbase') or '?'}-{d.get('fzltop') or '?'}"
        top, _ = _fl(d.get("top"))
        if d.get("hazard", "").upper() == "FZLVL" and d.get("level"):
            base, _ = _fl(d.get("level"))
        product = (d.get("product") or "").upper()
        expire = _ts(d.get("expireTime"))
        valid = _ts(d.get("validTime"))
        detail = d.get("due_to") or ""
        raw_lines = [
            f"G-AIRMET {product} {d.get('tag') or ''} — {d.get('hazard') or ''}".strip(),
            f"Snapshot valid {valid:%d %H%MZ}" if valid else "",
            f"Issued {_ts(d.get('issueTime')):%d %H%MZ}" if _ts(d.get("issueTime")) else "",
            f"Expires {expire:%d %H%MZ}" if expire else "",
            f"Severity {d['severity']}" if d.get("severity") else "",
            f"Due to {detail}" if detail else "",
            f"Base {d.get('base')}  Top {d.get('top')}" if d.get("base") or d.get("top") else "",
            f"Freezing level {d.get('fzlbase')}-{d.get('fzltop')}" if d.get("fzlbase") or d.get("fzltop") else "",
            f"Level {d.get('level')}" if d.get("level") else "",
            "Graphical product — decoded attributes shown (no plain-language text is issued).",
        ]
        out.append(
            AviationHazard(
                kind="G-AIRMET",
                name=f"G-AIRMET {product}".strip(),
                hazard=_hazard_code(d.get("hazard")),
                valid_from=valid,
                valid_to=expire,
                raw="\n".join(line for line in raw_lines if line),
                polygon=_coords(d.get("coords")),
                is_line=geom == "LINE",
                base_ft=base,
                top_ft=top,
                base_text=base_text,
                severity=(d.get("severity") or "").upper(),
                qualifier=detail,
                issuer="KKCI",
            )
        )
    return out


def parse_ak_airmets(geojson) -> list[AviationHazard]:
    """Alaska AIRMETs (GeoJSON: the JSON form carries no geometry)."""
    out = []
    for f in (geojson or {}).get("features") or []:
        p = f.get("properties") or {}
        geom = f.get("geometry") or {}
        poly: list[tuple[float, float]] = []
        coords = geom.get("coordinates") or []
        if geom.get("type") == "Polygon" and coords:
            poly = [(float(c[1]), float(c[0])) for c in coords[0]]
        hazard = _hazard_code(p.get("hazard"))
        zone = p.get("zone") or p.get("region") or ""
        out.append(
            AviationHazard(
                kind="AIRMET",
                name=f"AIRMET {zone}".strip(),
                hazard=hazard,
                valid_from=_ts(p.get("validTimeFrom")),
                valid_to=_ts(p.get("validTimeTo")),
                raw=(p.get("rawAirmet") or p.get("rawText") or f"AIRMET {p.get('hazard', '')} — {zone}").strip(),
                polygon=poly,
                base_ft=_hundreds(p.get("base")),
                top_ft=_hundreds(p.get("top")),
                issuer="PAWU",
            )
        )
    return out


def parse_cwas(data) -> list[AviationHazard]:
    out = []
    for d in data or []:
        raw = (d.get("rawText") or d.get("cwaText") or "").strip()
        mv = re.search(r"\bMOV\s+(?:FROM\s+)?\d{3}\d{2,3}KT|\bMOV\s+[NSEW]{1,3}\s+\d+\s*KT|\bSTNRY\b", raw)
        out.append(
            AviationHazard(
                kind="CWA",
                name=f"CWA {d.get('cwsu') or ''} {d.get('seriesId') or ''}".strip(),
                hazard=_hazard_code(d.get("hazard")),
                valid_from=_ts(d.get("validTimeFrom")),
                valid_to=_ts(d.get("validTimeTo")),
                raw=raw,
                polygon=_coords(d.get("coords")),
                is_line=(d.get("geom") or "").upper() == "LINE",
                base_ft=_hundreds(d.get("base")),
                top_ft=_hundreds(d.get("top")),
                qualifier=d.get("qualifier") or "",
                movement=mv.group(0) if mv else "",
                issuer=d.get("name") or d.get("cwsu") or "",
            )
        )
    return out


HAZARD_SOURCES = {
    "conus": [("SIGMET", "airsigmet", {}, parse_airsigmets), ("G-AIRMET", "gairmet", {}, parse_gairmets),
              ("CWA", "cwa", {}, parse_cwas)],
    "alaska": [("SIGMET", "isigmet", {}, parse_isigmets), ("AIRMET", "airmet", {"format": "geojson"}, parse_ak_airmets),
               ("CWA", "cwa", {}, parse_cwas)],
    "hawaii": [("SIGMET", "isigmet", {}, parse_isigmets)],
    "other": [("SIGMET", "isigmet", {}, parse_isigmets)],
}


async def fetch_hazards(http: Http, lat: float, lon: float) -> tuple[HazardSet, bool]:
    """SIGMETs, G-AIRMETs/AIRMETs and CWAs for the point's region, each fetched
    independently. Raises FetchError only when every source failed."""
    sources = HAZARD_SOURCES[A.region_for(lat, lon)]

    async def one(url_key: str, params: dict):
        return await http.get_json(URLS[url_key], {"format": "json", **params})

    results = await asyncio.gather(*(one(key, params) for _, key, params, _ in sources), return_exceptions=True)
    hs, stale = HazardSet(), False
    for (label, _, _, parser), res in zip(sources, results):
        if isinstance(res, BaseException):
            if not isinstance(res, FetchError):
                raise res
            hs.errors.append(f"{label}: {res}")
            continue
        data, s = res
        stale = stale or s
        try:
            hs.items.extend(parser(data))
            hs.sources.append(label)
        except (KeyError, TypeError, ValueError, IndexError) as e:
            hs.errors.append(f"{label}: unexpected format ({e})")
    if not hs.sources:
        raise FetchError("; ".join(hs.errors) or "hazards unavailable")
    A.locate_hazards(hs.items, lat, lon)
    return hs, stale


# ── winds aloft ──────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def fd_sites() -> dict[str, dict]:
    """Coordinates of winds-aloft sites (see scripts/build_fd_sites.py)."""
    try:
        return json.loads(resources.files("termwx").joinpath("data/fd_sites.json").read_text())
    except (OSError, ValueError):
        return {}


def nearest_fd_site(lat: float, lon: float, sites: dict[str, dict] | None = None) -> tuple[str, dict, float] | None:
    sites = fd_sites() if sites is None else sites
    best = None
    for sid, s in sites.items():
        d = A.distance_nm(lat, lon, s["lat"], s["lon"])
        if best is None or d < best[2]:
            best = (sid, s, d)
    return best


def decode_fd_group(group: str, altitude_ft: int) -> WindsAloftLevel | None:
    """One FD group: DDSS, DDSS±TT or DDSSTT (temps above 24,000 ft are always negative).

    9900 means light and variable; direction codes 51-86 encode speeds of 100-199 kt.
    """
    g = group.strip()
    if len(g) < 4 or not g[:4].isdigit():
        return None
    dd, ss = int(g[:2]), int(g[2:4])
    temp = None
    rest = g[4:]
    if rest:
        try:
            temp = int(rest) if rest[0] in "+-" else -int(rest)
        except ValueError:
            temp = None
    if dd == 99 and ss == 0:
        return WindsAloftLevel(altitude_ft, None, 0, temp)
    if dd >= 51:
        dd -= 50
        ss += 100
    return WindsAloftLevel(altitude_ft, dd * 10 % 360 or 360, ss, temp)


_FD_TIMES = re.compile(r"DATA BASED ON (\d{6})Z.*?VALID (\d{6})Z\s+FOR USE (\d{4}-\d{4})Z", re.S)


def _ddhhmm(s: str, ref: datetime) -> datetime | None:
    """A DDHHMM stamp near `ref` (handles month rollover)."""
    try:
        day, hh, mm = int(s[:2]), int(s[2:4]), int(s[4:6])
    except ValueError:
        return None
    best = None
    for dm in (-1, 0, 1):
        y, m = ref.year, ref.month + dm
        if m < 1:
            y, m = y - 1, 12
        elif m > 12:
            y, m = y + 1, 1
        try:
            cand = datetime(y, m, day, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            continue
        if best is None or abs((cand - ref).total_seconds()) < abs((best - ref).total_seconds()):
            best = cand
    return best


def parse_windtemp(text: str, now: datetime | None = None) -> dict[str, WindsAloft]:
    """Parse the FD (winds and temperatures aloft) bulletin by column position."""
    now = now or A.utcnow()
    based = valid = use_from = use_to = None
    use = ""
    m = _FD_TIMES.search(text or "")
    if m:
        based, valid, use = _ddhhmm(m.group(1), now), _ddhhmm(m.group(2), now), m.group(3) + "Z"
        use_from, use_to = _use_window(m.group(3), valid)
    out: dict[str, WindsAloft] = {}
    columns: list[tuple[int, int, int]] = []  # (start, end, altitude)
    for line in (text or "").splitlines():
        if line.startswith("FT"):
            columns = []
            prev_end = 3
            for mm in re.finditer(r"\d{4,5}", line):
                columns.append((prev_end, mm.end(), int(mm.group())))
                prev_end = mm.end()
            continue
        if not columns or not re.match(r"^[A-Z0-9]{3}\b", line):
            continue
        site = line[:3]
        levels = []
        for start, end, alt in columns:
            lv = decode_fd_group(line[start:end], alt)
            if lv:
                levels.append(lv)
        out[site] = WindsAloft(site, levels, based_on=based, valid=valid, use_period=use, use_from=use_from, use_to=use_to)
    return out


def _use_window(span: str, valid: datetime | None) -> tuple[datetime | None, datetime | None]:
    """'1500-0000' around a valid time → the datetimes the forecast is meant to be used between."""
    if valid is None:
        return None, None
    try:
        h0, h1 = int(span[:2]), int(span[5:7])
    except ValueError:
        return None, None
    start = valid.replace(hour=h0 % 24, minute=0)
    end = valid.replace(hour=h1 % 24, minute=0)
    if start > valid:
        start -= timedelta(days=1)
    if end <= valid:
        end += timedelta(days=1)
    return start, end


_FD_REGION = {"us": "us", "alaska": "alaska", "hawaii": "hawaii", "other_pac": "other_pac"}


async def fetch_winds(http: Http, lat: float, lon: float, max_nm: float = 150) -> tuple[WindsAloft | None, bool]:
    """Forecast winds/temps aloft at the nearest FD site.

    Each bulletin cycle has 6/12/24-hour forecasts with their own "FOR USE"
    windows; the first one whose window contains now is used (usually 1-2 requests).
    """
    near = nearest_fd_site(lat, lon)
    if near is None or near[2] > max_nm:
        return None, False
    sid, site, dist = near
    now = A.utcnow()
    first: tuple[WindsAloft | None, bool] | None = None
    for fcst in ("06", "12", "24"):
        params = {"region": _FD_REGION.get(site.get("region", "us"), "us"), "level": "low", "fcst": fcst}
        text, stale = await http.get_text_cached(URLS["windtemp"], params)
        winds = parse_windtemp(text or "", now).get(sid)
        if winds:
            winds.lat, winds.lon, winds.distance_nm = site["lat"], site["lon"], dist
        if first is None:
            first = (winds, stale)
        if winds and (winds.in_use(now) or (winds.use_from and winds.use_from > now)):
            return winds, stale
    return first or (None, False)
