"""User settings stored in ~/.config/termwx/config.toml."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .models import Location
from .units import IMPERIAL, METRIC


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "termwx" / "config.toml"


@dataclass
class Config:
    units: str = IMPERIAL
    location: Location | None = None
    favorites: list[Location] = field(default_factory=list)
    desktop_notify: bool = True
    bell: bool = True
    animate: bool = True
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
        for key in ("desktop_notify", "bell", "animate"):
            if isinstance(data.get(key), bool):
                setattr(cfg, key, data[key])
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
            "",
        ]
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


def _table(loc: Location) -> list[str]:
    out = []
    for k, v in loc.to_dict().items():
        out.append(f"{k} = {v!r}" if isinstance(v, float) else f"{k} = {_q(str(v))}")
    return out
