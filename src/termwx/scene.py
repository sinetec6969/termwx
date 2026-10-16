"""Animated ASCII weather scene.

`render_scene` is a pure function of (conditions, size, tick) so the TUI can
animate it and the --once snapshot can print a single frame.
"""

from __future__ import annotations

import math
import textwrap
from dataclasses import dataclass

from rich.text import Text

from . import wmo
from .models import Quip

FPS = 8

SUN_FRAMES = [
    [r"  \ | /  ", r" ― ( ) ― ", r"  / | \  "],
    [r"   \|/   ", r"  ―( )―  ", r"   /|\   "],
]
MOON = [r" .-.  ", r"(  (  ", r" `-'  "]
CLOUD_BIG = [r"    .--.    ", r" .-(    ).  ", r"(___.__)__) "]
CLOUD_SMALL = [r"  .-.    ", r" (   ).  ", r"(___(__) "]
HOUSE = [r"  /\  ", r" /__\ ", r" |▪▪| "]
TREE = [r" ^ ", r"/^\ ", r" | "]
RUNNER = [[r" o ", r"/|\ ", r"/ \ "], [r" o ", r"<|\ ", r" |\ "]]
HOLDER = [[r"\o/", r" | ", r"/ \ "], [r"\o/", r" | ", r" | "]]

# sky visitor timings, in ticks
RUN_IN = 3 * FPS
RUN_OUT = 2 * FPS
SIGN_POP = 4
SIGN_MAX_W = 44
SIGN_BOARD = "#e8d9b5"
SIGN_BORDER = f"#8b5e34 on {SIGN_BOARD}"
SIGN_HEADER = f"bold #8b5e34 on {SIGN_BOARD}"
SIGN_TEXT = f"bold #1c1917 on {SIGN_BOARD}"
SIGN_REVEAL = f"bold #b42318 on {SIGN_BOARD}"
SIGN_CREDIT = f"italic #6b5a45 on {SIGN_BOARD}"
VISITOR_BODY = "bold #ff7b72"
VISITOR_HEAD = "bold #ffd8b0"


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


@dataclass
class Visitor:
    """A little character who runs out of the house, holds up a sign, and runs off."""

    quip: Quip
    start: int = 0

    @property
    def reveal_at(self) -> int:
        """Ticks into the hold before the punchline appears."""
        return int(FPS * (3 + len(self.quip.text) / 15)) if self.quip.reveal else 0

    @property
    def hold(self) -> int:
        q = self.quip
        return self.reveal_at + int(FPS * (4 + (len(q.reveal) + len(q.credit) + (0 if q.reveal else len(q.text))) / 15))

    @property
    def duration(self) -> int:
        return RUN_IN + self.hold + RUN_OUT

    def done(self, tick: int) -> bool:
        return tick - self.start >= self.duration


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


def _draw_figure(cv: Canvas, x: int, y: int, frame: list[str]) -> None:
    cv.sprite(x, y, frame, VISITOR_BODY, opaque=False)
    cv.put(x + 1, y, "o", VISITOR_HEAD)


def _sign_lines(q: Quip, inner: int, revealed: bool) -> list[tuple[str, str]]:
    lines = [(line, SIGN_TEXT) for line in textwrap.wrap(q.text, inner)]
    if q.reveal and revealed:
        lines += [(line, SIGN_REVEAL) for line in textwrap.wrap(q.reveal, inner)]
    if q.credit:
        lines += [(line, SIGN_CREDIT) for line in textwrap.wrap(f"— {q.credit}", inner)]
    return lines


def _draw_sign(cv: Canvas, v: Visitor, t: int, center: int, bottom: int) -> None:
    """Sign whose bottom edge sits on row `bottom`; `t` is ticks into the hold."""
    inner_max = min(cv.w, SIGN_MAX_W) - 4
    rows_max = bottom - 1
    if inner_max < 6 or rows_max < 1:
        return
    q = v.quip
    full = _sign_lines(q, inner_max, revealed=True)
    header = q.kind[: inner_max - 2]
    inner = max(max(len(s) for s, _ in full), len(header) + 2)
    rows = min(len(full), rows_max)
    revealed = t >= v.reveal_at
    lines = _sign_lines(q, inner_max, revealed)

    # scroll long text through the window as the reader goes, and make sure a
    # freshly revealed punchline is on screen
    offset = 0
    overflow = len(lines) - rows
    if overflow > 0:
        progress = (t - 2 * FPS) / max(1, v.hold - 4 * FPS)
        offset = round(overflow * min(1.0, max(0.0, progress)))
        if revealed and q.reveal:
            first_reveal = len(textwrap.wrap(q.text, inner_max))
            offset = max(offset, first_reveal + 1 - rows)
        offset = min(offset, overflow)
    shown = lines[offset : offset + rows]

    sign_w = inner + 4
    left = min(max(0, center - sign_w // 2), cv.w - sign_w)
    top = bottom - rows - 1
    # pop up from the hands
    visible_top = bottom - (rows + 2) * (t + 1) // SIGN_POP + 1 if t < SIGN_POP else top

    def row(y: int, chars: str, styles: list[str]) -> None:
        if y >= max(top, visible_top):
            for dx, (ch, st) in enumerate(zip(chars, styles)):
                cv.put(left + dx, y, ch, st)

    title = f" {header} "
    border_top = "╭─" + title + "─" * (sign_w - 3 - len(title)) + "╮"
    row(top, border_top, [SIGN_BORDER, SIGN_BORDER] + [SIGN_HEADER] * len(title) + [SIGN_BORDER] * (sign_w - 2 - len(title)))
    for i in range(rows):
        text, style = shown[i] if i < len(shown) else ("", SIGN_TEXT)
        text = text.rjust(inner) if style == SIGN_CREDIT else text.center(inner)
        row(top + 1 + i, "│ " + text + " │", [SIGN_BORDER, SIGN_BORDER] + [style] * inner + [SIGN_BORDER, SIGN_BORDER])
    row(bottom, "╰" + "─" * (sign_w - 2) + "╯", [SIGN_BORDER] * sign_w)


def _draw_visitor(cv: Canvas, v: Visitor, tick: int, ground: int, door: int) -> None:
    t = tick - v.start
    y = ground - 2
    stop = max(0, min((cv.w - 3) // 2, door - 10))  # mid-sky, but clear of the tree
    if t < 0:
        return
    if t < RUN_IN:
        _draw_figure(cv, round(door + (stop - door) * t / RUN_IN), y, RUNNER[(t // 2) % 2])
        return
    t -= RUN_IN
    if t < v.hold:
        _draw_sign(cv, v, t, stop + 1, ground - 3)
        _draw_figure(cv, stop, y, HOLDER[(t // FPS) % 2])
        return
    t -= v.hold
    if t < RUN_OUT:
        _draw_figure(cv, round(stop + (-4 - stop) * t / RUN_OUT), y, RUNNER[(t // 2) % 2])


def render_scene(state: SceneState, width: int, height: int, tick: int, visitor: Visitor | None = None) -> Text:
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
    if visitor is not None and h >= 6:
        _draw_visitor(cv, visitor, tick, ground, door=w - 7 if w >= 24 else w)
    return cv.to_text()
