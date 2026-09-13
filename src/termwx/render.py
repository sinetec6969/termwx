"""Rich renderables shared by the TUI panels and the --once snapshot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from . import moon, units as U, wmo
from .models import Alert, AlertsResult, KpPoint, SpaceWeather, SwpcMessage, Weather

DIM = "#8b949e"
LABEL = "#7d8590"
ACCENT = "#58a6ff"

SEVERITY_COLORS = {
    "Extreme": "#ff2d55",
    "Severe": "#ff453a",
    "Moderate": "#ff9f0a",
    "Minor": "#ffd60a",
    "Unknown": "#8b949e",
}

# ── big digits ────────────────────────────────────────────────────────────────
BIG = {
    "0": ["█▀█", "█ █", "▀▀▀"],
    "1": ["▀█ ", " █ ", "▀▀▀"],
    "2": ["▀▀█", "█▀▀", "▀▀▀"],
    "3": ["▀▀█", " ▀█", "▀▀▀"],
    "4": ["█ █", "▀▀█", "  ▀"],
    "5": ["█▀▀", "▀▀█", "▀▀▀"],
    "6": ["█▀▀", "█▀█", "▀▀▀"],
    "7": ["▀▀█", "  █", "  ▀"],
    "8": ["█▀█", "█▀█", "▀▀▀"],
    "9": ["█▀█", "▀▀█", "▀▀▀"],
    "-": ["   ", "▀▀▀", "   "],
}


def big_number(value: float, color: str) -> list[Text]:
    s = str(round(value))
    rows = [Text() for _ in range(3)]
    for i, ch in enumerate(s):
        glyph = BIG.get(ch, BIG["-"])
        for r in range(3):
            rows[r].append(("" if i == 0 else " ") + glyph[r], style=f"bold {color}")
    return rows


# ── helpers ──────────────────────────────────────────────────────────────────
def hhmm(dt: datetime | None, short: bool = False) -> str:
    if dt is None:
        return "--"
    h = dt.hour % 12 or 12
    ap = "a" if dt.hour < 12 else "p"
    if short:
        return f"{h}{ap}"
    return f"{h}:{dt.minute:02d}{ap}"


def local_now(tz: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz)) if tz else datetime.now().astimezone()
    except (KeyError, ValueError):
        return datetime.now().astimezone()


def ago(dt: datetime) -> str:
    secs = (datetime.now() - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    return f"{int(secs // 3600)}h ago"


def kv(label: str, value: Text | str, style: str = "") -> Text:
    t = Text()
    t.append(f"{label} ", style=LABEL)
    if isinstance(value, Text):
        t.append_text(value)
    else:
        t.append(value, style=style)
    return t


BLOCKS = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[float], lo: float | None = None, hi: float | None = None) -> str:
    if not values:
        return ""
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    span = (hi - lo) or 1
    return "".join(BLOCKS[max(1, min(8, round((v - lo) / span * 7) + 1))] for v in values)


# ── current conditions ───────────────────────────────────────────────────────
def current_panel(w: Weather, width: int = 60) -> RenderableType:
    c, u = w.current, w.units
    cond = wmo.condition(c.code)
    g, gcolor = wmo.glyph(c.code, c.is_day)
    tcolor = U.temp_color(c.temp, u)

    head = Text()
    head.append(f"{g} ", style=f"bold {gcolor}")
    head.append(cond.label, style="bold #e6edf3")

    digits = big_number(c.temp, tcolor)
    side = [Text(), Text(), Text()]
    side[0].append("°" + ("F" if u == U.IMPERIAL else "C"), style=f"bold {tcolor}")
    side[0].append("   feels ", style=LABEL)
    side[0].append(U.temp(c.feels_like, u), style=f"bold {U.temp_color(c.feels_like, u)}")
    if w.today:
        side[1].append("    ↑ ", style=LABEL)
        side[1].append(U.temp(w.today.temp_max, u), style=U.temp_color(w.today.temp_max, u))
        side[1].append("  ↓ ", style=LABEL)
        side[1].append(U.temp(w.today.temp_min, u), style=U.temp_color(w.today.temp_min, u))
    if w.today and w.today.precip_prob:
        side[2].append(f"    ☂ {w.today.precip_prob:.0f}% today", style="#6cb6ff")
    big = Table.grid(padding=(0, 1))
    big.add_column()
    big.add_column()
    for r in range(3):
        big.add_row(digits[r], side[r])

    wind = Text()
    wind.append(f"{U.wind_arrow(c.wind_dir)} {U.compass(c.wind_dir)} {U.speed(c.wind_speed, u)}", style="#e6edf3")
    if c.wind_gust > c.wind_speed + 3:
        wind.append(f" g{round(c.wind_gust)}", style="#ff9f43" if c.wind_gust >= (40 if u == U.IMPERIAL else 64) else DIM)

    trend = w.pressure_trend
    arrow, tstyle = ("→", DIM)
    if trend > 0.7:
        arrow, tstyle = ("↗", "#9be15d")
    elif trend < -0.7:
        arrow, tstyle = ("↘", "#ff9f43")
    press = Text(U.pressure(c.pressure, u), style="#e6edf3")
    press.append(f" {arrow}", style=tstyle)

    uv_txt, uv_style = U.uv_label(c.uv)
    aqi_txt, aqi_style = U.aqi_label(w.aqi)

    cells = [
        kv("Wind  ", wind),
        kv("Humid ", f"{c.humidity:.0f}%  dew {U.temp(c.dew_point, u)}", "#e6edf3"),
        kv("Press ", press),
        kv("UV    ", uv_txt, uv_style),
        kv("Vis   ", U.visibility(c.visibility, u), "#e6edf3"),
        kv("AQI   ", aqi_txt, aqi_style),
        kv("Cloud ", f"{c.cloud_cover:.0f}%", "#e6edf3"),
        kv("Precip", U.precip(c.precip, u) if c.precip else "none", "#6cb6ff" if c.precip else "#e6edf3"),
    ]
    grid = Table.grid(padding=(0, 2))
    if width >= 42:
        grid.add_column()
        grid.add_column()
        for i in range(0, len(cells), 2):
            grid.add_row(cells[i], cells[i + 1])
    else:
        grid.add_column()
        for cell in cells:
            grid.add_row(cell)

    sun = Text()
    if w.today:
        sun.append("☀ ", style="#ffd23f")
        sun.append(f"↑{hhmm(w.today.sunrise)} ↓{hhmm(w.today.sunset)}", style="#e6edf3")
        if w.today.sunrise and w.today.sunset and width >= 52:
            mins = int((w.today.sunset - w.today.sunrise).total_seconds() // 60)
            sun.append(f"  {mins // 60}h{mins % 60:02d}m", style=DIM)
    name, emoji, illum = moon.phase()
    sun.append(f"   {emoji} {name} {illum * 100:.0f}%", style="#e8e6ff")

    return Group(head, big, Text(), grid, sun)


# ── hourly graph ─────────────────────────────────────────────────────────────
def hourly_panel(w: Weather, width: int = 80) -> RenderableType:
    u = w.units
    avail = max(20, width)
    cw = 3 if avail >= 72 else 2
    n = min(len(w.hours), avail // cw, 36)
    hours = w.hours[:n]
    if not hours:
        return Text("no hourly data", style=DIM)
    temps = [h.temp for h in hours]
    lo, hi = min(temps), max(temps)
    span = (hi - lo) or 1
    rows_h = 3
    levels = rows_h * 8

    glyph_row, label_row, time_row, rain_row = Text(), Text(), Text(), Text()
    graph = [Text() for _ in range(rows_h)]
    step = 3 if cw == 3 else 4
    for i, h in enumerate(hours):
        color = U.temp_color(h.temp, u)
        lvl = max(1, round((h.temp - lo) / span * (levels - 4)) + 3)
        for r in range(rows_h):
            base = (rows_h - 1 - r) * 8
            fill = max(0, min(8, lvl - base))
            graph[r].append(BLOCKS[fill] * cw if fill else " " * cw, style=color)
        marker = i % step == 0
        is_day = h.time.hour in range(6, 20)
        if w.today and w.today.sunrise and w.today.sunset:
            day = next((d for d in w.days if d.date == h.time.date()), None)
            if day and day.sunrise and day.sunset:
                is_day = day.sunrise <= h.time < day.sunset
        if marker:
            g, gc = wmo.glyph(h.code, is_day)
            glyph_row.append(g.ljust(cw * step)[: cw * step], style=gc)
            label_row.append(U.temp(h.temp, u).ljust(cw * step)[: cw * step], style=f"bold {color}")
            tlabel = "now" if i == 0 else (hhmm(h.time, short=True) if h.time.hour else h.time.strftime("%a"))
            time_row.append(tlabel.ljust(cw * step)[: cw * step], style=LABEL if h.time.hour else f"bold {ACCENT}")
        p = h.precip_prob
        if p >= 60:
            rc = "#3d9df3"
        elif p >= 30:
            rc = "#6cb6ff"
        else:
            rc = "#30363d"
        rain_row.append(BLOCKS[max(1, round(p / 100 * 8))] * cw if p > 0 else "·".ljust(cw), style=rc)

    peak = max(hours, key=lambda h: h.precip_prob)
    legend = Text()
    legend.append("☂ precip chance  ", style="#6cb6ff")
    if peak.precip_prob >= 20:
        legend.append(f"peaks {peak.precip_prob:.0f}% at {hhmm(peak.time, short=True)}", style="#e6edf3")
    else:
        legend.append("dry next " + str(n) + "h", style=DIM)
    return Group(glyph_row, label_row, *graph, rain_row, time_row, legend)


# ── 3-day forecast ───────────────────────────────────────────────────────────
def forecast_panel(w: Weather, width: int = 80, days: int = 3) -> RenderableType:
    u = w.units
    upcoming = w.days[1 : 1 + days]
    if not upcoming:
        return Text("no forecast data", style=DIM)
    if width < 60:
        t = Table.grid(padding=(0, 1))
        for _ in range(5):
            t.add_column()
        for d in upcoming:
            g, gc = wmo.glyph(d.code)
            t.add_row(
                Text(d.date.strftime("%a"), style=f"bold {ACCENT}"),
                Text(g, style=gc),
                Text.assemble(
                    (U.temp(d.temp_max, u), U.temp_color(d.temp_max, u)), ("/", DIM), (U.temp(d.temp_min, u), U.temp_color(d.temp_min, u))
                ),
                Text(f"☂{d.precip_prob:.0f}%", style="#6cb6ff" if d.precip_prob >= 30 else DIM),
                Text(wmo.condition(d.code).label, style="#c9d1d9"),
            )
        return t

    grid = Table.grid(padding=(0, 2), expand=True)
    for _ in upcoming:
        grid.add_column(ratio=1)
    cards = []
    for d in upcoming:
        lines: list[Text] = []
        title = Text(justify="left")
        title.append(d.date.strftime("%A"), style=f"bold {ACCENT}")
        title.append(d.date.strftime("  %b %-d"), style=DIM)
        lines.append(title)
        for row in wmo.icon(d.code):
            lines.append(Text.assemble(*row))
        lines.append(Text(wmo.condition(d.code).label, style="bold #e6edf3"))
        temps = Text()
        temps.append("↑" + U.temp(d.temp_max, u), style=f"bold {U.temp_color(d.temp_max, u)}")
        temps.append("  ↓" + U.temp(d.temp_min, u), style=f"bold {U.temp_color(d.temp_min, u)}")
        lines.append(temps)
        rain = Text()
        rain.append(f"☂ {d.precip_prob:.0f}%", style="#6cb6ff" if d.precip_prob >= 30 else DIM)
        if d.precip_sum:
            rain.append(f"  {U.precip(d.precip_sum, u)}", style="#6cb6ff")
        lines.append(rain)
        uv_txt, uv_style = U.uv_label(d.uv_max)
        misc = Text()
        misc.append(f"≋ {U.speed(d.wind_max, u)}", style="#c9d1d9")
        misc.append("  UV ", style=LABEL)
        misc.append(uv_txt.split()[0], style=uv_style)
        lines.append(misc)
        cards.append(Group(*lines))
    grid.add_row(*cards)
    return grid


# ── alerts ───────────────────────────────────────────────────────────────────
def alert_color(a: Alert) -> str:
    return SEVERITY_COLORS.get(a.severity, SEVERITY_COLORS["Unknown"])


def when_text(a: Alert) -> str:
    parts = []
    if a.onset:
        parts.append(f"from {a.onset.strftime('%a')} {hhmm(a.onset)}")
    end = a.ends or a.expires
    if end:
        parts.append(f"until {end.strftime('%a')} {hhmm(end)}")
    return " ".join(parts)


def banner_text(alerts: list[Alert]) -> Text:
    top = alerts[0]
    t = Text()
    t.append(" ⚠  ", style="bold")
    t.append(top.event.upper(), style="bold")
    w = when_text(top)
    if w:
        t.append(f"  {w}")
    if len(alerts) > 1:
        t.append(f"   +{len(alerts) - 1} more")
    t.append("   [w] details ", style="italic")
    return t


def alerts_summary(res: AlertsResult | None) -> RenderableType:
    if res is None:
        return Text("checking alerts…", style=DIM)
    if not res.supported:
        return Text("NWS alerts cover US locations only", style=DIM)
    if res.error and not res.alerts:
        return Text(f"alerts unavailable: {res.error}", style="#ff9f43")
    if not res.alerts:
        return Text("✓ No active NWS watches, warnings or advisories", style="#9be15d")
    lines = []
    for a in res.alerts:
        t = Text()
        t.append("■ ", style=alert_color(a))
        t.append(a.event, style=f"bold {alert_color(a)}")
        t.append(f"  {when_text(a)}", style=DIM)
        lines.append(t)
    return Group(*lines)


# ── space weather ────────────────────────────────────────────────────────────
SCALE_COLORS = ["#2ea043", "#d29922", "#f0883e", "#f85149", "#da3633", "#bc4bf5"]
SCALE_NAMES = {
    "G": ["none", "Minor", "Moderate", "Strong", "Severe", "Extreme"],
    "S": ["none", "Minor", "Moderate", "Strong", "Severe", "Extreme"],
    "R": ["none", "Minor", "Moderate", "Strong", "Severe", "Extreme"],
}


def badge(letter: str, level: int) -> Text:
    level = max(0, min(5, level))
    fg = "#0d1117" if level else "#e6edf3"
    return Text(f" {letter}{level} ", style=f"bold {fg} on {SCALE_COLORS[level]}")


def kp_color(kp: float) -> str:
    if kp < 4:
        return "#2ea043"
    if kp < 5:
        return "#d29922"
    if kp < 6:
        return "#f0883e"
    if kp < 7:
        return "#f85149"
    if kp < 8:
        return "#da3633"
    return "#bc4bf5"


def kp_to_g(kp: float) -> int:
    return max(0, min(5, int(kp + 0.34) - 4))


def kp_chart(points: list[KpPoint], rows: int = 3, col_width: int = 1, label_days: bool = True) -> list[Text]:
    """Vertical bar chart, Kp 0-9. Predicted bars are drawn with a lighter shade."""
    lines = [Text() for _ in range(rows)]
    per_row = 9 / rows
    now = datetime.now(timezone.utc)
    labels = [" "] * (len(points) * col_width)
    prev_day = None
    for i, p in enumerate(points):
        local = p.time.astimezone()
        if prev_day is not None and local.date() != prev_day:
            for j, ch in enumerate(local.strftime("%a")):
                if i * col_width + j < len(labels):
                    labels[i * col_width + j] = ch
        prev_day = local.date()
    for p in points:
        color = kp_color(p.kp)
        future = p.kind == "predicted"
        for r in range(rows):
            base = (rows - 1 - r) * per_row
            frac = (p.kp - base) / per_row
            if frac >= 1:
                ch = "▓" if future else "█"
            elif frac > 0:
                ch = BLOCKS[max(1, round(frac * 8))]
            else:
                ch = " "
            is_now = p.time <= now < p.time + timedelta(hours=3)
            style = f"{color} on #1f2a37" if is_now else color
            lines[r].append(ch * col_width, style=style)
    if label_days:
        lines.append(Text("".join(labels), style=LABEL))
    return lines


def flare_color(cls: str) -> str:
    return {"X": "#f85149", "M": "#f0883e", "C": "#d29922"}.get(cls[:1], "#2ea043")


def space_panel(sw: SpaceWeather | None, width: int = 40) -> RenderableType:
    if sw is None:
        return Text("fetching space weather…", style=DIM)
    out: list[RenderableType] = []

    scales = Text()
    scales.append("NOAA  ", style=LABEL)
    scales.append_text(badge("G", sw.g_now))
    scales.append(" ")
    scales.append_text(badge("S", sw.s_now))
    scales.append(" ")
    scales.append_text(badge("R", sw.r_now))
    worst = max(sw.g_now, sw.s_now, sw.r_now)
    scales.append(f"  {'quiet' if worst == 0 else SCALE_NAMES['G'][worst]}", style=SCALE_COLORS[worst] if worst else DIM)
    out.append(scales)

    kp_now = sw.kp_now
    if kp_now:
        t = Text()
        t.append("Kp    ", style=LABEL)
        t.append(f"{kp_now.kp:.1f}", style=f"bold {kp_color(kp_now.kp)}")
        g = kp_to_g(kp_now.kp)
        t.append(f" {'G' + str(g) if g else 'quiet'}", style=kp_color(kp_now.kp))
        mx = sw.kp_max_forecast
        if mx:
            t.append("  peak ", style=LABEL)
            t.append(f"{mx.kp:.1f}", style=f"bold {kp_color(mx.kp)}")
            loc = mx.time.astimezone()
            t.append(f" {loc.strftime('%a')} {hhmm(loc, short=True)}", style=DIM)
        out.append(t)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        pts = [p for p in sw.kp if p.time >= cutoff]
        cols = max(8, width)
        pts = sw.kp[-cols:]
        out.extend(kp_chart(pts, rows=3, col_width=1))

    wind = Text()
    wind.append("Wind  ", style=LABEL)
    if sw.wind_speed is not None:
        wc = "#f85149" if sw.wind_speed >= 700 else "#f0883e" if sw.wind_speed >= 500 else "#e6edf3"
        wind.append(f"{sw.wind_speed:.0f} km/s", style=f"bold {wc}")
        if sw.wind_density is not None:
            wind.append(f"  {sw.wind_density:.1f} p/cm³", style="#c9d1d9")
        if sw.speed_history:
            wind.append("  " + sparkline(sw.speed_history[-60::6]), style=wc)
    else:
        wind.append("--", style=DIM)
    out.append(wind)

    imf = Text()
    imf.append("IMF   ", style=LABEL)
    if sw.bz is not None:
        bzc = "#f85149" if sw.bz <= -10 else "#f0883e" if sw.bz <= -5 else "#9be15d" if sw.bz > 0 else "#e6edf3"
        imf.append(f"Bz {sw.bz:+.1f}", style=f"bold {bzc}")
        if sw.bt is not None:
            imf.append(f"  Bt {sw.bt:.1f} nT", style="#c9d1d9")
        if sw.bz_history:
            hist = sw.bz_history[-60::6]
            m = max(5.0, max(abs(v) for v in hist))
            imf.append("  " + sparkline(hist, -m, m), style=bzc)
    else:
        imf.append("--", style=DIM)
    out.append(imf)

    xr = Text()
    xr.append("X-ray ", style=LABEL)
    if sw.flare_current:
        xr.append(sw.flare_current, style=f"bold {flare_color(sw.flare_current)}")
        if sw.flare_max:
            xr.append("  max ", style=LABEL)
            xr.append(sw.flare_max, style=flare_color(sw.flare_max))
            if sw.flare_max_time:
                xr.append(f" {hhmm(sw.flare_max_time.astimezone())}", style=DIM)
    else:
        xr.append("--", style=DIM)
    if sw.f107 is not None:
        xr.append("  F10.7 ", style=LABEL)
        xr.append(f"{sw.f107:.0f}", style="#c9d1d9")
    out.append(xr)

    au = Text()
    au.append("Aurora", style=LABEL)
    if sw.aurora_overhead is not None:
        ac = "#3fb950" if (sw.aurora_nearby or 0) >= 10 else "#c9d1d9"
        au.append(f" {sw.aurora_overhead}% overhead", style=f"bold {ac}" if sw.aurora_overhead >= 10 else ac)
        au.append(f" · {sw.aurora_nearby}% horizon", style=ac)
    else:
        au.append(" --", style=DIM)
    out.append(au)

    if sw.outlook:
        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style=LABEL)
        for _ in sw.outlook:
            tbl.add_column(justify="right")
        tbl.add_row("      ", *[Text(o.date.strftime("%a"), style=ACCENT) for o in sw.outlook])
        tbl.add_row("G max ", *[Text(f"G{o.g or 0}", style=SCALE_COLORS[o.g or 0] if o.g else DIM) for o in sw.outlook])
        tbl.add_row("R1-2  ", *[_pct(o.r_minor_prob, 25) for o in sw.outlook])
        tbl.add_row("R3+   ", *[_pct(o.r_major_prob, 10) for o in sw.outlook])
        tbl.add_row("S1+   ", *[_pct(o.s_prob, 10) for o in sw.outlook])
        out.append(tbl)

    if sw.messages:
        m = sw.messages[0]
        t = Text(overflow="ellipsis", no_wrap=True)
        t.append(f"{m.issued.astimezone().strftime('%a')} {hhmm(m.issued.astimezone())} ", style=DIM)
        t.append(m.title, style=message_color(m))
        out.append(t)
    if sw.stale:
        out.append(Text("⚠ offline – cached", style="#ff9f43"))
    return Group(*out)


def message_color(m: SwpcMessage) -> str:
    if m.level:
        return SCALE_COLORS[m.level]
    if m.title.lower().startswith("warning"):
        return "#d29922"
    return "#c9d1d9"


def _pct(v: int | None, warn: int) -> Text:
    if v is None:
        return Text("--", style=DIM)
    return Text(f"{v}%", style="#f0883e" if v >= warn else "#c9d1d9")
