"""WMO weather interpretation codes (as used by Open-Meteo)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Condition:
    label: str
    scene: str  # clear | partly | cloudy | fog | drizzle | rain | freezing | snow | thunder
    intensity: int = 1  # 1 light, 2 moderate, 3 heavy


CODES: dict[int, Condition] = {
    0: Condition("Clear", "clear"),
    1: Condition("Mostly Clear", "clear"),
    2: Condition("Partly Cloudy", "partly"),
    3: Condition("Overcast", "cloudy"),
    45: Condition("Fog", "fog"),
    48: Condition("Freezing Fog", "fog", 2),
    51: Condition("Light Drizzle", "drizzle", 1),
    53: Condition("Drizzle", "drizzle", 2),
    55: Condition("Heavy Drizzle", "drizzle", 3),
    56: Condition("Light Freezing Drizzle", "freezing", 1),
    57: Condition("Freezing Drizzle", "freezing", 2),
    61: Condition("Light Rain", "rain", 1),
    63: Condition("Rain", "rain", 2),
    65: Condition("Heavy Rain", "rain", 3),
    66: Condition("Light Freezing Rain", "freezing", 1),
    67: Condition("Freezing Rain", "freezing", 3),
    71: Condition("Light Snow", "snow", 1),
    73: Condition("Snow", "snow", 2),
    75: Condition("Heavy Snow", "snow", 3),
    77: Condition("Snow Grains", "snow", 1),
    80: Condition("Light Showers", "rain", 1),
    81: Condition("Showers", "rain", 2),
    82: Condition("Violent Showers", "rain", 3),
    85: Condition("Snow Showers", "snow", 1),
    86: Condition("Heavy Snow Showers", "snow", 3),
    95: Condition("Thunderstorm", "thunder", 2),
    96: Condition("Thunderstorm w/ Hail", "thunder", 3),
    99: Condition("Severe Thunderstorm w/ Hail", "thunder", 3),
}

UNKNOWN = Condition("Unknown", "cloudy")

# Single-cell glyphs with predictable width in terminal fonts.
GLYPHS = {
    "clear": ("☀", "☾"),
    "partly": ("☁", "☁"),
    "cloudy": ("☁", "☁"),
    "fog": ("≡", "≡"),
    "drizzle": ("☂", "☂"),
    "rain": ("☂", "☂"),
    "freezing": ("❄", "❄"),
    "snow": ("❄", "❄"),
    "thunder": ("ϟ", "ϟ"),
}

GLYPH_COLORS = {
    "clear": ("#ffd23f", "#e8e6ff"),
    "partly": ("#f2e3a0", "#c9d1d9"),
    "cloudy": ("#c9d1d9", "#9aa4ae"),
    "fog": ("#9aa4ae", "#9aa4ae"),
    "drizzle": ("#6cb6ff", "#6cb6ff"),
    "rain": ("#3d9df3", "#3d9df3"),
    "freezing": ("#a5f3fc", "#a5f3fc"),
    "snow": ("#ffffff", "#ffffff"),
    "thunder": ("#ffe066", "#ffe066"),
}


def condition(code: int | None) -> Condition:
    if code is None:
        return UNKNOWN
    return CODES.get(int(code), UNKNOWN)


def glyph(code: int | None, is_day: bool = True) -> tuple[str, str]:
    """Return (glyph, color)."""
    scene = condition(code).scene
    i = 0 if is_day else 1
    return GLYPHS[scene][i], GLYPH_COLORS[scene][i]


# Small wttr.in-style icons (5 lines x 13 cols) for forecast cards.
# Each line is a list of (text, color) segments.
_Y, _W, _G, _B, _C, _L = "#ffd23f", "#e6edf3", "#8b949e", "#3d9df3", "#a5f3fc", "#ffe066"

ICONS: dict[str, list[list[tuple[str, str]]]] = {
    "clear": [
        [("    \\   /    ", _Y)],
        [("     .-.     ", _Y)],
        [("  ― (   ) ―  ", _Y)],
        [("     `-’     ", _Y)],
        [("    /   \\    ", _Y)],
    ],
    "partly": [
        [("   \\  /      ", _Y)],
        [(" _ /\"\"", _Y), (".-.    ", _W)],
        [("   \\_", _Y), ("(   ).  ", _W)],
        [("   /", _Y), ("(___(__) ", _W)],
        [("             ", _W)],
    ],
    "cloudy": [
        [("             ", _G)],
        [("     .--.    ", _G)],
        [("  .-(    ).  ", _G)],
        [(" (___.__)__) ", _G)],
        [("             ", _G)],
    ],
    "fog": [
        [("             ", _G)],
        [(" _ - _ - _ - ", _G)],
        [("  _ - _ - _  ", _G)],
        [(" _ - _ - _ - ", _G)],
        [("             ", _G)],
    ],
    "drizzle": [
        [("     .-.     ", _G)],
        [("    (   ).   ", _G)],
        [("   (___(__)  ", _G)],
        [("    ‘ ‘ ‘ ‘  ", _B)],
        [("   ‘ ‘ ‘ ‘   ", _B)],
    ],
    "rain": [
        [("     .-.     ", _G)],
        [("    (   ).   ", _G)],
        [("   (___(__)  ", _G)],
        [("  ‚‘‚‘‚‘‚‘   ", _B)],
        [("  ‚’‚’‚’‚’   ", _B)],
    ],
    "freezing": [
        [("     .-.     ", _G)],
        [("    (   ).   ", _G)],
        [("   (___(__)  ", _G)],
        [("    ‘ ", _B), ("*", _C), (" ‘ ", _B), ("*", _C), ("   ", _B)],
        [("   ", _B), ("*", _C), (" ‘ ", _B), ("*", _C), (" ‘    ", _B)],
    ],
    "snow": [
        [("     .-.     ", _G)],
        [("    (   ).   ", _G)],
        [("   (___(__)  ", _G)],
        [("    *  *  *  ", _W)],
        [("   *  *  *   ", _W)],
    ],
    "thunder": [
        [("     .-.     ", _G)],
        [("    (   ).   ", _G)],
        [("   (___(__)  ", _G)],
        [("  ‚‘", _B), ("ϟ", _L), ("‘‚", _B), ("ϟ", _L), ("‚‘  ", _B)],
        [("  ‚’‚’", _B), ("ϟ", _L), ("’‚’  ", _B)],
    ],
}

NIGHT_CLEAR = [
    [("             ", _W)],
    [("     _..     ", "#e8e6ff")],
    [("   .’  .’  ✦ ", "#e8e6ff")],
    [("  ✦ ‘-.’     ", "#e8e6ff")],
    [("        ✦    ", "#e8e6ff")],
]


def icon(code: int | None, is_day: bool = True) -> list[list[tuple[str, str]]]:
    scene = condition(code).scene
    if scene == "clear" and not is_day:
        return NIGHT_CLEAR
    return ICONS[scene]
