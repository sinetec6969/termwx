"""Non-interactive snapshot: print everything once and exit."""

from __future__ import annotations

import asyncio

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import aviation as A, render as R, render_aviation as RA, wmo
from .api import awc, faa_notam, faa_tfr, geo, nws, openmeteo, swpc
from .api.http import FetchError, Http
from .aviation_models import AviationResult
from .config import Config
from .demo import demo_alerts, demo_tfr
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


async def fetch_aviation(http: Http, loc: Location, explicit: str | None, demo: bool = False) -> AviationResult:
    """Station + METAR/TAF/PIREPs/advisories/TFRs (winds, NOTAMs) concurrently; each product fails on its own."""
    choice = await awc.choose_station(http, loc.lat, loc.lon, explicit or None)
    av = AviationResult(choice=choice)
    st = choice.station
    ref = (st.lat, st.lon) if st else (loc.lat, loc.lon)
    jobs = {"hazards": awc.fetch_hazards(http, *ref), "winds": awc.fetch_winds(http, *ref),
            "tfrs": faa_tfr.fetch_tfrs(http, loc.lat, loc.lon)}
    if st:
        jobs |= {"metars": awc.fetch_metars(http, st.icao), "taf": awc.fetch_taf(http, st.icao), "pireps": awc.fetch_pireps(http, st)}
        if faa_notam.configured():
            jobs["notams"] = faa_notam.fetch_notams(http, st.icao)
    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    now = A.utcnow()
    for name, res in zip(jobs, results):
        feed = getattr(av, name)
        if isinstance(res, (FetchError, KeyError, TypeError, ValueError, IndexError)):
            feed.fail(str(res) or res.__class__.__name__)
        elif isinstance(res, BaseException):
            raise res
        else:
            feed.ok(res[0], res[1], now)
    if demo:
        av.tfrs.data = [demo_tfr(loc.lat, loc.lon)] + (av.tfrs.data or [])
    return av


async def snapshot(
    cfg: Config, query: str | None, units: str, demo: bool = False, aviation_detail: bool = False,
    http: Http | None = None, console: Console | None = None,
) -> int:
    console = console or Console()
    http = http or Http()
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
            fetch_aviation(http, loc, cfg.aviation_station, demo),
            return_exceptions=True,
        )
    finally:
        await http.aclose()

    weather, alerts, space, aviation = results
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
    if isinstance(aviation, BaseException):
        bottom.append(_panel(Text(f"aviation unavailable: {aviation}", style="red"), "Aviation"))
    else:
        now = A.utcnow()
        bottom.append(_panel(RA.once_section(aviation, units, now), "Aviation"))
        if aviation_detail and aviation.station:
            bottom += aviation_snapshot(aviation, units, width, now)
    console.print(Group(*bottom))
    return 0


def aviation_snapshot(av: AviationResult, units: str, width: int, now) -> list:
    """`--once --aviation`: METAR, TAF timeline, advisories and the closest PIREPs — no full AFD dump."""
    out = [
        _panel(RA.metar_panel(av, units, width - 4, now), f"METAR · {av.station.icao}"),
        _panel(RA.taf_panel(av.taf, av.station.has_taf, now), "TAF"),
    ]
    hz = A.relevant_hazards(av.hazards.data.items, now) if av.hazards.data else []
    cards = [RA.hazard_card(h, now) for h in hz[:6]] or [Text(av.hazards.error or "no advisories within 50 NM", style=R.DIM)]
    out.append(_panel(Group(*cards), "Advisories"))
    tfrs = A.relevant_tfrs(av.tfrs.data, now) if av.tfrs.data else []
    cards = [RA.tfr_card(t, now) for t in tfrs[:6]] or [Text(av.tfrs.error or f"none within {A.TFR_NEAR_NM} NM", style=R.DIM)]
    out.append(_panel(Group(*cards), "TFRs · tfr.faa.gov"))
    if faa_notam.configured():
        lines = [RA.notam_line(n, now) for n in (av.notams.data or [])[:12]] or [Text(av.notams.error or "none", style=R.DIM)]
        out.append(_panel(Group(*lines), f"NOTAMs · {av.station.icao}"))
    pireps = A.recent_pireps(av.pireps.data, now)[:8] if av.pireps.data else []
    lines = [RA.pirep_line(p, now) for p in pireps] or [Text(av.pireps.error or "no recent PIREPs", style=R.DIM)]
    out.append(_panel(Group(*lines), f"PIREPs · {A.PIREP_RADIUS_NM} NM"))
    out.append(_panel(RA.winds_panel(av.winds, now), "Winds aloft"))
    out.append(Text(RA.DISCLAIMER, style=R.DIM))
    return out
