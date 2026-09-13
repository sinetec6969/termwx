"""Plain data containers shared by the API layer and the UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


US_STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}


@dataclass
class Location:
    name: str
    lat: float
    lon: float
    region: str = ""
    country: str = ""
    country_code: str = ""
    timezone: str = ""

    @property
    def label(self) -> str:
        parts = [self.name]
        if self.region and self.region != self.name:
            parts.append(US_STATE_ABBR.get(self.region, self.region) if self.country_code == "US" else self.region)
        if self.country_code and self.country_code != "US":
            parts.append(self.country_code)
        return ", ".join(parts)

    @property
    def coords(self) -> str:
        ns = "N" if self.lat >= 0 else "S"
        ew = "E" if self.lon >= 0 else "W"
        return f"{abs(self.lat):.2f}°{ns} {abs(self.lon):.2f}°{ew}"

    def same_place(self, other: Location | None) -> bool:
        return other is not None and abs(self.lat - other.lat) < 0.01 and abs(self.lon - other.lon) < 0.01

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "region": self.region,
            "country": self.country,
            "country_code": self.country_code,
            "timezone": self.timezone,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Location:
        return cls(
            name=str(d.get("name", "")),
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            region=str(d.get("region", "")),
            country=str(d.get("country", "")),
            country_code=str(d.get("country_code", "")),
            timezone=str(d.get("timezone", "")),
        )


@dataclass
class Current:
    time: datetime
    temp: float
    feels_like: float
    humidity: float
    dew_point: float
    code: int
    is_day: bool
    wind_speed: float
    wind_dir: float
    wind_gust: float
    pressure: float  # always hPa from the API
    cloud_cover: float
    precip: float
    visibility: float  # always meters from the API
    uv: float


@dataclass
class Hour:
    time: datetime
    temp: float
    precip_prob: float
    code: int
    pressure: float


@dataclass
class Day:
    date: date
    code: int
    temp_max: float
    temp_min: float
    precip_prob: float
    precip_sum: float
    wind_max: float
    sunrise: datetime | None
    sunset: datetime | None
    uv_max: float


@dataclass
class Weather:
    location: Location
    units: str
    current: Current
    hours: list[Hour]
    days: list[Day]
    aqi: int | None = None
    timezone_abbr: str = ""
    fetched_at: datetime = field(default_factory=datetime.now)
    stale: bool = False

    @property
    def today(self) -> Day | None:
        return self.days[0] if self.days else None

    @property
    def pressure_trend(self) -> float:
        """hPa change over the next 3 hours (positive = rising)."""
        if len(self.hours) < 4:
            return 0.0
        return self.hours[3].pressure - self.hours[0].pressure


SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}


@dataclass
class Alert:
    id: str
    event: str
    severity: str
    headline: str
    description: str = ""
    instruction: str = ""
    urgency: str = ""
    certainty: str = ""
    area: str = ""
    sender: str = ""
    onset: datetime | None = None
    ends: datetime | None = None
    expires: datetime | None = None
    source: str = "NWS"

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 0)


@dataclass
class AlertsResult:
    alerts: list[Alert]
    supported: bool = True
    stale: bool = False
    error: str = ""
    fetched_at: datetime = field(default_factory=datetime.now)


@dataclass
class KpPoint:
    time: datetime
    kp: float
    kind: str  # observed | estimated | predicted


@dataclass
class ScaleOutlook:
    date: date
    g: int | None
    r_minor_prob: int | None
    r_major_prob: int | None
    s_prob: int | None


@dataclass
class SwpcMessage:
    product_id: str
    issued: datetime
    code: str
    title: str
    body: str
    scale: str = ""  # e.g. "G3" when the message carries a NOAA scale

    @property
    def level(self) -> int:
        return int(self.scale[1:]) if len(self.scale) == 2 and self.scale[1].isdigit() else 0


@dataclass
class SpaceWeather:
    g_now: int = 0
    s_now: int = 0
    r_now: int = 0
    outlook: list[ScaleOutlook] = field(default_factory=list)
    kp: list[KpPoint] = field(default_factory=list)
    wind_speed: float | None = None
    wind_density: float | None = None
    bt: float | None = None
    bz: float | None = None
    bz_history: list[float] = field(default_factory=list)
    speed_history: list[float] = field(default_factory=list)
    flare_current: str = ""
    flare_max: str = ""
    flare_max_time: datetime | None = None
    f107: float | None = None
    messages: list[SwpcMessage] = field(default_factory=list)
    aurora_overhead: int | None = None
    aurora_nearby: int | None = None
    aurora_time: datetime | None = None
    errors: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)
    stale: bool = False

    @property
    def kp_now(self) -> KpPoint | None:
        now = datetime.now().astimezone()
        past = [p for p in self.kp if p.time <= now]
        return past[-1] if past else (self.kp[0] if self.kp else None)

    @property
    def kp_max_forecast(self) -> KpPoint | None:
        future = [p for p in self.kp if p.kind == "predicted"]
        return max(future, key=lambda p: p.kp) if future else None
