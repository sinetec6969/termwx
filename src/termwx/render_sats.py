"""Rich renderables for the satellite tracker: the dashboard panel, the tracker
screen's list rows and detail pane, and an ASCII polar sky plot of a pass."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from rich.console import Group, RenderableType
from rich.text import Text

from . import orbit, units as U
from .render import ACCENT, DIM, LABEL, hhmm
from .render_aviation import BAD, FG, GOOD, WARN
from .render_drone import _local
from .sat_models import Look, Pass, Satellite, SatState

UP_COLOR = "#3fb950"
PANEL_PASSES = 6
POLAR_R = 8  # radius in rows; columns are doubled for the 2:1 cell aspect


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── formatting ────────────────────────────────────────────────────────────────
def mhz(hz: float | None) -> str:
    return "—" if hz is None else f"{hz / 1e6:.3f}"


def duration(td: timedelta) -> str:
    mins = max(0, int(td.total_seconds() // 60))
    if mins < 60:
        return f"{mins}m"
    if mins < 48 * 60:
        return f"{mins // 60}h{mins % 60:02d}m"
    return f"{mins // 1440}d"


def until(dt: datetime, now: datetime) -> str:
    secs = (dt - now).total_seconds()
    return "now" if secs < 60 else f"in {duration(dt - now)}"


def el_color(el: float) -> str:
    return GOOD if el >= 45 else UP_COLOR if el >= 25 else WARN if el >= 10 else DIM


def tags(sat: Satellite) -> str:
    return "/".join(sat.kinds)


def current_pass(passes: list[Pass], now: datetime) -> Pass | None:
    return next((p for p in passes if p.active(now)), None)


def next_pass(passes: list[Pass], now: datetime) -> Pass | None:
    return next((p for p in passes if p.los > now), None)


def observer(st: SatState) -> orbit.Observer:
    return orbit.Observer(st.lat, st.lon)


def status_line(st: SatState, now: datetime) -> Text:
    """'TLEs 1h12m old · next update 6:40p' or the update error."""
    cat = st.catalog
    t = Text(no_wrap=True, overflow="ellipsis")
    if st.updating:
        t.append("updating TLEs…", style=ACCENT)
        return t
    if cat.fetched_at is None:
        t.append(f"⚠ {cat.error}" if cat.error else "no TLEs yet", style=WARN if cat.error else DIM)
        return t
    t.append(f"TLEs {duration(now - cat.fetched_at)} old", style=WARN if cat.stale else DIM)
    nxt = st.next_update()
    if nxt:
        t.append(f" · next {hhmm(_local(max(nxt, now), st.tz))}", style=DIM)
    if cat.stale:
        t.append(f" · ⚠ update failed: {cat.error}" if cat.error else " · ⚠ offline", style=WARN)
    return t


# ── dashboard panel ───────────────────────────────────────────────────────────
def sats_panel(st: SatState, width: int) -> RenderableType:
    now = utcnow()
    cat = st.catalog
    if not cat.satellites:
        return Group(Text("waiting for satellite data…", style=DIM), status_line(st, now))
    obs = observer(st)
    up: list[Text] = []
    upcoming: list[tuple[datetime, Satellite, Pass]] = []
    for sat in cat.satellites:
        for p in st.passes.get(sat.norad, []):
            if p.los <= now:
                continue
            if p.aos <= now:
                lk = orbit.look(sat, obs, now, subpoint=False)
                if lk and lk.el >= 0:
                    up.append(_up_line(sat, lk, width))
            else:
                upcoming.append((p.aos, sat, p))
    upcoming.sort(key=lambda r: r[0])
    rows: list[RenderableType] = list(up)
    for _, sat, p in upcoming[: max(0, PANEL_PASSES - len(up))]:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(f"  {sat.short_name[:10]:<10} ", style=f"bold {FG}")
        t.append(f"{tags(sat):<8}", style=LABEL)
        t.append(f"{hhmm(_local(p.aos, st.tz)):>6} ", style=FG)
        t.append(f"{until(p.aos, now):<8}", style=DIM)
        t.append(f"{p.max_el:3.0f}°", style=el_color(p.max_el))
        rows.append(t)
    if not rows:
        rows.append(Text(f"no passes ≥{st.min_el:.0f}° in the next 24 h", style=DIM))
    rows.append(status_line(st, now))
    return Group(*rows)


def _up_line(sat: Satellite, lk: Look, width: int) -> Text:
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append("▲ ", style=f"bold {UP_COLOR}")
    t.append(f"{sat.short_name[:10]:<10} ", style=f"bold {UP_COLOR}")
    t.append(f"{tags(sat):<8}", style=LABEL)
    t.append(f"el {lk.el:2.0f}° az {lk.az:3.0f}°", style=el_color(lk.el))
    dl = next((tx.downlink for tx in sat.transmitters if tx.downlink), None)
    if dl and width >= 48:
        t.append(f" ↓{mhz(orbit.doppler_downlink(dl, lk.range_rate))}", style=FG)
    return t


# ── tracker screen ────────────────────────────────────────────────────────────
def list_line(sat: Satellite, st: SatState, now: datetime, lk: Look | None) -> Text:
    passes = st.passes.get(sat.norad, [])
    t = Text(no_wrap=True, overflow="ellipsis")
    if lk and lk.el >= 0:
        t.append("▲ ", style=f"bold {UP_COLOR}")
        t.append(f"{sat.short_name[:14]:<14} ", style=f"bold {UP_COLOR}")
        t.append(f"{tags(sat):<8}", style=LABEL)
        t.append(f"el {lk.el:4.1f}°  az {lk.az:5.1f}° {U.compass(lk.az):<3}", style=el_color(lk.el))
        return t
    t.append(f"  {sat.short_name[:14]:<14} ", style=f"bold {FG}")
    t.append(f"{tags(sat):<8}", style=LABEL)
    p = next_pass(passes, now)
    if p is None:
        t.append(f"no pass ≥{st.min_el:.0f}° in 24 h", style=DIM)
        return t
    t.append(f"{hhmm(_local(p.aos, st.tz)):>6} ", style=FG)
    t.append(f"{until(p.aos, now):<9}", style=DIM)
    t.append(f"max {p.max_el:2.0f}°", style=el_color(p.max_el))
    return t


def sort_key(sat: Satellite, st: SatState, now: datetime, lk: Look | None) -> tuple:
    """Overhead first (highest first), then by next AOS, then no-pass satellites by name."""
    if lk and lk.el >= 0:
        return (0, -lk.el, sat.short_name)
    p = next_pass(st.passes.get(sat.norad, []), now)
    if p:
        return (1, p.aos.timestamp(), sat.short_name)
    return (2, 0, sat.short_name.upper())


def detail(sat: Satellite, st: SatState, now: datetime, lk: Look | None, pass_index: int) -> Group:
    passes = [p for p in st.passes.get(sat.norad, []) if p.los > now]
    parts: list[RenderableType] = []
    head = Text()
    head.append(sat.short_name, style=f"bold {ACCENT}")
    if sat.name and sat.name != sat.short_name:
        head.append(f"  {sat.name}", style=DIM)
    head.append(f"  NORAD {sat.norad}", style=LABEL)
    parts.append(head)
    epoch_age = now - sat.epoch
    parts.append(Text(f"TLE epoch {_local(sat.epoch, st.tz):%b %-d %H:%M} ({duration(epoch_age)} old)",
                      style=WARN if epoch_age > timedelta(days=7) else DIM))
    parts.append(Text())

    # live position
    if lk is None:
        parts.append(Text("SGP4 can't propagate this TLE (decayed or bad elements).", style=BAD))
    else:
        up = lk.el >= 0
        t = Text()
        t.append("▲ IN VIEW  " if up else "below horizon  ", style=f"bold {UP_COLOR}" if up else DIM)
        t.append(f"az {lk.az:5.1f}° {U.compass(lk.az):<3}  el {lk.el:5.1f}°", style=el_color(lk.el) if up else FG)
        parts.append(t)
        rr = Text(style=FG)
        rr.append(f"range {lk.range_km:,.0f} km  ")
        rr.append(f"{'receding' if lk.range_rate > 0 else 'approaching'} {abs(lk.range_rate):.2f} km/s", style=DIM)
        parts.append(rr)
        ns, ew = "N" if lk.lat >= 0 else "S", "E" if lk.lon >= 0 else "W"
        parts.append(Text(f"over {abs(lk.lat):.1f}°{ns} {abs(lk.lon):.1f}°{ew}  alt {lk.alt_km:,.0f} km", style=DIM))
    parts.append(Text())

    # frequencies with Doppler (live when up, at the next AOS otherwise)
    nxt = next_pass(passes, now)
    rate, when = None, ""
    if lk and lk.el >= 0:
        rate, when = lk.range_rate, "now"
    elif nxt:
        at = orbit.look(sat, observer(st), nxt.aos, subpoint=False)
        rate, when = (at.range_rate if at else None), f"at AOS {hhmm(_local(nxt.aos, st.tz))}"
    title = Text("Frequencies", style=f"bold {ACCENT}")
    if when:
        title.append(f"  Doppler {when}", style=DIM)
    parts.append(title)
    for tx in sat.transmitters:
        parts.append(Text.assemble((f"{tx.kind:<5}", f"bold {LABEL}"), (tx.description, FG)))
        up_line = Text("  ↑ up   ", style=DIM)
        up_line.append(mhz(tx.uplink), style=FG)
        if rate is not None and tx.uplink:
            up_line.append(f"  tx {mhz(orbit.doppler_uplink(tx.uplink, rate))}", style=WARN)
        if tx.ctcss:
            up_line.append(f"  CTCSS {tx.ctcss:g} Hz", style=ACCENT)
        dn_line = Text("  ↓ down ", style=DIM)
        dn_line.append(mhz(tx.downlink), style=FG)
        if rate is not None and tx.downlink:
            dn_line.append(f"  rx {mhz(orbit.doppler_downlink(tx.downlink, rate))}", style=GOOD)
        parts += [up_line, dn_line]
    parts.append(Text())

    # pass list
    parts.append(Text.assemble(("Passes", f"bold {ACCENT}"), (f"  next 24 h, max el ≥ {st.min_el:.0f}°", DIM)))
    if not passes:
        parts.append(Text("  none", style=DIM))
    else:
        parts.append(Text("  day  AOS     LOS      dur  max  path", style=LABEL))
        sel = min(pass_index, len(passes) - 1)
        for i, p in enumerate(passes):
            a, z = _local(p.aos, st.tz), _local(p.los, st.tz)
            t = Text(no_wrap=True)
            t.append("▸ " if i == sel else "  ", style=ACCENT)
            t.append(f"{a:%a}  {hhmm(a):<7} {hhmm(z):<7} {duration(p.duration):>4} ", style=FG if i == sel else DIM)
            t.append(f"{p.max_el:3.0f}°", style=el_color(p.max_el))
            t.append(f"  {U.compass(p.aos_az)}→{U.compass(p.tca_az)}→{U.compass(p.los_az)}",
                     style=FG if i == sel else DIM)
            if p.active(now):
                t.append("  now", style=f"bold {UP_COLOR}")
            parts.append(t)
        parts.append(Text())
        p = passes[sel]
        a = _local(p.aos, st.tz)
        parts.append(Text.assemble(("Sky plot", f"bold {ACCENT}"),
                                   (f"  {a:%a} {hhmm(a)} pass · [ ] other passes", DIM)))
        parts.append(polar(p, lk if p.active(now) else None))
    return Group(*parts)


def polar(p: Pass, now_look: Look | None = None, r: int = POLAR_R) -> Text:
    """Sky plot centred on the zenith: rings at 0°/30°/60°, north up, east right (as gpredict draws it)."""
    h, w = 2 * r + 3, 4 * r + 7
    cy, cx = r + 1, 2 * r + 3
    grid: list[list[tuple[str, str]]] = [[(" ", "")] * w for _ in range(h)]

    def cell(az: float, el: float) -> tuple[int, int]:
        rad = (90 - max(0.0, min(90.0, el))) / 90 * r
        a = math.radians(az)
        return round(cy - rad * math.cos(a)), round(cx + 2 * rad * math.sin(a))

    def put(row: int, col: int, ch: str, style: str) -> None:
        if 0 <= row < h and 0 <= col < w:
            grid[row][col] = (ch, style)

    for ring in (0, 30, 60):
        for deg in range(0, 360, 4):
            put(*cell(deg, ring), "·", "#30363d")
    put(cy, cx, "+", "#30363d")
    for label, (row, col) in {"N": (0, cx), "S": (h - 1, cx), "W": (cy, 0), "E": (cy, w - 1)}.items():
        put(row, col, label, LABEL)
    for (az1, el1), (az2, el2) in zip(p.track, p.track[1:]):
        for k in range(4):  # interpolate so the track is unbroken
            f = k / 4
            daz = (az2 - az1 + 180) % 360 - 180
            put(*cell(az1 + daz * f, el1 + (el2 - el1) * f), "•", ACCENT)
    if p.track:
        put(*cell(*p.track[0]), "A", f"bold {GOOD}")
        put(*cell(*p.track[-1]), "L", f"bold {BAD}")
    put(*cell(p.tca_az, p.max_el), "✱", f"bold {WARN}")
    if now_look and now_look.el >= 0:
        put(*cell(now_look.az, now_look.el), "◉", f"bold {UP_COLOR}")
    out = Text(no_wrap=True)
    for i, row in enumerate(grid):
        for ch, style in row:
            out.append(ch, style=style)
        if i < h - 1:
            out.append("\n")
    legend = Text("\nA AOS  ✱ max  L LOS", style=DIM)
    if now_look and now_look.el >= 0:
        legend.append("  ◉ now", style=UP_COLOR)
    out.append_text(legend)
    return out


def screen_header(st: SatState, place: str, now: datetime) -> Text:
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(" SATELLITES ", style="bold #0d1117 on #58a6ff")
    t.append(f"  {place}  ", style=f"bold {FG}")
    t.append_text(status_line(st, now))
    t.append(f" · every {st.refresh_hours:g}h · min el {st.min_el:.0f}° · {len(st.catalog.satellites)} FM/APRS sats",
             style=DIM)
    return t
