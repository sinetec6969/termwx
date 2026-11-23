"""Data types for the amateur satellite tracker (FM repeaters and APRS digipeaters)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

FM, APRS = "FM", "APRS"


@dataclass
class Transmitter:
    kind: str  # FM | APRS
    description: str
    uplink: int | None  # Hz
    downlink: int | None  # Hz
    mode: str = ""
    ctcss: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Transmitter:
        return cls(
            kind=d["kind"], description=d.get("description", ""), uplink=d.get("uplink"),
            downlink=d.get("downlink"), mode=d.get("mode", ""), ctcss=d.get("ctcss"),
        )


_OSCAR = re.compile(r"\(([A-Z]{1,4}-\d{1,3})\)")


@dataclass
class Satellite:
    norad: int
    name: str
    tle1: str
    tle2: str
    transmitters: list[Transmitter] = field(default_factory=list)
    alias: str = ""  # SatNOGS name, OSCAR number preferred (AO-91, PO-101)
    sat_id: str = ""  # SatNOGS satellite id (for its TLE fallback)
    names: list[str] = field(default_factory=list)  # every SatNOGS name and alias (CAS-3H, XW-2H, …)

    @property
    def short_name(self) -> str:
        """The SatNOGS alias, else `SAUDISAT 1C (SO-50)` → `SO-50`, `ISS (ZARYA)` → `ISS`, else the CelesTrak name."""
        if self.alias:
            return self.alias
        m = _OSCAR.search(self.name)
        if m:
            return m.group(1)
        if self.name.startswith("ISS"):
            return "ISS"
        return self.name.strip()

    @property
    def kinds(self) -> list[str]:
        return [k for k in (FM, APRS) if any(t.kind == k for t in self.transmitters)]

    @property
    def epoch(self) -> datetime:
        """TLE epoch from line 1 columns 19–32 (YYDDD.DDDDDDDD)."""
        yy, doy = int(self.tle1[18:20]), float(self.tle1[20:32])
        year = 2000 + yy if yy < 57 else 1900 + yy
        return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)

    @property
    def mean_motion(self) -> float:
        """Revolutions per day (line 2 columns 53–63)."""
        return float(self.tle2[52:63])

    def to_dict(self) -> dict:
        return {"norad": self.norad, "name": self.name, "alias": self.alias, "tle1": self.tle1, "tle2": self.tle2,
                "transmitters": [t.to_dict() for t in self.transmitters], "sat_id": self.sat_id, "names": self.names}

    @classmethod
    def from_dict(cls, d: dict) -> Satellite:
        return cls(int(d["norad"]), d["name"], d["tle1"], d["tle2"],
                   [Transmitter.from_dict(t) for t in d.get("transmitters", [])], d.get("alias", ""),
                   d.get("sat_id", ""), list(d.get("names") or []))


@dataclass
class Catalog:
    """What `api.sats` fetched: the satellites, plus when TLEs and transmitters were last downloaded."""

    satellites: list[Satellite] = field(default_factory=list)
    fetched_at: datetime | None = None  # TLEs (UTC)
    tx_fetched_at: datetime | None = None  # SatNOGS transmitter list (UTC)
    stale: bool = False  # the last update failed; this is the saved copy
    error: str = ""


@dataclass
class Look:
    """Where a satellite is from the observer at one instant."""

    az: float  # degrees from true north
    el: float  # degrees above the horizon
    range_km: float
    range_rate: float  # km/s, positive = moving away
    lat: float  # sub-satellite point
    lon: float
    alt_km: float


@dataclass
class Pass:
    aos: datetime  # horizon crossing, UTC
    tca: datetime  # time of maximum elevation
    los: datetime
    max_el: float
    aos_az: float
    tca_az: float
    los_az: float
    track: list[tuple[float, float]] = field(default_factory=list)  # (az, el) samples, AOS → LOS

    @property
    def duration(self) -> timedelta:
        return self.los - self.aos

    def active(self, now: datetime) -> bool:
        return self.aos <= now < self.los


@dataclass
class SatState:
    """Everything the dashboard panel and the tracker screen draw from."""

    catalog: Catalog = field(default_factory=Catalog)
    passes: dict[int, list[Pass]] = field(default_factory=dict)  # norad → passes in the next 24 h
    computed_at: datetime | None = None
    lat: float = 0.0
    lon: float = 0.0
    tz: str = ""
    min_el: float = 10.0
    refresh_hours: float = 3.0
    updating: bool = False

    def sat(self, norad: int) -> Satellite | None:
        return next((s for s in self.catalog.satellites if s.norad == norad), None)

    def next_update(self) -> datetime | None:
        if self.catalog.fetched_at is None:
            return None
        return self.catalog.fetched_at + timedelta(hours=self.refresh_hours)
