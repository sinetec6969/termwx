"""Part 107 drone go/no-go: weather checks scored against the regulatory floors
and the pilot's personal minimums, plus the manual preflight items. No I/O, no UI.

Everything is compared in mph, feet, statute miles and °F (Part 107 is a US
rule); values are converted at the boundary from METAR knots and from
Open-Meteo's mph or km/h.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import aviation as A, units as U, wmo
from .aviation_models import AviationResult, CEILING_COVERS, CloudLayer, TafPeriod
from .models import AlertsResult, Hour, Location, SpaceWeather, Weather

GO, CAUTION, NOGO, UNKNOWN = "GO", "CAUTION", "NO-GO", "UNKNOWN"
NOT_READY = "NOT READY"
STATUS_RANK = {GO: 0, UNKNOWN: 1, CAUTION: 1, NOGO: 2}

# Part 107 floors
MIN_VIS_SM = 3.0  # §107.51(c)
CLOUD_CLEARANCE_FT = 500  # §107.51(d): at least 500 ft below clouds
MAX_ALTITUDE_FT = 400  # §107.51(b)

MAX_WINDOW_H = 12
NEAR_STATION_NM = 10  # farther than this, the METAR may not describe the flight site
METAR_OLD_MIN = 75
CAUTION_FRACTION = 0.8  # within 80% of a personal limit
GUST_SPREAD_KT = 10
COLD_BATTERY_F = 50
HOT_MARGIN_F = 5
ICING_TEMP_C = 2
HIGH_DA_FT = 5000
LOW_LEVEL_FT = 3000  # advisories based above this don't reach a drone
KT_TO_MPH = 1.150779
SUN_ALT_SET = -0.833  # sunrise/sunset: upper limb with refraction
SUN_ALT_CIVIL = -6.0

_PRECIP = ("RA", "DZ", "SN", "SG", "PL", "GR", "GS", "UP", "IC", "SH")
_FREEZING_PRECIP = ("FZRA", "FZDZ", "PL")
_FREEZING_CODES = {56, 57, 66, 67}
_THUNDER_CODES = {95, 96, 99}
_LOW_LEVEL_HAZARDS = {"SFC WIND", "LLWS", "IFR", "MTN OBSC", "TURB"}


# ── limits ───────────────────────────────────────────────────────────────────
@dataclass
class DroneLimits:
    """Personal minimums from the [drone] table in config.toml."""

    panel: bool = True
    altitude_ft: int = 400
    window_hours: int = 2
    max_wind_mph: float = 20.0
    max_gust_mph: float = 25.0
    max_wind_aloft_mph: float = 25.0  # Open-Meteo 120 m level ≈ 400 ft AGL
    min_visibility_sm: float = 3.0
    max_precip_prob: int = 30
    allow_precip: bool = False
    min_temp_f: float = 32.0
    max_temp_f: float = 104.0
    max_kp: float = 5.0
    anticollision_lighting: bool = False
    night_qualified: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DroneLimits:
        """Keys of the wrong type keep their defaults; values past the Part 107 floors are clamped."""
        out = cls()
        for f in fields(cls):
            v, default = d.get(f.name), getattr(out, f.name)
            if isinstance(default, bool):
                if isinstance(v, bool):
                    setattr(out, f.name, v)
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                setattr(out, f.name, type(default)(v))
        return out.clamped()

    def clamped(self) -> DroneLimits:
        self.altitude_ft = max(1, min(MAX_ALTITUDE_FT, self.altitude_ft))
        self.window_hours = max(1, min(MAX_WINDOW_H, self.window_hours))
        self.min_visibility_sm = max(MIN_VIS_SM, self.min_visibility_sm)
        return self


# ── results ──────────────────────────────────────────────────────────────────
@dataclass
class CheckItem:
    id: str
    label: str
    status: str
    value: str = ""
    limit: str = ""
    source: str = ""
    rule: str = ""
    notes: list[str] = field(default_factory=list)
    series: list[float] = field(default_factory=list)  # hourly values across the window, for a sparkline

    @property
    def rank(self) -> int:
        return STATUS_RANK.get(self.status, 1)


@dataclass(frozen=True)
class ManualItem:
    id: str
    label: str
    rule: str
    detail: str


MANUAL_ITEMS = (
    ManualItem("cert", "Remote pilot certificate on hand", "§107.65",
               "Certificate and photo ID with you; recurrent training completed within the last 24 calendar months."),
    ManualItem("registration", "Aircraft registered and marked", "Part 48",
               "FAA registration current, registration number displayed on the outside of the aircraft."),
    ManualItem("remote_id", "Remote ID broadcasting", "Part 89",
               "Standard Remote ID or a broadcast module working — or you are inside an FAA-recognized identification area (FRIA)."),
    ManualItem("airspace", "Airspace checked", "§107.41",
               "Class B, C, D and surface-area E need a LAANC or DroneZone authorization before takeoff."),
    ManualItem("notams", "NOTAMs and TFRs checked", "§107.45 · §99.7",
               "Check NOTAMs and temporary flight restrictions (stadiums, VIP movement, wildfires, security areas) from official sources."),
    ManualItem("site", "Site surveyed", "§107.39 · §107.145 · §107.49",
               "People and moving vehicles, obstacles, wires and towers, launch and recovery area, emergency landing spots."),
    ManualItem("vlos", "Visual line of sight planned", "§107.31 · §107.33",
               "You (or a briefed visual observer) can see the aircraft unaided for the whole flight; crew roles and phraseology briefed."),
    ManualItem("aircraft", "Aircraft preflight done", "§107.15 · §107.49",
               "Props, frame and motors inspected; firmware current; compass and IMU OK; return-to-home altitude above the tallest obstacle."),
    ManualItem("batteries", "Batteries charged and warm", "§107.49",
               "Flight, controller and display batteries charged; flight batteries warm in cold weather; enough reserve to land."),
    ManualItem("pilot", "Pilot fit to fly (IMSAFE)", "§107.27 · §107.61",
               "Illness, medication, stress, alcohol (none within 8 h, below 0.04), fatigue, eating — no condition that interferes with safe operation."),
)


@dataclass
class GoNoGo:
    items: list[CheckItem]
    window_start: datetime  # UTC
    window_end: datetime
    altitude_ft: int
    timezone: str = ""
    manual_done: int = 0
    manual_total: int = len(MANUAL_ITEMS)

    @property
    def weather_verdict(self) -> str:
        """Worst automatic item; missing data counts as CAUTION, never as GO."""
        worst = max((i.rank for i in self.items), default=1)
        return {0: GO, 1: CAUTION, 2: NOGO}[worst]

    @property
    def verdict(self) -> str:
        v = self.weather_verdict
        return NOT_READY if v == GO and self.manual_done < self.manual_total else v

    @property
    def problems(self) -> list[CheckItem]:
        """Items that aren't GO, worst first."""
        return sorted((i for i in self.items if i.status != GO), key=lambda i: -i.rank)


# ── sun ──────────────────────────────────────────────────────────────────────
def solar_elevation(lat: float, lon: float, when: datetime) -> float:
    """Sun's elevation in degrees, without refraction (NOAA solar calculator equations)."""
    when = when.astimezone(timezone.utc)
    t = (when.timestamp() / 86400 + 2440587.5 - 2451545) / 36525  # Julian centuries since J2000
    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    anomaly = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    center = (math.sin(anomaly) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * anomaly) * (0.019993 - 0.000101 * t) + math.sin(3 * anomaly) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * t)
    app_long = math.radians(mean_long + center - 0.00569 - 0.00478 * math.sin(omega))
    obliquity = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    obliquity = math.radians(obliquity + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliquity) * math.sin(app_long))
    y = math.tan(obliquity / 2) ** 2
    l0 = math.radians(mean_long)
    eqtime = 4 * math.degrees(y * math.sin(2 * l0) - 2 * ecc * math.sin(anomaly)
                              + 4 * ecc * y * math.sin(anomaly) * math.cos(2 * l0)
                              - 0.5 * y * y * math.sin(4 * l0) - 1.25 * ecc * ecc * math.sin(2 * anomaly))
    minutes = when.hour * 60 + when.minute + when.second / 60
    ha = math.radians(((minutes + eqtime + 4 * lon) % 1440) / 4 - 180)
    phi = math.radians(lat)
    cos_zen = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))


def sun_crossings(lat: float, lon: float, day: date, angle: float) -> tuple[datetime | None, datetime | None]:
    """(rising, setting) times in UTC when the sun crosses `angle` on the local solar day; None in polar day/night."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=lon / 15)
    step = timedelta(minutes=10)
    rise = sett = None
    t0, e0 = start, solar_elevation(lat, lon, start) - angle
    for _ in range(144):
        t1 = t0 + step
        e1 = solar_elevation(lat, lon, t1) - angle
        if (e0 < 0) != (e1 < 0):
            lo, hi = t0, t1
            for _ in range(12):  # bisect to well under a second
                mid = lo + (hi - lo) / 2
                if (solar_elevation(lat, lon, mid) - angle < 0) == (e0 < 0):
                    lo = mid
                else:
                    hi = mid
            if e0 < 0 and rise is None:
                rise = hi
            elif e0 >= 0 and sett is None:
                sett = hi
        t0, e0 = t1, e1
    return rise, sett


def civil_twilight(lat: float, lon: float, day: date) -> tuple[datetime | None, datetime | None]:
    """Morning and evening civil twilight (sun 6° below the horizon), UTC."""
    return sun_crossings(lat, lon, day, SUN_ALT_CIVIL)


def sun_phase(lat: float, lon: float, when: datetime) -> str:
    """'day' | 'twilight' (civil) | 'night'."""
    e = solar_elevation(lat, lon, when)
    if e > SUN_ALT_SET:
        return "day"
    return "twilight" if e > SUN_ALT_CIVIL else "night"


# ── conversions & formatting ─────────────────────────────────────────────────
def _mph(v: float | None, units: str) -> float | None:
    if v is None:
        return None
    return v if units == U.IMPERIAL else v / 1.609344


def _kt(v: float | None) -> float | None:
    return None if v is None else v * KT_TO_MPH


def _temp_f(v: float | None, units: str) -> float | None:
    return None if v is None else U.to_fahrenheit(v, units)


def _c_to_f(c: float | None) -> float | None:
    return None if c is None else c * 9 / 5 + 32


def _sm(meters: float | None) -> float | None:
    return None if meters is None else meters / 1609.344


def _vis(sm: float, plus: bool = False) -> str:
    if sm >= 10:
        return "10+ SM"
    return f"{sm:.1f}".removesuffix(".0") + ("+" if plus else "") + " SM"


def _local(dt: datetime, tz: str) -> datetime:
    """Aware times converted to the location's zone; naive ones (Open-Meteo) are already local."""
    if dt.tzinfo is None:
        return dt
    try:
        return dt.astimezone(ZoneInfo(tz)) if tz else dt.astimezone()
    except (KeyError, ValueError):
        return dt.astimezone()


def _clock(dt: datetime | None, tz: str) -> str:
    if dt is None:
        return "--"
    local = _local(dt, tz)
    return f"{local.hour % 12 or 12}:{local.minute:02d}{'a' if local.hour < 12 else 'p'}"


def _limit_status(value: float, limit: float) -> str:
    if value > limit:
        return NOGO
    return CAUTION if value >= limit * CAUTION_FRACTION else GO


# ── evaluation context ───────────────────────────────────────────────────────
@dataclass
class _Sample:
    value: float
    source: str
    temporary: bool = False  # TEMPO/PROB group: can lower a NO-GO to CAUTION only
    label: str = ""  # display override, e.g. "6+ SM" for P6SM


@dataclass
class _Ctx:
    loc: Location
    weather: Weather | None
    alerts: AlertsResult | None
    av: AviationResult | None
    space: SpaceWeather | None
    limits: DroneLimits
    now: datetime
    end: datetime

    @property
    def units(self) -> str:
        return self.weather.units if self.weather else U.IMPERIAL

    @property
    def hours(self) -> list[Hour]:
        """Hourly forecast across the window, anchored on the model's own current time."""
        w = self.weather
        if not w or not w.hours:
            return []
        cutoff = w.current.time + timedelta(hours=self.limits.window_hours)
        return [h for h in w.hours if h.time < cutoff]

    @property
    def metar(self):
        return self.av.metar if self.av else None

    @property
    def station_id(self) -> str:
        st = self.av.station if self.av else None
        return st.icao if st else "METAR"

    def taf(self) -> list[tuple[TafPeriod, bool]]:
        """Effective TAF conditions overlapping the window, with whether each is temporary."""
        taf = self.av.taf.data if self.av else None
        if taf is None or taf.valid_to <= self.now:
            return []
        return [(eff, p.is_temporary) for p, eff in A.taf_rows(taf) if p.start < self.end and p.end > self.now]


def _judge(samples: list[_Sample], status_of, worse) -> tuple[str, _Sample | None]:
    """Worst status over the samples (temporary TAF groups cap at CAUTION); ties go to the worse value."""
    best: tuple[int, _Sample] | None = None
    for s in samples:
        st = status_of(s.value)
        if s.temporary and st == NOGO:
            st = CAUTION
        r = STATUS_RANK[st]
        if best is None or r > best[0] or (r == best[0] and worse(s.value, best[1].value)):
            best = (r, s)
    if best is None:
        return UNKNOWN, None
    return {0: GO, 1: CAUTION, 2: NOGO}[best[0]], best[1]


def _higher(a: float, b: float) -> bool:
    return a > b


def _lower(a: float, b: float) -> bool:
    return a < b


# ── checks ───────────────────────────────────────────────────────────────────
def check_daylight(c: _Ctx) -> CheckItem:
    lim, tz = c.limits, c.loc.timezone
    item = CheckItem("daylight", "Daylight", GO, rule="§107.29")
    times = [c.now + timedelta(minutes=10 * i) for i in range(int((c.end - c.now).total_seconds() // 600) + 1)]
    phases = [(t, sun_phase(c.loc.lat, c.loc.lon, t)) for t in times]
    now_phase = phases[0][1]
    today = _local(c.now, tz).date()
    _, dusk = civil_twilight(c.loc.lat, c.loc.lon, today)
    _, sunset = sun_crossings(c.loc.lat, c.loc.lon, today, SUN_ALT_SET)
    item.value = {"day": "daylight", "twilight": "civil twilight", "night": "night"}[now_phase]
    if sunset and dusk:
        item.notes.append(f"sunset {_clock(sunset, tz)} · end of civil twilight {_clock(dusk, tz)}")

    def allowed(phase: str) -> bool:
        if phase == "twilight":
            return lim.anticollision_lighting
        if phase == "night":
            return lim.anticollision_lighting and lim.night_qualified
        return True

    need = {"twilight": "anti-collision lighting visible 3 SM",
            "night": "anti-collision lighting and night training (updated knowledge test or recurrent)"}
    item.limit = "day" + (" + twilight" if lim.anticollision_lighting else "") + (
        " + night" if lim.anticollision_lighting and lim.night_qualified else "")
    if not allowed(now_phase):
        item.status = NOGO
        item.notes.append(f"{now_phase.capitalize()} operations need {need[now_phase]}.")
        return item
    later = next(((t, p) for t, p in phases if not allowed(p)), None)
    if later:
        item.status = CAUTION
        item.notes.append(f"{later[1].capitalize()} from {_clock(later[0], tz)} — land by then or carry {need[later[1]]}.")
    elif now_phase != "day" or any(p != "day" for _, p in phases):
        item.notes.append("Anti-collision lighting on" + (" — night-qualified" if lim.night_qualified else "") + ".")
    return item


def check_visibility(c: _Ctx) -> CheckItem:
    floor = max(MIN_VIS_SM, c.limits.min_visibility_sm)
    item = CheckItem("vis", "Visibility", GO, limit=f"≥ {floor:g} SM", rule="§107.51(c)")
    samples: list[_Sample] = []
    m = c.metar
    if m and m.visibility_sm is not None:
        samples.append(_Sample(m.visibility_sm, c.station_id, label=_vis(m.visibility_sm, m.visibility_plus)))
    if c.weather:
        samples.append(_Sample(_sm(c.weather.current.visibility), "model now"))
        item.series = [_sm(h.visibility) for h in c.hours if h.visibility is not None]
        samples += [_Sample(v, "model forecast") for v in item.series]
    for eff, temp in c.taf():
        vis = eff.visibility_sm if eff.visibility_sm is not None else (6.0 if eff.visibility_plus else None)
        if vis is not None:
            samples.append(_Sample(vis, "TAF " + ("TEMPO/PROB" if temp else "prevailing"), temp,
                                   _vis(vis, eff.visibility_plus)))

    def status_of(v: float) -> str:
        return NOGO if v < floor else CAUTION if v < floor + 2 else GO

    item.status, worst = _judge(samples, status_of, _lower)
    if worst:
        item.value, item.source = worst.label or _vis(worst.value), worst.source
    return item


def _layers(clouds: list[CloudLayer], vv: int | None) -> tuple[int | None, int | None]:
    """(ceiling, lowest FEW/SCT base) in feet AGL."""
    ceiling = min([cl.base_ft for cl in clouds if cl.cover in CEILING_COVERS and cl.base_ft is not None]
                  + ([vv] if vv is not None else []), default=None)
    scattered = min((cl.base_ft for cl in clouds if cl.cover in ("FEW", "SCT") and cl.base_ft is not None), default=None)
    return ceiling, scattered


def check_clouds(c: _Ctx) -> CheckItem:
    need = c.limits.altitude_ft + CLOUD_CLEARANCE_FT
    item = CheckItem("clouds", "Cloud clearance", GO, limit=f"≥ {need} ft AGL", rule="§107.51(d)")
    sources: list[tuple[list[CloudLayer], int | None, str, bool]] = []
    m = c.metar
    if m:
        sources.append((m.clouds, m.vert_vis_ft, c.station_id, False))
    for eff, temp in c.taf():
        sources.append((eff.clouds, eff.vert_vis_ft, "TAF " + ("TEMPO/PROB" if temp else "prevailing"), temp))
    if not sources:
        item.status, item.value = UNKNOWN, "no METAR or TAF"
        item.notes.append("Cloud bases come only from METAR/TAF; check the sky on site.")
        return item
    samples: list[_Sample] = []
    few: list[_Sample] = []
    for clouds, vv, src, temp in sources:
        ceiling, scattered = _layers(clouds, vv)
        if ceiling is not None:
            samples.append(_Sample(ceiling, src, temp))
        if scattered is not None:
            few.append(_Sample(scattered, src, temp))
    item.status, worst = _judge(samples, lambda v: NOGO if v < need else GO, _lower)
    if worst is None:
        item.status = GO
    low_few = min((s for s in few if s.value < need), key=lambda s: s.value, default=None)
    if low_few and item.status == GO:
        item.status = CAUTION
        item.value, item.source = f"FEW/SCT {low_few.value:,.0f} ft", low_few.source
        item.notes.append("Broken layer below clearance height — stay 500 ft below and 2,000 ft horizontally from any cloud.")
    elif worst:
        item.value, item.source = f"ceiling {worst.value:,.0f} ft", worst.source
    else:
        item.value, item.source = "no ceiling", c.station_id if m else "TAF"
    return item


def check_wind(c: _Ctx) -> CheckItem:
    lim = c.limits.max_wind_mph
    item = CheckItem("wind", "Surface wind", GO, limit=f"≤ {lim:g} mph", rule="personal minimum")
    samples: list[_Sample] = []
    if c.weather:
        samples.append(_Sample(_mph(c.weather.current.wind_speed, c.units), "model now"))
        item.series = [_mph(h.wind_speed, c.units) for h in c.hours if h.wind_speed is not None]
        samples += [_Sample(v, "model forecast") for v in item.series]
    m = c.metar
    if m and m.wind_speed_kt is not None:
        samples.append(_Sample(_kt(m.wind_speed_kt), c.station_id))
    for eff, temp in c.taf():
        if eff.wind_speed_kt is not None:
            samples.append(_Sample(_kt(eff.wind_speed_kt), "TAF " + ("TEMPO/PROB" if temp else "prevailing"), temp))
    item.status, worst = _judge(samples, lambda v: _limit_status(v, lim), _higher)
    if worst:
        item.value, item.source = f"{worst.value:.0f} mph", worst.source
    return item


def check_gusts(c: _Ctx) -> CheckItem:
    lim = c.limits.max_gust_mph
    item = CheckItem("gust", "Gusts", GO, limit=f"≤ {lim:g} mph", rule="personal minimum")
    samples: list[_Sample] = []
    if c.weather:
        samples.append(_Sample(_mph(c.weather.current.wind_gust, c.units), "model now"))
        item.series = [_mph(h.wind_gust, c.units) for h in c.hours if h.wind_gust is not None]
        samples += [_Sample(v, "model forecast") for v in item.series]
    m = c.metar
    if m and m.wind_gust_kt is not None:
        samples.append(_Sample(_kt(m.wind_gust_kt), c.station_id))
    taf = c.taf()
    for eff, temp in taf:
        if eff.wind_gust_kt is not None:
            samples.append(_Sample(_kt(eff.wind_gust_kt), "TAF " + ("TEMPO/PROB" if temp else "prevailing"), temp))
    if not samples and m is None:
        item.status, item.value = UNKNOWN, "no data"
        return item
    item.status, worst = _judge(samples, lambda v: _limit_status(v, lim), _higher)
    if worst:
        item.value, item.source = f"{worst.value:.0f} mph", worst.source
    else:
        item.status, item.value, item.source = GO, "none reported", c.station_id
    if m and m.wind_gust_kt and m.wind_speed_kt is not None and m.wind_gust_kt - m.wind_speed_kt >= GUST_SPREAD_KT:
        item.notes.append(f"Gust spread {m.wind_gust_kt - m.wind_speed_kt} kt at {c.station_id} — expect turbulence near obstacles.")
        item.status = max(item.status, CAUTION, key=lambda s: STATUS_RANK[s])
    shear = next((eff.wind_shear for eff, _ in taf if eff.wind_shear), "")
    if shear:
        item.notes.append(f"Low-level wind shear in the TAF ({shear}).")
        item.status = max(item.status, CAUTION, key=lambda s: STATUS_RANK[s])
    return item


def check_wind_aloft(c: _Ctx) -> CheckItem:
    lim = c.limits.max_wind_aloft_mph
    item = CheckItem("aloft", "Wind at 400 ft", GO, limit=f"≤ {lim:g} mph", rule="personal minimum",
                     source="model 120 m")
    item.series = [_mph(h.wind_120m, c.units) for h in c.hours if h.wind_120m is not None]
    if not item.series:
        item.status, item.value = UNKNOWN, "no model data"
        return item
    peak = max(item.series)
    item.status, item.value = _limit_status(peak, lim), f"{peak:.0f} mph"
    item.notes.append("Wind usually strengthens with height; return-to-home may be slower into it.")
    return item


def _precip_wx(wx: str) -> bool:
    return any(code in tok for tok in wx.split() if not tok.startswith("VC") for code in _PRECIP)


def check_precip(c: _Ctx) -> CheckItem:
    lim = c.limits.max_precip_prob
    item = CheckItem("precip", "Precipitation", GO, value="dry", limit=f"none · < {lim}% chance",
                     rule="aircraft limits")
    now_sources = []
    w, m = c.weather, c.metar
    if w and (w.current.precip > 0 or wmo.condition(w.current.code).scene in ("drizzle", "rain", "freezing", "snow", "thunder")):
        now_sources.append(("model now", wmo.condition(w.current.code).label))
    if m and _precip_wx(m.wx):
        now_sources.append((c.station_id, m.wx))
    if now_sources:
        src, what = now_sources[0]
        item.value, item.source = what.lower(), src
        item.status = CAUTION if c.limits.allow_precip else NOGO
        return item
    if w is None and m is None:
        item.status, item.value = UNKNOWN, "no data"
        return item
    item.series = [h.precip_prob for h in c.hours]
    peak = max(c.hours, key=lambda h: h.precip_prob, default=None)
    if peak and peak.precip_prob >= lim:
        item.status = CAUTION
        item.value, item.source = f"{peak.precip_prob:.0f}% at {_clock(peak.time, c.loc.timezone)}", "model forecast"
    taf_wx = next((eff.wx for eff, _ in c.taf() if _precip_wx(eff.wx)), "")
    if taf_wx:
        item.status = CAUTION
        item.notes.append(f"TAF forecasts {taf_wx} in the window.")
        if item.value == "dry":
            item.value, item.source = taf_wx, "TAF"
    if item.status == GO and peak:
        item.value, item.source = f"dry · ≤ {peak.precip_prob:.0f}%", "model forecast"
    return item


def _hazards_here(c: _Ctx):
    """(hazard, contains, distance NM) for unexpired advisories, measured from the flight location."""
    hs = c.av.hazards.data if c.av else None
    out = []
    for h in hs.items if hs else []:
        if h.valid_to is not None and h.valid_to <= c.now or not h.polygon:
            continue
        closed = not h.is_line
        inside = closed and A.point_in_polygon(c.loc.lat, c.loc.lon, h.polygon)
        out.append((h, inside, A.distance_to_shape_nm(c.loc.lat, c.loc.lon, h.polygon, closed=closed)))
    return out


def check_thunder(c: _Ctx) -> CheckItem:
    item = CheckItem("ts", "Thunderstorms", GO, value="none", limit="none within 50 NM", rule="aircraft limits")
    w, m = c.weather, c.metar
    if m and "TS" in m.wx:
        item.status, item.value, item.source = NOGO, m.wx, c.station_id
    elif w and w.current.code in _THUNDER_CODES:
        item.status, item.value, item.source = NOGO, wmo.condition(w.current.code).label.lower(), "model now"
    convective = [(h, inside, d) for h, inside, d in _hazards_here(c)
                  if h.kind == "CONVECTIVE SIGMET" or h.hazard == "TS"]
    over = [h for h, inside, _ in convective if inside]
    near = [(h, d) for h, inside, d in convective if not inside and d is not None and d <= A.HAZARD_NEAR_NM]
    if over:
        item.status = NOGO
        item.notes += [f"{h.name} covers this location." for h in over]
        if item.value == "none":
            item.value, item.source = over[0].name, over[0].kind
    if near:
        item.notes += [f"{h.name} {d:.0f} NM away." for h, d in near]
        if item.status == GO:
            item.status, item.value, item.source = CAUTION, f"{near[0][0].name} {near[0][1]:.0f} NM", near[0][0].kind
    forecast = any(h.code in _THUNDER_CODES for h in c.hours) or any("TS" in eff.wx for eff, _ in c.taf())
    if forecast:
        item.notes.append("Thunderstorms forecast in the window.")
        if item.status == GO:
            item.status, item.value, item.source = CAUTION, "forecast", "model/TAF"
    if w is None and m is None and c.av is None:
        item.status, item.value = UNKNOWN, "no data"
    return item


def check_icing(c: _Ctx) -> CheckItem:
    item = CheckItem("ice", "Icing", GO, value="no risk", limit=f"no moisture at ≤ {ICING_TEMP_C}°C", rule="aircraft limits")
    w, m = c.weather, c.metar
    temp_c = None
    if w:
        tf = _temp_f(w.current.temp, c.units)
        temp_c = (tf - 32) * 5 / 9
    elif m and m.temp_c is not None:
        temp_c = m.temp_c
    wx = m.wx if m else ""
    if (m and any(code in wx for code in _FREEZING_PRECIP)) or (w and w.current.code in _FREEZING_CODES):
        item.status, item.value = NOGO, "freezing precipitation"
        item.source = c.station_id if m and any(code in wx for code in _FREEZING_PRECIP) else "model now"
        return item
    if temp_c is None:
        item.status, item.value = UNKNOWN, "no temperature"
        return item
    if temp_c > ICING_TEMP_C:
        item.source = "model now" if w else c.station_id
        return item
    moisture = []
    if w and wmo.condition(w.current.code).scene in ("fog", "drizzle", "rain", "snow", "freezing"):
        moisture.append(wmo.condition(w.current.code).label.lower())
    if "FG" in wx or "BR" in wx or _precip_wx(wx):
        moisture.append(wx)
    if m and m.temp_c is not None and m.dewp_c is not None and m.temp_c - m.dewp_c <= 2:
        moisture.append(f"dew-point spread {m.temp_c - m.dewp_c:.0f}°C")
    if moisture:
        item.status, item.value, item.source = CAUTION, f"{temp_c:.0f}°C with {moisture[0]}", "model/METAR"
        item.notes.append("Visible moisture near freezing can ice propellers within minutes.")
    else:
        item.value = f"{temp_c:.0f}°C, dry"
    return item


def check_temperature(c: _Ctx) -> CheckItem:
    lo, hi = c.limits.min_temp_f, c.limits.max_temp_f
    item = CheckItem("temp", "Temperature", GO, limit=f"{lo:g}–{hi:g}°F", rule="battery limits")
    samples: list[_Sample] = []
    if c.weather:
        samples.append(_Sample(_temp_f(c.weather.current.temp, c.units), "model now"))
        item.series = [_temp_f(h.temp, c.units) for h in c.hours]
        samples += [_Sample(v, "model forecast") for v in item.series]
    m = c.metar
    if m and m.temp_c is not None:
        samples.append(_Sample(_c_to_f(m.temp_c), c.station_id))
    if not samples:
        item.status, item.value = UNKNOWN, "no data"
        return item
    cold = min(samples, key=lambda s: s.value)
    hot = max(samples, key=lambda s: s.value)
    item.value = f"{cold.value:.0f}°F" if round(cold.value) == round(hot.value) else f"{cold.value:.0f}–{hot.value:.0f}°F"
    if cold.value < lo or hot.value > hi:
        bad = cold if cold.value < lo else hot
        item.status, item.source = NOGO, bad.source
    elif cold.value < COLD_BATTERY_F:
        item.status, item.source = CAUTION, cold.source
        item.notes.append("Cold batteries sag under load — keep them warm and land with extra reserve.")
    elif hot.value > hi - HOT_MARGIN_F:
        item.status, item.source = CAUTION, hot.source
        item.notes.append("Near the aircraft's upper limit — watch battery and motor temperatures.")
    else:
        item.source = "model" if c.weather else c.station_id
    return item


def check_kp(c: _Ctx) -> CheckItem:
    lim = c.limits.max_kp
    item = CheckItem("kp", "Kp index (GNSS)", GO, limit=f"< {lim:g}", rule="aircraft limits", source="NOAA SWPC")
    sw = c.space
    if sw is None or not sw.kp:
        item.status, item.value = UNKNOWN, "no data"
        return item
    past = [p for p in sw.kp if p.time <= c.now]
    pts = ([past[-1]] if past else []) + [p for p in sw.kp if c.now < p.time <= c.end]
    pts = pts or [sw.kp[-1]]
    peak = max(pts, key=lambda p: p.kp)
    item.value = f"{peak.kp:.1f}" + (" forecast" if peak.kind == "predicted" else "")
    if peak.kp >= lim + 1:
        item.status = NOGO
    elif peak.kp >= lim:
        item.status = CAUTION
    if item.status != GO:
        item.notes.append("Geomagnetic storms degrade GPS positioning and compass headings — expect drift, fly in ATTI-ready mode.")
    return item


def check_alerts(c: _Ctx) -> CheckItem:
    item = CheckItem("alerts", "NWS alerts", GO, value="none", limit="none active", source="NWS")
    res = c.alerts
    if res is None:
        item.status, item.value = UNKNOWN, "loading"
        return item
    if not res.supported:
        item.status, item.value = UNKNOWN, "not available here"
        return item
    if not res.alerts:
        return item
    events = list(dict.fromkeys(a.event for a in res.alerts))
    item.value = events[0] + (f" +{len(events) - 1}" if len(events) > 1 else "")
    item.status = NOGO if max(a.rank for a in res.alerts) >= 3 else CAUTION
    item.notes += [a.headline or a.event for a in res.alerts[:4]]
    return item


def check_advisories(c: _Ctx) -> CheckItem:
    item = CheckItem("advisories", "Low-level advisories", GO, value="none", limit="none over this location",
                     source="AWC")
    if c.av is None or not c.av.hazards.loaded:
        item.status, item.value = UNKNOWN, "loading"
        return item
    if c.av.hazards.data is None:
        item.status, item.value = UNKNOWN, c.av.hazards.error or "unavailable"
        return item
    hits = []
    for h, inside, _ in _hazards_here(c):
        if not inside or h.kind == "CONVECTIVE SIGMET" or h.hazard == "TS":
            continue
        low = h.base_ft is None or h.base_ft <= LOW_LEVEL_FT or h.base_text.upper() in ("SFC", "FZL")
        if low and (h.hazard in _LOW_LEVEL_HAZARDS or "SIGMET" in h.kind):
            hits.append(h)
    if hits:
        item.status = CAUTION
        item.value = ", ".join(dict.fromkeys(h.hazard for h in hits))
        item.notes += [f"{h.name} · {h.hazard}" for h in hits]
    if c.av.hazards.stale:
        item.notes.append("Advisories are from the offline cache.")
    return item


def check_density_altitude(c: _Ctx) -> CheckItem:
    item = CheckItem("da", "Density altitude", GO, limit=f"< {HIGH_DA_FT:,} ft", rule="performance")
    m, st = c.metar, c.av.station if c.av else None
    elev_m = st.elev_m if st and st.elev_m is not None else (m.elev_m if m else None)
    if m is None or m.temp_c is None or m.altimeter_hpa is None or elev_m is None:
        item.status, item.value = UNKNOWN, "no METAR"
        return item
    da = A.density_altitude_ft(elev_m * 3.28084, m.temp_c, m.altimeter_hpa, m.dewp_c)
    item.value, item.source = f"{da:,.0f} ft", c.station_id
    if da >= HIGH_DA_FT:
        item.status = CAUTION
        item.notes.append("Thin air: less thrust and lift, shorter flight time, slower climbs.")
    return item


def check_station(c: _Ctx) -> CheckItem:
    item = CheckItem("station", "Observation station", GO, limit=f"≤ {NEAR_STATION_NM} NM, ≤ {METAR_OLD_MIN} min old",
                     rule="data quality")
    av = c.av
    if av is None or av.choice is None:
        item.status, item.value = UNKNOWN, "finding station"
        return item
    st = av.station
    if st is None:
        item.status, item.value = UNKNOWN, av.choice.error or "none nearby"
        item.notes.append("Ceiling and visibility can't be confirmed from a report — judge them on site.")
        return item
    m = av.metar
    dist = av.choice.distance_nm
    item.source = st.icao
    if m is None:
        item.status, item.value = UNKNOWN, av.metars.error or "no METAR"
        return item
    age = (c.now - m.obs_time).total_seconds() / 60
    item.value = f"{st.icao} {dist:.0f} NM · {age:.0f}m old" if dist is not None else f"{st.icao} · {age:.0f}m old"
    if dist is not None and dist > NEAR_STATION_NM:
        item.status = CAUTION
        item.notes.append(f"{st.icao} is {dist:.0f} NM away — verify ceiling, visibility and wind on site.")
    if age > METAR_OLD_MIN:
        item.status = CAUTION
        item.notes.append(f"Latest METAR is {age:.0f} minutes old.")
    if av.metars.stale:
        item.status = CAUTION
        item.notes.append("Offline — showing cached observations.")
    return item


CHECKS = (
    check_daylight, check_visibility, check_clouds, check_wind, check_gusts, check_wind_aloft,
    check_precip, check_thunder, check_icing, check_temperature, check_kp, check_alerts,
    check_advisories, check_density_altitude, check_station,
)


def evaluate(
    loc: Location,
    weather: Weather | None,
    alerts: AlertsResult | None,
    av: AviationResult | None,
    space: SpaceWeather | None,
    limits: DroneLimits,
    ticks: set[str],
    now: datetime,
) -> GoNoGo:
    """Score every automatic check for now through now + window_hours."""
    end = now + timedelta(hours=limits.window_hours)
    ctx = _Ctx(loc, weather, alerts, av, space, limits, now, end)
    items = [check(ctx) for check in CHECKS]
    done = sum(1 for m in MANUAL_ITEMS if m.id in ticks)
    return GoNoGo(items, now, end, limits.altitude_ft, loc.timezone, done, len(MANUAL_ITEMS))
