"""Rich renderables for aviation data: the Aviation screen, the dashboard
summary panel and the --once snapshot.

Aviation values use aviation units (kt, SM, ft, °C) regardless of the
weather units setting; only the distance to the station follows it.
Every product carries its age, and anything from the offline cache says so.
"""

from __future__ import annotations

from datetime import datetime

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from . import aviation as A, units as U
from .aviation_models import (
    AviationHazard,
    AviationResult,
    CloudLayer,
    Feed,
    MetarReport,
    Pirep,
    TafForecast,
    TafPeriod,
    WindsAloft,
)
from .render import ACCENT, DIM, LABEL

FG = "#e6edf3"
WARN = "#ff9f43"
BAD = "#f85149"
GOOD = "#9be15d"

CATEGORY_COLORS = {"VFR": "#3fb950", "MVFR": "#58a6ff", "IFR": "#f85149", "LIFR": "#d670f0"}
HAZARD_COLORS = {
    "TS": "#f85149", "TURB": "#f0883e", "ICE": "#6cb6ff", "IFR": "#d670f0", "MTN OBSC": "#c9a26b",
    "LLWS": "#f0883e", "SFC WIND": "#e3b341", "VA": "#bc4bf5", "TC": "#ff2d55",
}
METAR_OLD_MIN = 75  # routine METARs are hourly; older than this is worth flagging
DISCLAIMER = "Weather information only — not a substitute for an official preflight briefing."


# ── formatting ───────────────────────────────────────────────────────────────
def cat_color(cat: str) -> str:
    return CATEGORY_COLORS.get(cat, DIM)


def cat_badge(cat: str) -> Text:
    if not cat:
        return Text(" ---- ", style=f"bold {DIM} on #21262d")
    return Text(f" {cat} ", style=f"bold #0d1117 on {cat_color(cat)}")


def fmt_age(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "--"
    mins = int((now - dt).total_seconds() // 60)
    if mins < 0:
        return "now"
    if mins < 60:
        return f"{mins}m"
    if mins < 48 * 60:
        return f"{mins // 60}h{mins % 60:02d}m"
    return f"{mins // 1440}d"


def fmt_until(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return ""
    mins = int((dt - now).total_seconds() // 60)
    if mins <= 0:
        return "expired"
    return f"{mins}m" if mins < 60 else f"{mins // 60}h{mins % 60:02d}m" if mins < 600 else f"{mins // 60}h"


def zulu(dt: datetime | None, minutes: bool = False) -> str:
    if dt is None:
        return "--"
    return dt.strftime("%H%MZ") if minutes else dt.strftime("%HZ")


def fmt_wind(direction: int | None, variable: bool, speed: int | None, gust: int | None) -> str:
    """Readable: '220° 8G14kt', 'VRB 3kt', 'calm'."""
    if speed is None:
        return "--"
    if speed == 0 and not gust:
        return "calm"
    d = "VRB" if variable or direction is None else f"{direction:03d}°"
    g = f"G{gust}" if gust else ""
    return f"{d} {speed}{g}kt"


def wind_code(direction: int | None, variable: bool, speed: int | None, gust: int | None) -> str:
    """Coded: '22008G14KT', 'VRB03KT', '00000KT'."""
    if speed is None:
        return "-----"
    if speed == 0 and not gust:
        return "00000KT"
    d = "VRB" if variable or direction is None else f"{direction:03d}"
    return f"{d}{speed:02d}{f'G{gust:02d}' if gust else ''}KT"


_FRACTIONS = {0.125: "1/8", 0.25: "1/4", 0.375: "3/8", 0.5: "1/2", 0.625: "5/8", 0.75: "3/4"}


def fmt_vis(vis: float | None, plus: bool = False) -> str:
    if vis is None:
        return "--"
    if plus:
        return "10SM" if vis >= 10 else f"P{vis:g}SM"
    whole, frac = int(vis), round(vis - int(vis), 3)
    if frac in _FRACTIONS:
        return f"{whole} {_FRACTIONS[frac]}SM" if whole else f"{_FRACTIONS[frac]}SM"
    return f"{vis:g}SM"


def fmt_clouds(clouds: list[CloudLayer], vert_vis_ft: int | None = None, limit: int = 4) -> str:
    parts = [c.code for c in clouds[:limit]]
    if vert_vis_ft is not None:
        parts.append(f"VV{vert_vis_ft // 100:03d}")
    return " ".join(parts) if parts else "SKC"


def fmt_ft(ft: float | None) -> str:
    return "--" if ft is None else f"{ft:,.0f} ft"


def fmt_alt(ft: int | None, text: str = "") -> str:
    """Advisory altitude: 'SFC', 'FZL', '080', 'FL240'."""
    if text:
        return text
    if ft is None:
        return "?"
    if ft == 0:
        return "SFC"
    return f"FL{ft // 100:03d}" if ft >= 18000 else f"{ft // 100:03d}"


def fmt_distance(nm: float | None, units: str) -> str:
    if nm is None:
        return ""
    if units == U.IMPERIAL:
        return f"{nm * 1.15078:.0f} mi"
    return f"{nm * 1.852:.0f} km"


def freshness(feed: Feed, now: datetime) -> Text:
    """'⚠ cached 2h05m' when the data came from the offline cache, '⚠ <error>' when failing."""
    t = Text()
    if feed.stale:
        t.append(f"⚠ offline · cached {fmt_age(feed.fetched_at, now)} ago", style=f"bold {WARN}")
    elif feed.error and feed.data is None:
        t.append(f"⚠ {feed.error}", style=WARN)
    return t


# ── header ───────────────────────────────────────────────────────────────────
def station_header(av: AviationResult | None, units: str, now: datetime) -> Text:
    t = Text(no_wrap=True, overflow="ellipsis")
    choice = av.choice if av else None
    if choice is None:
        t.append("finding nearest aviation station…", style=DIM)
        return t
    st = choice.station
    if st is None:
        t.append("✈ ", style=ACCENT)
        t.append("No aviation station", style=f"bold {WARN}")
        t.append(f"  {choice.error or 'unavailable'}", style=DIM)
        return t
    t.append(f"✈ {st.icao}", style=f"bold {ACCENT}")
    t.append(f" · {st.name}", style=f"bold {FG}")
    if choice.distance_nm is not None:
        far = choice.distance_nm > A.MAX_STATION_NM
        t.append(" · ", style=DIM)
        t.append(
            f"{fmt_distance(choice.distance_nm, units)} {U.compass(choice.bearing)}".strip(),
            style=f"bold {WARN}" if far else FG,
        )
    t.append("  (your choice)" if choice.source == "explicit" else "  (nearest)", style=DIM)
    m = av.metar
    if m:
        t.append("   ")
        t.append_text(metar_age_text(m, now))
    t.append(" ")
    t.append_text(freshness(av.metars, now))
    return t


def metar_age_text(m: MetarReport, now: datetime) -> Text:
    mins = (now - m.obs_time).total_seconds() / 60
    style = BAD if mins > 180 else WARN if mins > METAR_OLD_MIN else DIM
    return Text(f"{m.report_type} {fmt_age(m.obs_time, now)} old", style=style)


# ── METAR ────────────────────────────────────────────────────────────────────
def metar_panel(av: AviationResult, units: str, width: int, now: datetime) -> RenderableType:
    m = av.metar
    if m is None:
        if av.metars.loaded:
            msg = av.metars.error or (f"no recent METAR for {av.station.icao}" if av.station else "no station")
            return Text(msg, style=WARN if av.metars.error else DIM)
        return Text("loading METAR…", style=DIM)
    cat = A.metar_category(m)
    head = Text()
    head.append_text(cat_badge(cat))
    head.append(f"  {m.report_type} ", style=f"bold {BAD if m.is_speci else FG}")
    head.append(m.obs_time.strftime("%d %H%MZ"), style=FG)
    age = metar_age_text(m, now)
    head.append(f"  {age.plain.split(' ', 1)[1]}", style=age.style)
    if m.is_speci:
        head.append("  special obs", style=WARN)

    wind = Text(fmt_wind(m.wind_dir, m.wind_variable, m.wind_speed_kt, m.wind_gust_kt), style=f"bold {FG}")
    if (m.wind_gust_kt or 0) >= 25 or (m.wind_speed_kt or 0) >= 20:
        wind.stylize(f"bold {WARN}")
    ceiling = m.ceiling_ft
    ceil_txt = Text(fmt_ft(ceiling) if ceiling is not None else "none", style=f"bold {cat_color(A.flight_category(ceiling, None))}" if ceiling is not None else FG)
    vis_txt = Text(fmt_vis(m.visibility_sm, m.visibility_plus), style=f"bold {cat_color(A.flight_category(None, m.visibility_sm))}")
    temp = Text(f"{_c(m.temp_c)} / {_c(m.dewp_c)}", style=FG)
    if units == U.IMPERIAL and m.temp_c is not None:
        temp.append(f"  ({m.temp_c * 9 / 5 + 32:.0f}°F)", style=DIM)
    if m.temp_c is not None and m.dewp_c is not None and m.temp_c - m.dewp_c <= 2:
        temp.append("  spread ≤2°", style=WARN)
    alt = Text()
    if m.altimeter_hpa is not None:
        alt.append(f"{m.altimeter_inhg:.2f} inHg", style=FG)
        alt.append(f"  {m.altimeter_hpa:.0f} hPa", style=DIM)
    else:
        alt.append("--", style=DIM)
    if m.pressure_tendency_hpa is not None:
        tend = m.pressure_tendency_hpa
        alt.append(f"  3h {tend:+.1f} hPa (measured)", style=GOOD if tend > 1 else WARN if tend < -1 else DIM)

    grid = Table.grid(padding=(0, 1))
    grid.add_column(style=LABEL, no_wrap=True)
    grid.add_column()
    grid.add_row("Wind", wind)
    grid.add_row("Visibility", vis_txt)
    grid.add_row("Ceiling", ceil_txt)
    grid.add_row("Clouds", Text(fmt_clouds(m.clouds, m.vert_vis_ft), style=FG))
    if m.wx:
        grid.add_row("Weather", Text(m.wx, style=f"bold {WARN}"))
    grid.add_row("Temp/Dew", temp)
    grid.add_row("Altimeter", alt)
    raw = Text(m.raw, style="#c9d1d9")
    return Group(head, Text(), grid, Text(), Text("Raw", style=LABEL), raw)


def _c(v: float | None) -> str:
    return "--" if v is None else f"{v:.0f}°C"


# ── derived tools ────────────────────────────────────────────────────────────
def density_altitude_for(av: AviationResult) -> float | None:
    m, st = av.metar, av.station
    if m is None or m.temp_c is None or m.altimeter_hpa is None:
        return None
    elev_m = st.elev_m if st and st.elev_m is not None else m.elev_m
    if elev_m is None:
        return None
    return A.density_altitude_ft(elev_m * 3.28084, m.temp_c, m.altimeter_hpa, m.dewp_c)


def crosswind_text(m: MetarReport | None, runway: str, heading: int | None, magdec: float | None) -> Text:
    """'RWY 18 · wind 220/14G22 → Headwind 11 kt · Xwind 9G14 kt from right'."""
    t = Text()
    if heading is None:
        t.append("Runway  ", style=LABEL)
        t.append("press h to enter a runway heading", style=DIM)
        return t
    t.append(f"RWY {runway}" if runway else f"HDG {heading:03d}°", style=f"bold {ACCENT}")
    if m is None or m.wind_speed_kt is None:
        t.append("  no wind report", style=DIM)
        return t
    if m.wind_speed_kt == 0 and not m.wind_gust_kt:
        t.append(" · wind calm", style=FG)
        return t
    if m.wind_variable or m.wind_dir is None:
        t.append(f" · wind VRB {m.wind_speed_kt}kt — components vary", style=WARN)
        return t
    wdir = A.true_to_magnetic(m.wind_dir, magdec)
    gust = f"G{m.wind_gust_kt}" if m.wind_gust_kt else ""
    t.append(f" · wind {wdir:03.0f}/{m.wind_speed_kt}{gust}", style=FG)
    if magdec is not None:
        t.append("M", style=DIM)
    head, cross = A.wind_components(heading, wdir, m.wind_speed_kt)
    t.append("\n")
    if head >= 0:
        t.append(f"Headwind {head:.0f} kt", style=GOOD)
    else:
        t.append(f"Tailwind {-head:.0f} kt", style=f"bold {WARN}")
    xc = abs(cross)
    gx = abs(A.wind_components(heading, wdir, m.wind_gust_kt)[1]) if m.wind_gust_kt else 0.0
    t.append(" · ", style=DIM)
    xtxt = f"Xwind {xc:.0f}" + (f"G{gx:.0f}" if round(gx) > round(xc) else "")
    side = "" if round(max(xc, gx)) == 0 else " from right" if cross > 0 else " from left"
    peak = max(xc, gx)
    t.append(f"{xtxt} kt{side}", style=f"bold {BAD if peak >= 20 else WARN if peak >= 12 else FG}")
    return t


def trend_lines(metars: list[MetarReport], count: int = 6) -> list[Text]:
    """Oldest → newest, one observation per line, then the rule-based trend."""
    obs = list(reversed(metars[:count]))
    if not obs:
        return []
    out = []
    for m in obs:
        cat = A.metar_category(m)
        row = Text(no_wrap=True, overflow="ellipsis")
        row.append(m.obs_time.strftime("%H%MZ" if m.is_speci else "%HZ  "), style=LABEL)
        row.append(f"  {cat or '--':<5}", style=f"bold {cat_color(cat)}")
        row.append(f" {fmt_vis(m.visibility_sm, m.visibility_plus):<7}", style=FG)
        ceil = m.ceiling_ft
        row.append(f"{'cig ' + format(ceil, ',') if ceil is not None else 'no cig':<11}", style=FG if ceil is not None else DIM)
        if m.wx:
            row.append(m.wx, style=WARN)
        if m.is_speci:
            row.append(" SPECI", style=WARN)
        out.append(row)
    trend = A.condition_trend(metars, count)
    if trend:
        arrow, style = {"improving": ("▲", GOOD), "deteriorating": ("▼", BAD), "steady": ("▶", DIM)}[trend]
        label = Text(f"{arrow} {trend}", style=f"bold {style}" if trend != "steady" else style)
        label.append(f" over the last {len(obs)} reports", style=DIM)
        out.append(label)
    return out


def derived_panel(av: AviationResult, runway: str, heading: int | None, now: datetime) -> RenderableType:
    out: list[RenderableType] = []
    da = density_altitude_for(av)
    st = av.station
    t = Text()
    t.append("Density Alt  ", style=LABEL)
    if da is None:
        t.append("--", style=DIM)
    else:
        elev = st.elev_ft if st and st.elev_ft is not None else None
        high = elev is not None and da - elev >= 2000
        t.append(f"{da:,.0f} ft", style=f"bold {WARN if high else FG}")
        if elev is not None:
            t.append(f"  field {elev:,.0f} ft", style=DIM)
    out.append(t)
    out.append(crosswind_text(av.metar, runway, heading, st.magdec if st else None))
    if st and st.runways:
        rw = Text("Runways  ", style=LABEL)
        rw.append("  ".join(r.ident for r in st.runways[:6]), style=DIM)
        rw.append("   [ ] cycle · h heading", style=DIM)
        out.append(rw)
    if st and st.magdec is not None:
        out.append(Text(f"Wind converted to magnetic (variation {abs(st.magdec):.0f}°{'E' if st.magdec > 0 else 'W'})", style=DIM))
    lines = trend_lines(av.metars.data or [])
    if lines:
        out.append(Text())
        out.append(Text("Recent observations", style=LABEL))
        out.extend(lines)
    return Group(*out)


# ── TAF ──────────────────────────────────────────────────────────────────────
def taf_row(p: TafPeriod, eff: TafPeriod, prev_cat: str, now: datetime, cloud_w: int = 20) -> Text:
    cat = A.period_category(eff)
    t = Text(no_wrap=True, overflow="ellipsis")
    active = p.start <= now < p.end
    when = p.start.strftime("%d/%HZ") if p.change == "BASE" else p.start.strftime("%HZ")
    t.append("▸ " if active else "  ", style=ACCENT)
    t.append(f"{when:<6}", style=f"bold {FG}" if active else FG)
    label = p.label
    if p.is_temporary:
        label = f"{label} -{p.end.strftime('%HZ')}"
    elif p.change == "BECMG" and p.becmg_end:
        label = f"BECMG -{p.becmg_end.strftime('%HZ')}"
    t.append(f"{label:<13}", style=WARN if p.is_temporary else LABEL)
    t.append(f"{cat or '--':<5}", style=f"bold {cat_color(cat)}")
    change = ""
    if prev_cat and cat and not p.is_temporary:
        r0, r1 = A.category_rank(prev_cat), A.category_rank(cat)
        change = "▼" if r1 > r0 else "▲" if r1 < r0 else ""
    elif p.is_temporary and cat and A.category_rank(cat) > A.category_rank(prev_cat):
        change = "▽"
    t.append(f"{change:<2}", style=BAD if change in ("▼", "▽") else GOOD)
    shown_wind = p.wind_speed_kt is not None
    t.append(f"{wind_code(p.wind_dir, p.wind_variable, p.wind_speed_kt, p.wind_gust_kt) if shown_wind else '':<12}",
             style=WARN if (p.wind_gust_kt or 0) >= 25 else FG)
    t.append(f"{fmt_vis(p.visibility_sm, p.visibility_plus) if p.visibility_sm is not None else '':<8}", style=FG)
    clouds = fmt_clouds(p.clouds, p.vert_vis_ft, limit=3) if p.clouds or p.vert_vis_ft is not None else ""
    t.append(f"{clouds:<{cloud_w}}", style=FG)
    extra = " ".join(x for x in (p.wx, p.wind_shear) if x)
    if extra:
        t.append(extra, style=f"bold {WARN}")
    return t


def taf_panel(feed: Feed[TafForecast], station_has_taf: bool, now: datetime, show_raw: bool = True) -> RenderableType:
    taf = feed.data
    if taf is None:
        if not feed.loaded:
            return Text("loading TAF…", style=DIM)
        if feed.error:
            return Text(f"TAF unavailable: {feed.error}", style=WARN)
        return Text("No TAF is issued for this station" if not station_has_taf else "No current TAF", style=DIM)
    head = Text()
    head.append(f"{taf.station} ", style=f"bold {ACCENT}")
    head.append(f"issued {taf.issue_time.strftime('%d %H%MZ')}", style=FG)
    head.append(f" ({fmt_age(taf.issue_time, now)} ago)", style=DIM)
    head.append(f"  valid {taf.valid_from.strftime('%d/%HZ')}–{taf.valid_to.strftime('%d/%HZ')}", style=DIM)
    if taf.amended:
        head.append("  AMD", style=f"bold {WARN}")
    if taf.corrected:
        head.append("  COR", style=f"bold {WARN}")
    if taf.valid_to <= now:
        head.append("  EXPIRED", style=f"bold {BAD}")
    head.append(" ")
    head.append_text(freshness(feed, now))
    rows = [head, Text()]
    current = [(p, eff) for p, eff in A.taf_rows(taf) if p.end > now]
    cloud_w = max([20] + [len(fmt_clouds(p.clouds, p.vert_vis_ft, limit=3)) + 2 for p, _ in current])
    rows.append(Text(f"  {'FROM':<6}{'GROUP':<13}{'CAT':<5}  {'WIND':<12}{'VIS':<8}{'CLOUDS':<{cloud_w}}WX", style=LABEL))
    prev = ""
    for p, eff in current:
        rows.append(taf_row(p, eff, prev, now, cloud_w))
        if not p.is_temporary:
            prev = A.period_category(eff)
    if len(rows) == 3:
        rows.append(Text("  all forecast periods have ended", style=DIM))
    cur, changes, temps = A.taf_outlook(taf, now)
    rows.append(Text())
    rows.append(taf_outlook_text(cur, changes, temps))
    if show_raw:
        rows += [Text(), Text("Raw TAF", style=LABEL), Text(format_raw_taf(taf.raw), style="#c9d1d9")]
    return Group(*rows)


def format_raw_taf(raw: str) -> str:
    """One change group per line, the way TAFs are usually printed."""
    out, line = [], []
    for tok in raw.split():
        if line and (tok.startswith("FM") and tok[2:].isdigit() or tok in ("TEMPO", "BECMG") or tok.startswith("PROB")):
            # PROB30 TEMPO stays on one line
            if not (tok == "TEMPO" and line and line[-1].startswith("PROB")):
                out.append(" ".join(line))
                line = []
        line.append(tok)
    if line:
        out.append(" ".join(line))
    return "\n     ".join(out)


def taf_outlook_text(cur: str, changes: list, temps: list) -> Text:
    t = Text()
    t.append("TAF ", style=LABEL)
    t.append(cur or "--", style=f"bold {cat_color(cur)}")
    for cat, when in changes[:3]:
        t.append(" → ", style=DIM)
        t.append(cat, style=f"bold {cat_color(cat)}")
        t.append(f" {zulu(when)}", style=FG)
    if not changes:
        t.append(" through the forecast", style=DIM)
    if temps:
        p, cat = max(temps, key=lambda pc: A.category_rank(pc[1]))
        t.append(f" · {p.label} ", style=WARN)
        t.append(cat, style=f"bold {cat_color(cat)}")
        t.append(f" {zulu(p.start)}–{zulu(p.end)}", style=DIM)
    return t


def taf_summary(taf: TafForecast | None, now: datetime) -> Text:
    if taf is None:
        return Text("TAF --", style=DIM)
    if taf.valid_to <= now:
        return Text("TAF expired", style=WARN)
    return taf_outlook_text(*A.taf_outlook(taf, now))


# ── hazards and PIREPs ───────────────────────────────────────────────────────
def hazard_color(h: AviationHazard) -> str:
    return HAZARD_COLORS.get(h.hazard.split()[0] if h.hazard else "", "#c9d1d9")


def hazard_altitudes(h: AviationHazard) -> str:
    """'060-FL180', 'FZL 060-090-FL180', 'tops FL450', 'to FL180', or ''."""
    has_base = h.base_ft is not None or bool(h.base_text)
    if not has_base and h.top_ft is None:
        return ""
    if not has_base:
        return f"{'tops' if 'CONVECTIVE' in h.kind or h.hazard == 'TS' else 'to'} {fmt_alt(h.top_ft)}"
    top = fmt_alt(h.top_ft) if h.top_ft is not None else "?"
    return f"{fmt_alt(h.base_ft, h.base_text)}-{top}"


def hazard_card(h: AviationHazard, now: datetime) -> Text:
    """'SIGMET NOVEMBER 3 · TS · 42m left' with altitudes and position on a second line."""
    t = Text(no_wrap=True, overflow="ellipsis")
    color = hazard_color(h)
    t.append("■ ", style=color)
    t.append(h.name, style=f"bold {FG}")
    t.append(f" · {h.hazard}", style=f"bold {color}")
    if h.kind == "G-AIRMET" and h.valid_from:
        t.append(f" · snapshot {zulu(h.valid_from, True)}", style=DIM)
    elif h.valid_from and h.valid_from > now:
        t.append(f" · from {zulu(h.valid_from, True)}", style=DIM)
    left = fmt_until(h.valid_to, now)
    if left:
        t.append(f" · {left} left", style=DIM)
    t.append("\n  ")
    alts = hazard_altitudes(h)
    if alts:
        t.append(f"{alts}  ", style=FG)
    if h.contains_point:
        t.append("over station", style=f"bold {WARN}")
    elif h.distance_nm is not None:
        t.append(f"{h.distance_nm:.0f} NM away", style=DIM)
    if h.movement:
        t.append(f"  {h.movement}", style=DIM)
    if h.severity and h.kind == "G-AIRMET":
        t.append(f"  {h.severity}", style=DIM)
    return t


def hazard_detail(h: AviationHazard, now: datetime) -> Group:
    head = Text()
    head.append(f" {h.hazard or '?'} ", style=f"bold #0d1117 on {hazard_color(h)}")
    head.append(f"  {h.name}", style=f"bold {FG}")
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style=LABEL)
    meta.add_column()
    meta.add_row("Product", h.kind)
    meta.add_row("Snapshot" if h.kind == "G-AIRMET" else "Valid", f"{h.valid_from.strftime('%d %H%MZ') if h.valid_from else '?'} – "
                          f"{h.valid_to.strftime('%d %H%MZ') if h.valid_to else '?'}  ({fmt_until(h.valid_to, now)} left)")
    if hazard_altitudes(h):
        meta.add_row("Altitude", hazard_altitudes(h))
    meta.add_row("Position", "covers the station" if h.contains_point else f"{h.distance_nm:.0f} NM from the station" if h.distance_nm is not None else "--")
    if h.qualifier:
        meta.add_row("Detail", h.qualifier)
    if h.movement:
        meta.add_row("Movement", h.movement)
    if h.issuer:
        meta.add_row("Issuer", h.issuer)
    return Group(head, Text(), meta, Text(), Text("Official text", style=LABEL), Text(h.raw or "(none supplied)", style="#c9d1d9"))


def _cond_text(conds) -> str:
    parts = []
    for c in conds:
        rng = ""
        if c.base_ft is not None or c.top_ft is not None:
            rng = f" {(c.base_ft or 0) // 100:03d}-{(c.top_ft or 0) // 100:03d}" if c.top_ft else f" {(c.base_ft or 0) // 100:03d}"
        parts.append(" ".join(x for x in (c.freq, c.intensity, c.type) if x) + rng)
    return ", ".join(parts)


def pirep_line(p: Pirep, now: datetime) -> Text:
    sig = A.pirep_significance(p)
    color = {3: BAD, 2: WARN, 1: "#d670f0"}.get(sig, "#c9d1d9")
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append("● " if sig >= 2 else "· ", style=color)
    if p.urgent:
        t.append("UUA ", style=f"bold {BAD}")
    t.append(f"{fmt_age(p.obs_time, now):>5} ", style=DIM)
    if p.distance_nm is not None:
        t.append(f"{p.distance_nm:>3.0f}NM {U.compass(p.bearing):<3} ", style=FG)
    t.append(f"{fmt_alt(p.altitude_ft) if p.altitude_ft is not None else '???':>5} ", style=f"bold {FG}")
    t.append(f"{(p.aircraft or '?')[:6]:<6} ", style=DIM)
    bits = []
    tb, ic = A.worst(p.turbulence), A.worst(p.icing)
    if tb and A.intensity_level(tb.intensity) >= 0:
        bits.append(("TB " + _cond_text([tb]), WARN if A.intensity_level(tb.intensity) >= 2 else "#c9d1d9"))
    if ic and A.intensity_level(ic.intensity) >= 0:
        bits.append(("IC " + _cond_text([ic]), "#6cb6ff"))
    if p.clouds and sig <= 1:
        bits.append(("SK " + " ".join(c.cover + (f"{c.base_ft // 100:03d}" if c.base_ft else "") for c in p.clouds[:2]), DIM))
    if p.wx:
        bits.append(("WX " + p.wx, WARN))
    if p.temp_c is not None:
        bits.append((f"{p.temp_c:+.0f}°C", DIM))
    for i, (txt, style) in enumerate(bits):
        t.append(("  " if i else "") + txt, style=style)
    return t


def pirep_detail(p: Pirep, now: datetime) -> Group:
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style=LABEL)
    meta.add_column()
    meta.add_row("Type", f"{p.report_type}{' (URGENT)' if p.urgent else ''}")
    meta.add_row("Time", f"{p.obs_time.strftime('%d %H%MZ')} ({fmt_age(p.obs_time, now)} ago)")
    if p.distance_nm is not None:
        meta.add_row("Position", f"{p.distance_nm:.0f} NM {U.compass(p.bearing)} of the station")
    meta.add_row("Altitude", f"{p.altitude_ft:,} ft" + (f" ({p.phase})" if p.phase else "") if p.altitude_ft is not None else "--")
    meta.add_row("Aircraft", p.aircraft or "--")
    if p.turbulence:
        meta.add_row("Turbulence", _cond_text(p.turbulence))
    if p.icing:
        meta.add_row("Icing", _cond_text(p.icing))
    if p.clouds:
        meta.add_row("Sky", " ".join(c.cover + (f" {c.base_ft:,}" if c.base_ft else "") + (f"-{c.top_ft:,}" if c.top_ft else "") for c in p.clouds))
    if p.visibility_sm is not None:
        meta.add_row("Visibility", fmt_vis(p.visibility_sm))
    if p.wx:
        meta.add_row("Weather", p.wx)
    if p.temp_c is not None:
        meta.add_row("Temperature", f"{p.temp_c:.0f}°C")
    if p.wind_speed_kt:
        meta.add_row("Wind", f"{p.wind_dir or 0:03d}° {p.wind_speed_kt} kt")
    if p.braking:
        meta.add_row("Braking", p.braking)
    return Group(Text("Pilot report", style=f"bold {ACCENT}"), Text(), meta, Text(), Text("Raw report", style=LABEL), Text(p.raw, style="#c9d1d9"))


# ── winds aloft ──────────────────────────────────────────────────────────────
WINDS_LEVELS = (3000, 6000, 9000, 12000, 18000, 24000, 30000, 34000, 39000)


def winds_panel(feed: Feed[WindsAloft], now: datetime, levels: tuple[int, ...] = WINDS_LEVELS[:6]) -> RenderableType:
    w = feed.data
    if w is None:
        if not feed.loaded:
            return Text("loading winds aloft…", style=DIM)
        return Text(feed.error or "no winds-aloft forecast site nearby", style=WARN if feed.error else DIM)
    head = Text()
    head.append(f"FD site {w.site}", style=f"bold {ACCENT}")
    if w.distance_nm is not None:
        head.append(f" · {w.distance_nm:.0f} NM", style=DIM)
    if w.valid:
        head.append(f" · valid {w.valid.strftime('%d %H%MZ')}", style=FG)
    if w.use_period:
        head.append(f" for use {w.use_period}", style=DIM)
        if w.use_to is not None and w.use_to <= now:
            head.append("  ⚠ past its use period", style=f"bold {WARN}")
    head.append(" ")
    head.append_text(freshness(feed, now))
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(justify="right", style=LABEL)
    tbl.add_column(justify="right")
    tbl.add_column(justify="right")
    tbl.add_column()
    tbl.add_row("ALT", "WIND", "TEMP", "")
    fz = A.freezing_level_ft(w.levels)
    rows = [lv for lv in w.levels if lv.altitude_ft in levels]
    prev = None
    for lv in rows:
        if lv.wind_speed_kt is None:
            wind = Text("--", style=DIM)
        elif lv.wind_dir is None:
            wind = Text("lgt/vrb", style=DIM)
        else:
            style = BAD if lv.wind_speed_kt >= 80 else WARN if lv.wind_speed_kt >= 50 else FG
            wind = Text(f"{lv.wind_dir:03d}/{lv.wind_speed_kt:>2}", style=f"bold {style}" if lv.wind_speed_kt >= 50 else style)
        temp = Text("—" if lv.temp_c is None else f"{lv.temp_c:+03d}", style="#6cb6ff" if lv.temp_c is not None and lv.temp_c <= 0 else FG)
        note = Text()
        if lv.temp_c is not None and abs(lv.temp_c) <= 2:
            note.append("≈0°C ", style="#6cb6ff")
        if prev is not None and (prev.wind_speed_kt or 0) >= 10 and (lv.wind_speed_kt or 0) >= 10:
            turn = A.direction_change(prev.wind_dir, lv.wind_dir)
            if turn >= 60:
                note.append(f"turns {turn}°", style=WARN)
        tbl.add_row(f"{lv.altitude_ft:,}", wind, temp, note)
        prev = lv
    foot = Text()
    if fz is not None:
        foot.append("Freezing level ≈ ", style=LABEL)
        foot.append(f"{fz:,.0f} ft", style="bold #6cb6ff")
        foot.append(" (forecast)", style=DIM)
    note = Text("Forecast winds, not observations — not a substitute for formal flight planning.", style=DIM)
    return Group(head, Text(), tbl, foot, note)


# ── summaries ────────────────────────────────────────────────────────────────
def hazard_counts(av: AviationResult, now: datetime) -> tuple[int, int, int, int]:
    """(SIGMETs, AIRMETs incl. G-AIRMETs, CWAs, PIREPs) currently relevant."""
    hz = A.relevant_hazards(av.hazards.data.items, now) if av.hazards.data else []
    sig = sum(1 for h in hz if "SIGMET" in h.kind)
    air = sum(1 for h in hz if "AIRMET" in h.kind)
    cwa = sum(1 for h in hz if h.kind == "CWA")
    pir = len(A.recent_pireps(av.pireps.data, now)) if av.pireps.data is not None else 0
    return sig, air, cwa, pir


def hazards_line(av: AviationResult, now: datetime) -> Text:
    t = Text()
    if not av.hazards.loaded and not av.pireps.loaded:
        return Text("hazards loading…", style=DIM)
    sig, air, cwa, pir = hazard_counts(av, now)
    t.append(f"{sig} SIGMET", style=f"bold {BAD}" if sig else DIM)
    t.append(" · ", style=DIM)
    t.append(f"{air} AIRMET", style=f"bold {WARN}" if air else DIM)
    if cwa:
        t.append(f" · {cwa} CWA", style=f"bold {WARN}")
    t.append(" · ", style=DIM)
    if av.pireps.data is None and av.pireps.error:
        t.append("PIREPs unavailable", style=WARN)
    else:
        t.append(f"{pir} PIREP{'s' if pir != 1 else ''}/{A.PIREP_RADIUS_NM}NM", style=FG if pir else DIM)
    if av.hazards.data is None and av.hazards.error:
        t.append(" · advisories unavailable", style=WARN)
    if av.hazards.stale or av.pireps.stale:
        t.append(" ⚠ cached", style=WARN)
    return t


def aviation_summary(av: AviationResult | None, width: int, units: str = U.IMPERIAL, now: datetime | None = None) -> RenderableType:
    """Compact dashboard panel; the full picture lives on the Aviation screen (v)."""
    now = now or A.utcnow()
    if av is None or av.choice is None:
        return Text("finding nearest aviation station…", style=DIM)
    st = av.station
    if st is None:
        return Group(Text(f"✈ no aviation station · {av.choice.error or 'unavailable'}", style=DIM),
                     Text("press v for options", style=DIM))
    line1 = Text(no_wrap=True, overflow="ellipsis")
    line1.append(st.icao, style=f"bold {ACCENT}")
    line1.append("  ")
    m = av.metar
    line1.append_text(cat_badge(A.metar_category(m) if m else ""))
    if m:
        line1.append("  ")
        line1.append_text(metar_age_text(m, now))
    if av.choice.distance_nm is not None and width >= 40:
        line1.append(f"  {fmt_distance(av.choice.distance_nm, units)} {U.compass(av.choice.bearing)}", style=DIM)
    lines = [line1]
    if m:
        line2 = Text(no_wrap=True, overflow="ellipsis")
        wind = fmt_wind(m.wind_dir, m.wind_variable, m.wind_speed_kt, m.wind_gust_kt)
        line2.append(wind, style=FG)
        line2.append(" · ", style=DIM)
        line2.append(fmt_vis(m.visibility_sm, m.visibility_plus), style=FG)
        line2.append(" · ", style=DIM)
        line2.append(fmt_clouds(m.clouds, m.vert_vis_ft, limit=2), style=FG)
        if m.wx:
            line2.append(f" · {m.wx}", style=WARN)
        lines.append(line2)
    elif av.metars.loaded:
        lines.append(Text(av.metars.error or "no recent METAR", style=WARN if av.metars.error else DIM))
    lines.append(taf_summary(av.taf.data, now) if st.has_taf or av.taf.data else Text("no TAF for this station", style=DIM))
    lines.append(hazards_line(av, now))
    stale = freshness(av.metars, now)
    if stale.plain:
        lines.append(stale)
    return Group(*lines)


def once_section(av: AviationResult, units: str, now: datetime) -> RenderableType:
    """Compact block for `termwx --once`."""
    st = av.station
    head = Text()
    if st is None:
        head.append(f"no aviation station · {av.choice.error if av.choice else 'unavailable'}", style=DIM)
        return head
    m = av.metar
    cat = A.metar_category(m) if m else ""
    head.append(st.icao, style=f"bold {ACCENT}")
    head.append(f" · {st.name}", style=FG)
    if av.choice and av.choice.distance_nm is not None:
        head.append(f" ({fmt_distance(av.choice.distance_nm, units)} {U.compass(av.choice.bearing)})", style=DIM)
    head.append(" · ")
    head.append(cat or "--", style=f"bold {cat_color(cat)}")
    lines: list[RenderableType] = [head]
    if m:
        t = Text()
        t.append(f"{m.report_type} {fmt_age(m.obs_time, now)} old", style=WARN if (now - m.obs_time).total_seconds() > METAR_OLD_MIN * 60 else DIM)
        t.append(" · ", style=DIM)
        t.append(" ".join([wind_code(m.wind_dir, m.wind_variable, m.wind_speed_kt, m.wind_gust_kt),
                           fmt_vis(m.visibility_sm, m.visibility_plus), fmt_clouds(m.clouds, m.vert_vis_ft, 3)]
                          + ([m.wx] if m.wx else [])), style=FG)
        lines.append(t)
    else:
        lines.append(Text(av.metars.error or "no recent METAR", style=DIM))
    if st.has_taf or av.taf.data:
        lines.append(taf_summary(av.taf.data, now))
    lines.append(hazards_line(av, now))
    stale = [name for name, f in (("METAR", av.metars), ("TAF", av.taf), ("PIREP", av.pireps), ("hazards", av.hazards)) if f.stale]
    if stale:
        lines.append(Text(f"⚠ offline – cached {', '.join(stale)} shown", style=WARN))
    return Group(*lines)


# ── forecast discussion ──────────────────────────────────────────────────────
def discussion_indicator(disc, error: str = "", now: datetime | None = None) -> Text:
    """'AFD GSP · updated 38m ago · d' for the dashboard."""
    now = now or A.utcnow()
    t = Text(no_wrap=True, overflow="ellipsis")
    if disc is None:
        if error:
            t.append(f"AFD unavailable: {error}", style=DIM)
        return t
    t.append(f"AFD {disc.office}", style=f"bold {ACCENT}")
    t.append(f" · issued {fmt_age(disc.issued, now)} ago", style=DIM)
    if disc.stale:
        t.append(" ⚠ cached", style=WARN)
    t.append("  d", style=DIM)
    return t
