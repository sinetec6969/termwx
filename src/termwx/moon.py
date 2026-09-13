"""Moon phase and illumination (Meeus, Astronomical Algorithms ch. 48, low precision)."""

from __future__ import annotations

import math
from datetime import datetime, timezone

SYNODIC = 29.530588853

# Upper bound of each phase as a fraction of the lunation.
PHASES = [
    (1.84566 / SYNODIC, "New Moon", "🌑"),
    (5.53699 / SYNODIC, "Waxing Crescent", "🌒"),
    (9.22831 / SYNODIC, "First Quarter", "🌓"),
    (12.91963 / SYNODIC, "Waxing Gibbous", "🌔"),
    (16.61096 / SYNODIC, "Full Moon", "🌕"),
    (20.30228 / SYNODIC, "Waning Gibbous", "🌖"),
    (23.99361 / SYNODIC, "Last Quarter", "🌗"),
    (27.68493 / SYNODIC, "Waning Crescent", "🌘"),
    (1.0, "New Moon", "🌑"),
]


def _fraction_and_illumination(when: datetime) -> tuple[float, float]:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    jd = when.timestamp() / 86400 + 2440587.5
    t = (jd - 2451545.0) / 36525
    d = math.radians((297.8501921 + 445267.1114034 * t) % 360)  # mean elongation
    m = math.radians((357.5291092 + 35999.0502909 * t) % 360)  # sun mean anomaly
    mp = math.radians((134.9633964 + 477198.8675055 * t) % 360)  # moon mean anomaly
    i = (
        180
        - math.degrees(d)
        - 6.289 * math.sin(mp)
        + 2.100 * math.sin(m)
        - 1.274 * math.sin(2 * d - mp)
        - 0.658 * math.sin(2 * d)
        - 0.214 * math.sin(2 * mp)
        - 0.110 * math.sin(d)
    )
    illum = (1 + math.cos(math.radians(i))) / 2
    elongation = math.degrees(math.acos(max(-1.0, min(1.0, 1 - 2 * illum))))  # 0..180
    waxing = math.sin(d) >= 0
    frac = elongation / 360 if waxing else 1 - elongation / 360
    return frac, illum


def age(when: datetime | None = None) -> float:
    frac, _ = _fraction_and_illumination(when or datetime.now(timezone.utc))
    return frac * SYNODIC


def phase(when: datetime | None = None) -> tuple[str, str, float]:
    """Return (name, emoji, illumination 0..1)."""
    frac, illum = _fraction_and_illumination(when or datetime.now(timezone.utc))
    for limit, name, emoji in PHASES:
        if frac < limit:
            return name, emoji, illum
    return "New Moon", "🌑", illum
