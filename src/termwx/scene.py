"""Animated ASCII weather scene.

`render_scene` is a pure function of (conditions, size, tick) so the TUI can
animate it and the --once snapshot can print a single frame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rich.text import Text

from . import wmo

SUN_FRAMES = [
    [r"  \ | /  ", r" ― ( ) ― ", r"  / | \  "],
    [r"   \|/   ", r"  ―( )―  ", r"   /|\   "],
]
MOON = [r" .-.  ", r"(  (  ", r" `-'  "]
CLOUD_BIG = [r"    .--.    ", r" .-(    ).  ", r"(___.__)__) "]
CLOUD_SMALL = [r"  .-.    ", r" (   ).  ", r"(___(__) "]
HOUSE = [r"  /\  ", r" /__\ ", r" |▪▪| "]
TREE = [r" ^ ", r"/^\ ", r" | "]


@dataclass
class SceneState:
    scene: str = "clear"
    intensity: int = 1
    is_day: bool = True
    wind_speed_kmh: float = 5.0
    wind_dir: float = 270.0
    cloud_cover: float = 0.0

    @classmethod
    def from_weather(cls, code: int, is_day: bool, wind_speed: float, wind_dir: float, cloud_cover: float, units: str):
        cond = wmo.condition(code)
        kmh = wind_speed * 1.609 if units == "imperial" else wind_speed
        return cls(cond.scene, cond.intensity, is_day, kmh, wind_dir, cloud_cover)


def _rand(i: int) -> float:
    """Deterministic hash → [0, 1)."""
    i = (i * 2654435761 + 0x9E3779B9) & 0xFFFFFFFF
    i ^= i >> 16
    i = (i * 0x45D9F3B) & 0xFFFFFFFF
    i ^= i >> 16
    return i / 2**32


class Canvas:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.chars = [[" "] * w for _ in range(h)]
        self.styles: list[list[str]] = [[""] * w for _ in range(h)]

    def put(self, x: int, y: int, ch: str, style: str) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.chars[y][x] = ch
            self.styles[y][x] = style

    def get(self, x: int, y: int) -> str:
        return self.chars[y][x] if 0 <= x < self.w and 0 <= y < self.h else ""

    def sprite(self, x: int, y: int, lines: list[str], style: str, opaque: bool = True) -> None:
        for dy, line in enumerate(lines):
            stripped = line.rstrip()
            first = len(line) - len(line.lstrip())
            for dx, ch in enumerate(stripped):
                if ch != " " or (opaque and dx >= first):
                    self.put(x + dx, y + dy, ch, style)

    def to_text(self) -> Text:
        out = Text(no_wrap=True, overflow="crop")
        for y in range(self.h):
            run, run_style = "", None
            for x in range(self.w):
                st = self.styles[y][x]
                if st != run_style and run:
                    out.append(run, style=run_style or "")
                    run = ""
                run_style = st
                run += self.chars[y][x]
            if run:
                out.append(run, style=run_style or "")
            if y < self.h - 1:
                out.append("\n")
        return out


def render_scene(state: SceneState, width: int, height: int, tick: int) -> Text:
    w, h = max(10, width), max(5, height)
    cv = Canvas(w, h)
    scene = state.scene
    ground = h - 1
    stormy = scene in ("rain", "thunder", "snow", "freezing", "drizzle")
    flash = scene == "thunder" and (tick + 7) % 45 < 3

    # stars
    if not state.is_day:
        star_chars = [".", "·", "+", "✦", "·", "."]
        density = 0.06 * max(0.15, 1 - state.cloud_cover / 100)
        for i in range(w * (h - 3)):
            if _rand(i * 31 + 5) < density:
                x, y = i % w, i // w
                ch = star_chars[(tick // 5 + i) % len(star_chars)]
                cv.put(x, y, ch, "#e8e6ff" if ch in "+✦" else "#6e7681")

    def sky_body() -> None:
        if state.is_day:
            frame = SUN_FRAMES[(tick // 6) % 2]
            cv.sprite(1, 0, frame, "#ffd23f", opaque=scene != "cloudy")
            for dx, ch in enumerate(frame[1]):
                if ch in "()":
                    cv.put(1 + dx, 1, ch, "bold #ffe066")
        else:
            cv.sprite(2, 0, MOON, "#e8e6ff", opaque=scene != "cloudy")

    # sun / moon: peeks out behind clouds when overcast, in front otherwise
    if scene == "cloudy" and state.cloud_cover < 90:
        sky_body()

    # clouds
    wind_u = -math.sin(math.radians(state.wind_dir)) * state.wind_speed_kmh  # + eastward
    drift = 0.04 + min(state.wind_speed_kmh, 60) * 0.006
    direction = 1 if wind_u >= 0 else -1
    if scene == "clear":
        clouds = []
    elif scene == "partly":
        clouds = [(CLOUD_SMALL, 0), (CLOUD_BIG, 1)]
    elif scene == "fog":
        clouds = []
    else:
        clouds = [(CLOUD_BIG, 0), (CLOUD_BIG, 1), (CLOUD_SMALL, 2)]
        if w > 44:
            clouds.append((CLOUD_BIG, 3))
    if flash:
        cloud_style = "bold #ffffff"
    elif scene == "thunder":
        cloud_style = "#6e7681"
    elif stormy:
        cloud_style = "#8b949e"
    elif scene == "cloudy":
        cloud_style = "#b1bac4"
    else:
        cloud_style = "#e6edf3"
    span = w + 12
    for sprite, idx in clouds:
        base = _rand(idx * 97 + 3) * span
        x = int((base + direction * tick * drift * (1 + idx * 0.25)) % span) - 12
        y = 0 if idx % 2 == 0 else 1
        cv.sprite(x, y, sprite, cloud_style)

    if scene in ("clear", "partly"):
        sky_body()

    # birds on fair days
    if state.is_day and scene in ("clear", "partly") and h >= 8:
        for b in range(3):
            period = w + 20
            bx = int((_rand(b * 53 + 11) * period + tick * (0.35 + b * 0.1)) % period) - 10
            by = 3 + int(_rand(b * 7 + 1) * max(1, h - 7)) + (1 if (tick // 8 + b) % 4 == 0 else 0)
            flap = "v" if (tick // 3 + b) % 2 else "-"
            if cv.get(bx, by) == " ":
                cv.put(bx, by, flap, "#8b949e")

    # precipitation
    top = 3
    fall_rows = ground - top
    if scene in ("rain", "drizzle", "thunder", "snow", "freezing") and fall_rows > 0:
        slant = max(-0.8, min(0.8, wind_u / 35))
        density = {1: 0.35, 2: 0.55, 3: 0.8}.get(state.intensity, 0.5)
        per_col = 1 if scene == "drizzle" else min(3, state.intensity + 1)
        for c in range(w):
            for k in range(per_col):
                seed = c * 131 + k * 17
                if _rand(seed) > density:
                    continue
                kind = scene
                if scene == "freezing":
                    kind = "snow" if _rand(seed + 1) < 0.4 else "rain"
                speed = {"rain": 1.0, "thunder": 1.2, "drizzle": 0.5, "snow": 0.3}[kind]
                period = fall_rows + int(_rand(seed + 2) * 6)
                pos = tick * speed + _rand(seed + 3) * period
                y = top + int(pos) % period
                if y >= ground:
                    if y == ground and kind != "snow":
                        cv.put(int(c + pos * slant) % w, ground - 0, "˙", "#3d9df3")
                    continue
                if kind == "snow":
                    x = int(c + pos * slant * 0.5 + round(math.sin((tick + seed) / 4))) % w
                    ch, st = ("*" if _rand(seed + 4) < 0.5 else "·"), "#e6edf3"
                else:
                    x = int(c + pos * slant) % w
                    if kind == "drizzle":
                        ch, st = "'", "#6cb6ff"
                    else:
                        ch = "|" if abs(slant) < 0.2 else ("\\" if slant > 0 else "/")
                        st = "#3d9df3"
                if cv.get(x, y) == " ":
                    cv.put(x, y, ch, st)

    # lightning bolt
    if flash:
        bx = int(_rand((tick + 7) // 45) * (w - 10)) + 5
        x = bx
        for y in range(2, ground):
            step = -1 if _rand(y * 13 + bx) < 0.5 else 1
            cv.put(x, y, "/" if step < 0 else "\\", "bold #ffe066")
            x += step

    # fog bands
    if scene == "fog":
        pattern = "~ ─ ~~─  ── ~ ─── ~~  "
        for row in range(1, ground):
            if row % 2:
                continue
            shift = (tick // 4) * (1 if row % 4 == 0 else -1)
            for x in range(w):
                ch = pattern[(x + shift + row * 5) % len(pattern)]
                if ch != " ":
                    cv.put(x, row, ch, "#8b949e" if row % 4 == 0 else "#6e7681")

    # ground, tree and house
    snow_ground = scene == "snow"
    ground_style = "#e6edf3" if snow_ground else ("#2ea043" if state.is_day else "#1f5f33")
    for x in range(w):
        if cv.get(x, ground) in (" ", "˙"):
            cv.put(x, ground, "▁", ground_style if cv.get(x, ground) == " " else "#3d9df3")
    if w >= 24:
        hx = w - 8
        cv.sprite(hx, ground - 2, HOUSE, "#c9a27a" if state.is_day else "#8b6f52")
        win = "bold #ffd23f" if not state.is_day else "#6e7681"
        for dx, ch in enumerate(HOUSE[2]):
            if ch == "▪":
                cv.put(hx + dx, ground, ch, win)
        cv.sprite(hx - 5, ground - 2, TREE, "#3fb950" if state.is_day else "#2d6a3e")
        if snow_ground:
            cv.put(hx + 2, ground - 3, "_", "#e6edf3")
            cv.put(hx + 3, ground - 3, "_", "#e6edf3")
    return cv.to_text()
