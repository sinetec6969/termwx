"""Non-interactive snapshot: print everything once and exit."""

from __future__ import annotations

import asyncio

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import render as R, wmo
from .api import geo, nws, openmeteo, swpc
from .api.http import FetchError, Http
from .config import Config
from .demo import demo_alerts
from .models import AlertsResult, Location
from .scene import SceneState, render_scene

BORDER = "#30363d"


def _panel(body, title: str, border: str = BORDER) -> Panel:
    return Panel(body, title=f"[bold {R.ACCENT}]{escape(title)}[/]", title_align="left", border_style=border, padding=(0, 1))


async def resolve_location(http: Http, cfg: Config, query: str | None) -> Location:
    if query:
        results = await geo.search(http, query)
        if not results:
            raise FetchError(f"no location found for {query!r}")
        return results[0]
    if cfg.location:
        return cfg.location
    return await geo.autolocate(http)


async def snapshot(cfg: Config, query: str | None, units: str, demo: bool = False) -> int:
    console = Console()
    http = Http()
    try:
        try:
            loc = await resolve_location(http, cfg, query)
        except FetchError as e:
            console.print(f"[red]location error:[/] {e}")
            return 1
        results = await asyncio.gather(
            openmeteo.fetch_weather(http, loc, units),
            nws.fetch_alerts(http, loc),
            swpc.fetch_space(http, loc.lat, loc.lon),
            return_exceptions=True,
        )
    finally:
        await http.aclose()

    weather, alerts, space = results
    width = console.width

    title = Text()
    title.append(" termwx ", style="bold #0d1117 on #58a6ff")
    title.append(f"  {loc.label}", style="bold #e6edf3")
    title.append(f"  {loc.coords}", style=R.DIM)
    now = R.local_now(loc.timezone)
    title.append(f"   {now.strftime('%a %b %-d')} {R.hhmm(now)} {now.strftime('%Z')}", style=R.DIM)
    console.print(title)

    if isinstance(alerts, BaseException):
        alerts = None
    if demo:
        alerts = alerts or AlertsResult([])
        alerts.alerts = demo_alerts() + alerts.alerts
    if alerts and alerts.alerts:
        for a in alerts.alerts:
            body = Text()
            body.append(a.headline + "\n", style="bold")
            body.append(a.area[:200], style=R.DIM)
            console.print(_panel(body, f"⚠ {a.event}", R.alert_color(a)))

    if isinstance(weather, BaseException):
        console.print(f"[red]weather unavailable:[/] {weather}")
    else:
        c = weather.current
        state = SceneState.from_weather(c.code, c.is_day, c.wind_speed, c.wind_dir, c.cloud_cover, units)
        top = Table.grid(padding=(0, 1), expand=True)
        cur_w = max(40, width - 44) if width >= 90 else width - 4
        if width >= 90:
            top.add_column(ratio=1)
            top.add_column(width=40)
            top.add_row(
                _panel(R.current_panel(weather, cur_w - 4), "Now"),
                _panel(render_scene(state, 36, 10, 0), wmo.condition(c.code).label),
            )
        else:
            top.add_column()
            top.add_row(_panel(R.current_panel(weather, cur_w), "Now"))
        console.print(top)
        console.print(_panel(R.hourly_panel(weather, width - 4), "Next hours"))
        console.print(_panel(R.forecast_panel(weather, width - 4), "3-Day Forecast"))

    bottom = []
    bottom.append(_panel(R.alerts_summary(alerts), "NWS Alerts"))
    if isinstance(space, BaseException):
        bottom.append(_panel(Text(f"space weather unavailable: {space}", style="red"), "Space Weather"))
    else:
        bottom.append(_panel(R.space_panel(space, min(width - 4, 60)), "Space Weather"))
    console.print(Group(*bottom))
    return 0
