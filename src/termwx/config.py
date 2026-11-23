"""User settings stored in ~/.config/termwx/config.toml."""

from __future__ import annotations

import json
import math
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .api.sats import MAX_HOURS, MIN_HOURS
from .drone import DroneLimits
from .models import Location
from .units import IMPERIAL, METRIC


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "termwx" / "config.toml"


@dataclass
class SatSettings:
    """The [sats] table: the FM/APRS satellite tracker."""

    panel: bool = True
    tle_refresh_hours: float = 3.0
    min_elevation: float = 10.0
    # only these satellites (names, OSCAR numbers or NORAD numbers); empty = every FM/APRS satellite
    track: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SatSettings:
        out = cls()
        if isinstance(d.get("panel"), bool):
            out.panel = d["panel"]
        for key in ("tle_refresh_hours", "min_elevation"):
            v = d.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                setattr(out, key, float(v))
        track = d.get("track")
        if isinstance(track, list):
            items = (str(t).strip() for t in track if isinstance(t, (str, int)) and not isinstance(t, bool))
            out.track = [t for t in items if t]
        return out.clamped()

    def clamped(self) -> SatSettings:
        self.tle_refresh_hours = max(MIN_HOURS, min(MAX_HOURS, self.tle_refresh_hours))
        self.min_elevation = max(0.0, min(80.0, self.min_elevation))
        return self


@dataclass
class Config:
    units: str = IMPERIAL
    location: Location | None = None
    favorites: list[Location] = field(default_factory=list)
    desktop_notify: bool = True
    bell: bool = True
    animate: bool = True
    visitor: bool = True
    aviation_station: str = ""  # explicit ICAO override; empty = nearest METAR station
    drone: DroneLimits = field(default_factory=DroneLimits)
    sats: SatSettings = field(default_factory=SatSettings)
    path: Path = field(default_factory=default_path)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or default_path()
        cfg = cls(path=path)
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return cfg
        if data.get("units") in (IMPERIAL, METRIC):
            cfg.units = data["units"]
        for key in ("desktop_notify", "bell", "animate", "visitor"):
            if isinstance(data.get(key), bool):
                setattr(cfg, key, data[key])
        if isinstance(data.get("aviation_station"), str):
            cfg.aviation_station = data["aviation_station"].strip().upper()
        if isinstance(data.get("drone"), dict):
            cfg.drone = DroneLimits.from_dict(data["drone"])
        if isinstance(data.get("sats"), dict):
            cfg.sats = SatSettings.from_dict(data["sats"])
        try:
            if "location" in data:
                cfg.location = Location.from_dict(data["location"])
            cfg.favorites = [Location.from_dict(f) for f in data.get("favorites", [])]
        except (KeyError, TypeError, ValueError):
            pass
        return cfg

    def save(self) -> None:
        lines = [
            "# termwx settings",
            f"units = {_q(self.units)}",
            f"desktop_notify = {_b(self.desktop_notify)}",
            f"bell = {_b(self.bell)}",
            f"animate = {_b(self.animate)}",
            f"visitor = {_b(self.visitor)}",
        ]
        if self.aviation_station:
            lines.append(f"aviation_station = {_q(self.aviation_station)}")
        lines.append("")
        lines += ["[drone]", *(f"{k} = {_v(v)}" for k, v in self.drone.to_dict().items()), ""]
        lines += ["[sats]", *(f"{k} = {_v(v)}" for k, v in self.sats.to_dict().items()), ""]
        if self.location:
            lines += ["[location]", *_table(self.location), ""]
        for fav in self.favorites:
            lines += ["[[favorites]]", *_table(fav), ""]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines))
        tmp.replace(self.path)

    def is_favorite(self, loc: Location) -> bool:
        return any(loc.same_place(f) for f in self.favorites)

    def toggle_favorite(self, loc: Location) -> bool:
        """Add or remove; returns True if it is now a favorite."""
        if self.is_favorite(loc):
            self.favorites = [f for f in self.favorites if not loc.same_place(f)]
            return False
        self.favorites.append(loc)
        return True


def _q(s: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes.
    return json.dumps(s, ensure_ascii=False)


def _b(v: bool) -> str:
    return "true" if v else "false"


def _v(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(_v(x) for x in v) + "]"
    if isinstance(v, str):
        return _q(v)
    return _b(v) if isinstance(v, bool) else f"{v:g}" if isinstance(v, float) else str(v)


def _table(loc: Location) -> list[str]:
    out = []
    for k, v in loc.to_dict().items():
        out.append(f"{k} = {v!r}" if isinstance(v, float) else f"{k} = {_q(str(v))}")
    return out
