"""Rich renderables for the drone go/no-go checklist, shared by the dashboard
panel and the drone screen."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from . import drone as D
from .render import ACCENT, DIM, LABEL, hhmm, kv, sparkline
from .render_aviation import BAD, FG, GOOD, WARN

STATUS_COLORS = {D.GO: GOOD, D.CAUTION: WARN, D.NOGO: BAD, D.UNKNOWN: DIM, D.NOT_READY: ACCENT}
ICONS = {D.GO: "✔", D.CAUTION: "▲", D.NOGO: "✖", D.UNKNOWN: "?"}
DISCLAIMER = "Advisory only — the remote pilot in command is responsible for the go/no-go decision (§107.19)."


def verdict_badge(verdict: str) -> Text:
    return Text(f" {verdict} ", style=f"bold #0d1117 on {STATUS_COLORS.get(verdict, DIM)}")


def _local(dt: datetime, tz: str) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(tz)) if tz else dt.astimezone()
    except (KeyError, ValueError):
        return dt.astimezone()


def window_text(r: D.GoNoGo) -> str:
    """'2:10p–4:10p EDT · 400 ft AGL'."""
    start, end = _local(r.window_start, r.timezone), _local(r.window_end, r.timezone)
    return f"{hhmm(start)}–{hhmm(end)} {end.strftime('%Z')} · {r.altitude_ft} ft AGL"


def preflight_text(r: D.GoNoGo) -> Text:
    done = r.manual_done == r.manual_total
    return Text(f"preflight {r.manual_done}/{r.manual_total}", style=GOOD if done else WARN)


def drone_summary(r: D.GoNoGo | None, width: int) -> RenderableType:
    """Dashboard panel: verdict, the worst reasons, preflight progress."""
    if r is None:
        return Text("waiting for weather…", style=DIM)
    line1 = Text(no_wrap=True, overflow="ellipsis")
    line1.append_text(verdict_badge(r.verdict))
    hours = round((r.window_end - r.window_start).total_seconds() / 3600)
    line1.append(f"  next {hours}h · {r.altitude_ft} ft", style=DIM)
    reasons = Text(overflow="ellipsis")
    for i, item in enumerate(r.problems[:4]):
        if i:
            reasons.append(" · ", style=DIM)
        color = STATUS_COLORS[item.status]
        reasons.append(f"{ICONS[item.status]} ", style=color)
        reasons.append(f"{item.label.lower()}: {item.value}", style=color if item.status == D.NOGO else FG)
    if not r.problems:
        reasons.append("all weather checks within limits", style=GOOD)
    line3 = preflight_text(r)
    line3.append("  ·  g for the checklist", style=DIM)
    return Group(line1, reasons, line3)


def _cell(s: str, n: int) -> str:
    """Pad to a fixed column, truncating with an ellipsis (one space always separates columns)."""
    return (s[: n - 2] + "…" if len(s) > n - 1 else s).ljust(n)


def check_line(item: D.CheckItem, width: int = 80) -> Text:
    """'✔ Visibility        10+ SM   ≥ 3 SM   KCLT'."""
    t = Text(no_wrap=True, overflow="ellipsis")
    color = STATUS_COLORS[item.status]
    t.append(f"{ICONS[item.status]} ", style=f"bold {color}")
    t.append(_cell(item.label, 21), style=FG)
    t.append(_cell(item.value, 24), style=f"bold {color}" if item.status != D.GO else FG)
    if width >= 70:
        t.append(_cell(item.limit, 24), style=LABEL)
    if width >= 90 and item.source:
        t.append(item.source, style=DIM)
    return t


def check_detail(item: D.CheckItem) -> Group:
    head = Text()
    head.append_text(verdict_badge(item.status))
    head.append(f"  {item.label}", style=f"bold {FG}")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=LABEL)
    grid.add_column()
    grid.add_row("Value", Text(item.value or "--", style=f"bold {STATUS_COLORS[item.status]}"))
    grid.add_row("Limit", Text(item.limit or "--", style=FG))
    if item.source:
        grid.add_row("Source", Text(item.source, style=FG))
    if item.rule:
        grid.add_row("Rule", Text(item.rule, style=FG))
    parts: list[RenderableType] = [head, Text(), grid]
    if len(item.series) >= 2:
        parts += [Text(), kv("Hourly over the window", Text(sparkline(item.series), style=ACCENT))]
    if item.notes:
        parts.append(Text())
        parts += [Text(f"• {n}", style="#c9d1d9") for n in item.notes]
    return Group(*parts)


def manual_line(m: D.ManualItem, ticked: bool) -> Text:
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append("☑ " if ticked else "☐ ", style=f"bold {GOOD if ticked else WARN}")
    t.append(f"{m.label:<36}", style=FG if ticked else "#c9d1d9")
    t.append(m.rule, style=DIM)
    return t


def manual_detail(m: D.ManualItem, ticked: bool, note: str = "") -> Group:
    head = Text()
    head.append(" DONE " if ticked else " TO DO ", style=f"bold #0d1117 on {GOOD if ticked else WARN}")
    head.append(f"  {m.label}", style=f"bold {FG}")
    parts: list[RenderableType] = [head, Text(), Text(m.detail, style="#c9d1d9"), Text(), kv("Rule", m.rule)]
    if note:
        parts += [Text(), Text(note, style=WARN)]
    parts += [Text(), Text("space or enter to tick", style=DIM)]
    return Group(*parts)


def screen_header(r: D.GoNoGo | None, place: str) -> Text:
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(" DRONE GO/NO-GO ", style="bold #0d1117 on #58a6ff")
    t.append(f"  {place}", style=f"bold {FG}")
    if r is None:
        t.append("  waiting for weather…", style=DIM)
        return t
    t.append(f"  {window_text(r)}  ", style="#c9d1d9")
    t.append_text(verdict_badge(r.verdict))
    t.append("  ")
    t.append_text(preflight_text(r))
    return t
