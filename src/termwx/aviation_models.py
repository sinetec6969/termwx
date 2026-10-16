"""Aviation data containers, normalized from Aviation Weather Center products.

All times are timezone-aware UTC. Units follow aviation convention: knots,
statute miles, feet, °C, hPa (with inHg helpers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar

CATEGORIES = ("VFR", "MVFR", "IFR", "LIFR")
# Higher = worse. Unknown sorts as VFR-equivalent so it never looks alarming by accident.
CATEGORY_RANK = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}
CEILING_COVERS = ("BKN", "OVC", "OVX")

T = TypeVar("T")


@dataclass
class CloudLayer:
    cover: str  # FEW SCT BKN OVC OVX CLR SKC CAVOK
    base_ft: int | None = None  # AGL for METAR/TAF, MSL for PIREPs
    top_ft: int | None = None
    kind: str = ""  # CB / TCU when reported

    @property
    def code(self) -> str:
        if self.base_ft is None or self.cover in ("CLR", "SKC", "CAVOK", "NSC"):
            return self.cover
        return f"{self.cover}{self.base_ft // 100:03d}{self.kind}"


def ceiling_of(clouds: list[CloudLayer], vert_vis_ft: int | None = None) -> int | None:
    """Lowest broken/overcast layer (or vertical visibility), in feet."""
    bases = [c.base_ft for c in clouds if c.cover in CEILING_COVERS and c.base_ft is not None]
    if vert_vis_ft is not None:
        bases.append(vert_vis_ft)
    return min(bases) if bases else None


@dataclass
class Runway:
    ident: str  # "18C/36C"
    length_ft: int | None = None
    width_ft: int | None = None
    surface: str = ""

    @property
    def ends(self) -> list[tuple[str, int]]:
        """[(designator, magnetic heading)] derived from the runway numbers, e.g. 18C → 180°."""
        out = []
        for end in self.ident.split("/"):
            m = re.match(r"^(\d{1,2})([LRC]?)$", end.strip().upper())
            if m and 1 <= int(m.group(1)) <= 36:  # helipads (H1) and water lanes (NW) have no heading
                out.append((end.strip(), int(m.group(1)) * 10))
        return out


@dataclass
class AviationStation:
    icao: str
    name: str
    lat: float
    lon: float
    elev_m: float | None = None
    state: str = ""
    country: str = ""
    has_metar: bool = True
    has_taf: bool = False
    magdec: float | None = None  # magnetic variation, degrees east positive
    runways: list[Runway] = field(default_factory=list)

    @property
    def elev_ft(self) -> float | None:
        return None if self.elev_m is None else self.elev_m * 3.28084


@dataclass
class StationChoice:
    """The station in use for the current location and how it was chosen."""

    station: AviationStation | None
    source: str  # "auto" | "explicit"
    distance_nm: float | None = None  # from the weather location
    bearing: float | None = None  # from the weather location to the station
    error: str = ""
    retryable: bool = False  # the lookup failed on the network, not for lack of a station


@dataclass
class MetarReport:
    station: str
    obs_time: datetime
    raw: str
    report_type: str = "METAR"  # METAR | SPECI
    temp_c: float | None = None
    dewp_c: float | None = None
    wind_dir: int | None = None  # degrees true; None when variable or calm
    wind_variable: bool = False
    wind_speed_kt: int | None = None
    wind_gust_kt: int | None = None
    visibility_sm: float | None = None
    visibility_plus: bool = False  # "10+": greater than the reported value
    altimeter_hpa: float | None = None
    slp_hpa: float | None = None
    pressure_tendency_hpa: float | None = None  # measured, last 3h
    clouds: list[CloudLayer] = field(default_factory=list)
    vert_vis_ft: int | None = None
    wx: str = ""
    flight_category: str = ""
    name: str = ""
    lat: float | None = None
    lon: float | None = None
    elev_m: float | None = None

    @property
    def is_speci(self) -> bool:
        return self.report_type == "SPECI"

    @property
    def ceiling_ft(self) -> int | None:
        return ceiling_of(self.clouds, self.vert_vis_ft)

    @property
    def altimeter_inhg(self) -> float | None:
        return None if self.altimeter_hpa is None else self.altimeter_hpa * 0.0295300


@dataclass
class IcingTurbulence:
    kind: str  # ICE | TURB
    intensity: str  # raw code, e.g. "MOD", "LGT-MOD", "SEV"
    base_ft: int | None = None
    top_ft: int | None = None
    type: str = ""  # RIME, CLEAR, CHOP, CAT…
    freq: str = ""  # ISOL, OCNL, CONT


@dataclass
class TafPeriod:
    start: datetime
    end: datetime
    change: str = "BASE"  # BASE | FM | BECMG | TEMPO | PROB
    probability: int | None = None
    becmg_end: datetime | None = None
    wind_dir: int | None = None
    wind_variable: bool = False
    wind_speed_kt: int | None = None
    wind_gust_kt: int | None = None
    visibility_sm: float | None = None
    visibility_plus: bool = False
    clouds: list[CloudLayer] = field(default_factory=list)
    vert_vis_ft: int | None = None
    wx: str = ""
    wind_shear: str = ""  # e.g. "WS020/24040KT"
    icing_turbulence: list[IcingTurbulence] = field(default_factory=list)
    not_decoded: str = ""

    @property
    def ceiling_ft(self) -> int | None:
        return ceiling_of(self.clouds, self.vert_vis_ft)

    @property
    def is_temporary(self) -> bool:
        """TEMPO and PROB groups modify the prevailing forecast only briefly."""
        return self.change in ("TEMPO", "PROB")

    @property
    def label(self) -> str:
        if self.change == "PROB":
            return f"PROB{self.probability or ''}"
        if self.change == "TEMPO" and self.probability:
            return f"PROB{self.probability} TEMPO"
        return "" if self.change == "BASE" else self.change


@dataclass
class TafForecast:
    station: str
    issue_time: datetime
    valid_from: datetime
    valid_to: datetime
    raw: str
    periods: list[TafPeriod] = field(default_factory=list)
    amended: bool = False
    corrected: bool = False
    name: str = ""


@dataclass
class Pirep:
    obs_time: datetime
    raw: str
    report_type: str = "PIREP"  # PIREP | Urgent PIREP | AIREP | AMDAR
    aircraft: str = ""
    lat: float | None = None
    lon: float | None = None
    altitude_ft: int | None = None
    phase: str = ""  # DURC (climb), DURD (descent), CRUISE…
    turbulence: list[IcingTurbulence] = field(default_factory=list)
    icing: list[IcingTurbulence] = field(default_factory=list)
    clouds: list[CloudLayer] = field(default_factory=list)
    visibility_sm: float | None = None
    wx: str = ""
    temp_c: float | None = None
    wind_dir: int | None = None
    wind_speed_kt: int | None = None
    braking: str = ""
    distance_nm: float | None = None  # from the aviation reference point
    bearing: float | None = None  # from the reference point to the report

    @property
    def urgent(self) -> bool:
        return self.report_type.upper().startswith("URGENT") or " UUA " in f" {self.raw} "


@dataclass
class AviationHazard:
    kind: str  # SIGMET | CONVECTIVE SIGMET | INTL SIGMET | G-AIRMET | AIRMET | CWA
    name: str  # display name, e.g. "SIGMET NOVEMBER 3", "G-AIRMET TANGO"
    hazard: str  # TS, TURB, ICE, IFR, MTN OBSC, VA, TC…
    valid_from: datetime | None
    valid_to: datetime | None
    raw: str = ""
    polygon: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon)
    is_line: bool = False
    base_ft: int | None = None
    top_ft: int | None = None
    base_text: str = ""  # e.g. "FZL" when the base is the freezing level
    severity: str = ""
    qualifier: str = ""
    movement: str = ""
    issuer: str = ""
    # Filled in by applicability checks against the reference point:
    contains_point: bool = False
    distance_nm: float | None = None


@dataclass
class HazardSet:
    items: list[AviationHazard] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class WindsAloftLevel:
    altitude_ft: int
    wind_dir: int | None  # None = light and variable
    wind_speed_kt: int | None
    temp_c: int | None = None


@dataclass
class WindsAloft:
    site: str
    levels: list[WindsAloftLevel]
    lat: float | None = None
    lon: float | None = None
    based_on: datetime | None = None
    valid: datetime | None = None
    use_period: str = ""  # e.g. "0800-1500Z"
    use_from: datetime | None = None
    use_to: datetime | None = None
    distance_nm: float | None = None

    def in_use(self, now: datetime) -> bool:
        return self.use_from is not None and self.use_to is not None and self.use_from <= now < self.use_to


@dataclass
class Feed(Generic[T]):
    """One independently fetched product and its freshness.

    `fetched_at` is when termwx last received the data (live or from cache);
    `stale` means the network failed and the data came from the offline cache.
    """

    data: T | None = None
    fetched_at: datetime | None = None
    stale: bool = False
    error: str = ""
    loaded: bool = False

    def ok(self, data: T | None, stale: bool, now: datetime) -> None:
        self.data, self.stale, self.error, self.loaded = data, stale, "", True
        self.fetched_at = now

    def fail(self, error: str) -> None:
        # Keep whatever we had; the UI marks it stale.
        self.error, self.loaded = error, True
        if self.data is not None:
            self.stale = True


@dataclass
class AviationResult:
    choice: StationChoice | None = None
    metars: Feed[list[MetarReport]] = field(default_factory=Feed)
    taf: Feed[TafForecast] = field(default_factory=Feed)
    pireps: Feed[list[Pirep]] = field(default_factory=Feed)
    hazards: Feed[HazardSet] = field(default_factory=Feed)
    winds: Feed[WindsAloft] = field(default_factory=Feed)
    notam_status: str = ""

    @property
    def station(self) -> AviationStation | None:
        return self.choice.station if self.choice else None

    @property
    def metar(self) -> MetarReport | None:
        return self.metars.data[0] if self.metars.data else None
