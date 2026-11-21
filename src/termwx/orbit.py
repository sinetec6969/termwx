"""Satellite position and pass prediction: SGP4 (via the `sgp4` package) plus the
observer geometry gpredict does — TEME → Earth-fixed → topocentric az/el,
range rate for Doppler, and AOS/TCA/LOS search. Pure functions, no I/O.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sgp4.api import Satrec, jday

from .sat_models import Look, Pass, Satellite

C_KMS = 299_792.458
WGS84_A = 6378.137  # km
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)
EARTH_ROT = 7.292115146706979e-5  # rad/s

COARSE_STEP = 60  # seconds between samples when scanning for passes
TRACK_POINTS = 40


@lru_cache(maxsize=256)
def _satrec(tle1: str, tle2: str) -> Satrec:
    return Satrec.twoline2rv(tle1, tle2)


def satrec(sat: Satellite) -> Satrec:
    return _satrec(sat.tle1, sat.tle2)


class Observer:
    """A fixed point on the WGS-84 ellipsoid, with its ECEF position precomputed."""

    def __init__(self, lat: float, lon: float, alt_km: float = 0.0):
        self.lat, self.lon, self.alt_km = lat, lon, alt_km
        self.slat, self.clat = math.sin(math.radians(lat)), math.cos(math.radians(lat))
        self.slon, self.clon = math.sin(math.radians(lon)), math.cos(math.radians(lon))
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * self.slat**2)
        self.x = (n + alt_km) * self.clat * self.clon
        self.y = (n + alt_km) * self.clat * self.slon
        self.z = (n * (1 - WGS84_E2) + alt_km) * self.slat


def gmst(jd_ut1: float) -> float:
    """Greenwich mean sidereal time in radians (IAU 1982, as used by SGP4/Vallado)."""
    t = (jd_ut1 - 2451545.0) / 36525.0
    secs = 67310.54841 + (876600.0 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t * t * t
    return math.radians((secs % 86400) / 240.0) % (2 * math.pi)


def _jd(t: datetime) -> tuple[float, float]:
    t = t.astimezone(timezone.utc)
    return jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond / 1e6)


def _geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """ECEF (km) → geodetic lat, lon (degrees) and height (km)."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - WGS84_E2))
    h = 0.0
    for _ in range(5):
        s = math.sin(lat)
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * s * s)
        h = p / math.cos(lat) - n if abs(lat) < math.radians(89) else z / s - n * (1 - WGS84_E2)
        lat = math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def look(sat: Satellite, obs: Observer, t: datetime, subpoint: bool = True) -> Look | None:
    """Az/el/range/range-rate of `sat` from `obs` at `t`. None if SGP4 fails (e.g. decayed)."""
    jd, fr = _jd(t)
    err, r, v = satrec(sat).sgp4(jd, fr)
    if err:
        return None
    th = gmst(jd + fr)
    c, s = math.cos(th), math.sin(th)
    # TEME → pseudo Earth-fixed (polar motion ignored; well under a km)
    x, y, z = c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2]
    vx = c * v[0] + s * v[1] + EARTH_ROT * y
    vy = -s * v[0] + c * v[1] - EARTH_ROT * x
    vz = v[2]
    rx, ry, rz = x - obs.x, y - obs.y, z - obs.z
    rng = math.sqrt(rx * rx + ry * ry + rz * rz)
    south = obs.slat * obs.clon * rx + obs.slat * obs.slon * ry - obs.clat * rz
    east = -obs.slon * rx + obs.clon * ry
    up = obs.clat * obs.clon * rx + obs.clat * obs.slon * ry + obs.slat * rz
    el = math.degrees(math.asin(max(-1.0, min(1.0, up / rng))))
    az = math.degrees(math.atan2(east, -south)) % 360
    rate = (rx * vx + ry * vy + rz * vz) / rng
    lat = lon = alt = 0.0
    if subpoint:
        lat, lon, alt = _geodetic(x, y, z)
    return Look(az, el, rng, rate, lat, lon, alt)


def elevation(sat: Satellite, obs: Observer, t: datetime) -> float:
    lk = look(sat, obs, t, subpoint=False)
    return -90.0 if lk is None else lk.el


def doppler_downlink(f_hz: float, range_rate: float) -> float:
    """What you hear: a receding satellite (positive range rate) sounds low."""
    return f_hz * (1 - range_rate / C_KMS)


def doppler_uplink(f_hz: float, range_rate: float) -> float:
    """What to transmit so the satellite hears its nominal uplink."""
    return f_hz * (1 + range_rate / C_KMS)


def _crossing(sat: Satellite, obs: Observer, lo: datetime, hi: datetime, rising: bool) -> datetime:
    """Bisect the horizon crossing between `lo` and `hi` to about a second."""
    while (hi - lo).total_seconds() > 1:
        mid = lo + (hi - lo) / 2
        up = elevation(sat, obs, mid) >= 0
        if up == rising:
            hi = mid
        else:
            lo = mid
    return hi if rising else lo


def _peak(sat: Satellite, obs: Observer, a: datetime, b: datetime) -> datetime:
    """Golden-section search for maximum elevation in [a, b]."""
    g = (math.sqrt(5) - 1) / 2
    c, d = b - (b - a) * g, a + (b - a) * g
    fc, fd = elevation(sat, obs, c), elevation(sat, obs, d)
    while (b - a).total_seconds() > 1:
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - (b - a) * g
            fc = elevation(sat, obs, c)
        else:
            a, c, fc = c, d, fd
            d = a + (b - a) * g
            fd = elevation(sat, obs, d)
    return a + (b - a) / 2


def _build_pass(sat: Satellite, obs: Observer, aos: datetime, los: datetime) -> Pass:
    tca = _peak(sat, obs, aos, los)
    a, m, z = (look(sat, obs, t, subpoint=False) for t in (aos, tca, los))
    span = (los - aos) / (TRACK_POINTS - 1)
    track = []
    for i in range(TRACK_POINTS):
        lk = look(sat, obs, aos + span * i, subpoint=False)
        if lk:
            track.append((lk.az, max(0.0, lk.el)))
    return Pass(aos, tca, los, m.el if m else 0.0, a.az if a else 0.0, m.az if m else 0.0, z.az if z else 0.0, track)


def find_passes(sat: Satellite, obs: Observer, start: datetime, hours: float = 24, min_el: float = 10.0,
                step: int = COARSE_STEP) -> list[Pass]:
    """Passes whose LOS is after `start` and that begin within `hours`, peaking at or above `min_el`.

    AOS/LOS are horizon (0°) crossings, as gpredict reports them; `min_el` only
    drops passes too low to work. A pass already in progress at `start` is
    included with its real AOS (searched up to an hour back).
    """
    start = start.astimezone(timezone.utc)
    end = start + timedelta(hours=hours)
    dt = timedelta(seconds=step)
    t = start
    prev_up = elevation(sat, obs, t) >= 0
    passes: list[Pass] = []
    aos: datetime | None = None
    if prev_up:
        # back up to find the AOS of the pass in progress
        back = start
        limit = start - timedelta(hours=1)
        while back > limit and elevation(sat, obs, back - dt) >= 0:
            back -= dt
        aos = _crossing(sat, obs, back - dt, back, rising=True) if back > limit else back
    while t < end or aos is not None:
        nxt = t + dt
        up = elevation(sat, obs, nxt) >= 0
        if up and not prev_up:
            if nxt > end:
                break
            aos = _crossing(sat, obs, t, nxt, rising=True)
        elif prev_up and not up and aos is not None:
            los = _crossing(sat, obs, t, nxt, rising=False)
            p = _build_pass(sat, obs, aos, los)
            if p.max_el >= min_el:
                passes.append(p)
            aos = None
        elif up and aos is not None and nxt - aos > timedelta(hours=12):
            # geostationary or deep-space object that never sets: not a pass
            return passes
        prev_up, t = up, nxt
    return passes
