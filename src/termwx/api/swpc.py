"""NOAA Space Weather Prediction Center products."""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone

from ..models import KpPoint, ScaleOutlook, SpaceWeather, SwpcMessage
from .http import FetchError, Http

BASE = "https://services.swpc.noaa.gov"
URLS = {
    "scales": f"{BASE}/products/noaa-scales.json",
    "kp": f"{BASE}/products/noaa-planetary-k-index-forecast.json",
    "wind": f"{BASE}/json/rtsw/rtsw_wind_1m.json",
    "mag": f"{BASE}/json/rtsw/rtsw_mag_1m.json",
    "mag_now": f"{BASE}/products/summary/solar-wind-mag-field.json",
    "flares": f"{BASE}/json/goes/primary/xray-flares-latest.json",
    "f107": f"{BASE}/products/summary/10cm-flux.json",
    "alerts": f"{BASE}/products/alerts.json",
    "aurora": f"{BASE}/json/ovation_aurora_latest.json",
}


def _utc(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace(" ", "T").replace("Z", "")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_scales(data: dict, sw: SpaceWeather) -> None:
    now = data.get("0", {})
    sw.g_now = _int(now.get("G", {}).get("Scale")) or 0
    sw.s_now = _int(now.get("S", {}).get("Scale")) or 0
    sw.r_now = _int(now.get("R", {}).get("Scale")) or 0
    outlook = []
    for key in ("1", "2", "3"):
        e = data.get(key)
        if not e:
            continue
        outlook.append(
            ScaleOutlook(
                date=date.fromisoformat(e["DateStamp"]),
                g=_int(e.get("G", {}).get("Scale")),
                r_minor_prob=_int(e.get("R", {}).get("MinorProb")),
                r_major_prob=_int(e.get("R", {}).get("MajorProb")),
                s_prob=_int(e.get("S", {}).get("Prob")),
            )
        )
    sw.outlook = outlook


def parse_kp(data: list, sw: SpaceWeather) -> None:
    points = []
    for row in data:
        if isinstance(row, dict):
            t, kp, kind = row.get("time_tag"), row.get("kp"), row.get("observed", "")
        else:  # legacy array-of-arrays format with a header row
            if row and row[0] == "time_tag":
                continue
            t, kp, kind = row[0], row[1], row[2] if len(row) > 2 else ""
        ts = _utc(t)
        if ts is None or kp is None:
            continue
        points.append(KpPoint(ts, float(kp), str(kind)))
    points.sort(key=lambda p: p.time)
    sw.kp = points


def _active_rows(data: list, hours: float = 3) -> list[dict]:
    rows = [r for r in data if r.get("active")]
    rows.sort(key=lambda r: r.get("time_tag", ""))
    if not rows:
        return []
    latest = _utc(rows[-1]["time_tag"])
    cutoff = latest - timedelta(hours=hours)
    return [r for r in rows if (_utc(r["time_tag"]) or latest) >= cutoff]


def parse_wind(data: list, sw: SpaceWeather) -> None:
    rows = [r for r in _active_rows(data) if r.get("proton_speed") is not None]
    if rows:
        sw.wind_speed = float(rows[-1]["proton_speed"])
        dens = [r["proton_density"] for r in rows if r.get("proton_density") is not None]
        sw.wind_density = float(dens[-1]) if dens else None
        sw.speed_history = [float(r["proton_speed"]) for r in rows]


def parse_mag(data: list, sw: SpaceWeather) -> None:
    rows = [r for r in _active_rows(data) if r.get("bz_gsm") is not None]
    if rows:
        sw.bz_history = [float(r["bz_gsm"]) for r in rows]
        if sw.bz is None:
            sw.bz = float(rows[-1]["bz_gsm"])
            sw.bt = float(rows[-1]["bt"]) if rows[-1].get("bt") is not None else None


def parse_mag_now(data: list, sw: SpaceWeather) -> None:
    if data:
        sw.bt = float(data[0]["bt"]) if data[0].get("bt") is not None else None
        sw.bz = float(data[0]["bz_gsm"]) if data[0].get("bz_gsm") is not None else None


def parse_flares(data: list, sw: SpaceWeather) -> None:
    if data:
        f = data[0]
        sw.flare_current = f.get("current_class") or ""
        sw.flare_max = f.get("max_class") or ""
        sw.flare_max_time = _utc(f.get("max_time"))


def parse_f107(data: list, sw: SpaceWeather) -> None:
    if data and data[0].get("flux") is not None:
        sw.f107 = float(data[0]["flux"])


_SCALE_RE = re.compile(r"NOAA Scale:\s*([GSR][1-5])", re.I)
_TITLE_RE = re.compile(r"^(ALERT|WARNING|WATCH|SUMMARY|CONTINUED ALERT|EXTENDED WARNING|CANCEL \w+)\s*:\s*(.+)$", re.M)


def parse_messages(data: list) -> list[SwpcMessage]:
    out = []
    for row in data:
        msg = (row.get("message") or "").replace("\r\n", "\n")
        issued = _utc(row.get("issue_datetime"))
        if issued is None:
            continue
        code_m = re.search(r"Message Code:\s*(\w+)", msg)
        title_m = _TITLE_RE.search(msg)
        scale_m = _SCALE_RE.search(msg)
        title = f"{title_m.group(1).title()}: {title_m.group(2).strip()}" if title_m else msg.split("\n", 1)[0]
        out.append(
            SwpcMessage(
                product_id=row.get("product_id", ""),
                issued=issued,
                code=code_m.group(1) if code_m else "",
                title=title,
                body=msg.strip(),
                scale=scale_m.group(1).upper() if scale_m else "",
            )
        )
    out.sort(key=lambda m: m.issued, reverse=True)
    return out


def aurora_lookup(data: dict, lat: float, lon: float) -> tuple[int | None, int | None, datetime | None]:
    """Return (probability overhead, max probability within view range, forecast time).

    Aurora can be seen on the horizon from several hundred km away, so "nearby"
    scans ~8° of latitude toward the pole and ±15° of longitude.
    """
    coords = data.get("coordinates") or []
    if not coords:
        return None, None, None
    grid: dict[tuple[int, int], int] = {}
    for c in coords:
        grid[(int(c[0]), int(c[1]))] = int(c[2])
    glon = round(lon) % 360
    glat = max(-90, min(90, round(lat)))
    overhead = grid.get((glon, glat))
    step = 1 if lat >= 0 else -1
    nearby = 0
    for dlat in range(0, 9):
        la = glat + dlat * step
        if not -90 <= la <= 90:
            break
        for dlon in range(-15, 16):
            nearby = max(nearby, grid.get(((glon + dlon) % 360, la), 0))
    return overhead, nearby, _utc(data.get("Forecast Time"))


async def fetch_space(http: Http, lat: float, lon: float) -> SpaceWeather:
    names = list(URLS)
    results = await asyncio.gather(*(http.get_json(URLS[n]) for n in names), return_exceptions=True)
    sw = SpaceWeather()
    parsers = {
        "scales": parse_scales,
        "kp": parse_kp,
        "wind": parse_wind,
        "mag_now": parse_mag_now,
        "flares": parse_flares,
        "f107": parse_f107,
    }
    got = dict(zip(names, results))
    for name, res in got.items():
        if isinstance(res, BaseException):
            if not isinstance(res, FetchError):
                raise res
            sw.errors.append(f"{name}: {res}")
            continue
        data, stale = res
        sw.stale = sw.stale or stale
        try:
            if name in parsers:
                parsers[name](data, sw)
            elif name == "alerts":
                sw.messages = parse_messages(data)
            elif name == "aurora":
                sw.aurora_overhead, sw.aurora_nearby, sw.aurora_time = aurora_lookup(data, lat, lon)
        except (KeyError, TypeError, ValueError, IndexError) as e:
            sw.errors.append(f"{name}: unexpected format ({e})")
    # mag history is parsed last so the 1-minute summary value wins when present.
    mag = got.get("mag")
    if mag is not None and not isinstance(mag, BaseException):
        try:
            parse_mag(mag[0], sw)
        except (KeyError, TypeError, ValueError):
            pass
    if len(sw.errors) == len(names):
        raise FetchError("space weather unavailable")
    return sw


def recent_major_messages(sw: SpaceWeather, min_level: int = 3, hours: int = 24) -> list[SwpcMessage]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [
        m
        for m in sw.messages
        if m.level >= min_level and m.issued >= cutoff and not m.title.lower().startswith(("summary", "cancel"))
    ]
