"""The termwx Textual application."""

from __future__ import annotations

import asyncio
import random
import shutil

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Static

from rich.console import Group

from . import aviation as A, drone as D, render as R, render_aviation as RA, render_drone as RD, units as U, wmo
from .api import awc, faa_notam, geo, nws, openmeteo, quips, swpc
from .api.http import FetchError, Http
from .aviation_models import AviationResult, StationChoice
from .config import Config
from .demo import demo_alerts
from .models import Alert, AlertsResult, ForecastDiscussion, Location, SpaceWeather, Weather
from .scene import SceneState
from .screens import AUTO, AlertsScreen, HelpScreen, LocationScreen, SpaceScreen
from .screens_aviation import AviationScreen, DiscussionScreen
from .screens_drone import DroneScreen
from .widgets import AlertBanner, DataPanel, ScenePanel

WEATHER_EVERY = 600
ALERTS_EVERY = 120
SPACE_EVERY = 300
# Aviation products refresh independently; station metadata only on location/station change.
METAR_EVERY = 300
TAF_EVERY = 600
HAZARDS_EVERY = 300  # SIGMET / G-AIRMET / CWA and PIREPs
WINDS_EVERY = 1800  # FD winds aloft are issued four times a day
AFD_EVERY = 1200
AV_ERRORS = (FetchError, KeyError, TypeError, ValueError, IndexError)
VISITOR_MIN, VISITOR_MAX = 500, 900  # seconds between sky visitors, picked at random

WIDE = 120
TINY = 76


def swpc_as_alerts(sw: SpaceWeather) -> list[Alert]:
    severity = {3: "Moderate", 4: "Severe", 5: "Extreme"}
    return [
        Alert(
            id=f"swpc-{m.product_id}-{m.issued.isoformat()}",
            event=f"Space Weather {m.scale}: {m.title.split(':', 1)[-1].strip()}",
            severity=severity.get(m.level, "Moderate"),
            headline=m.title,
            description=m.body,
            onset=m.issued.astimezone(),
            sender="NOAA Space Weather Prediction Center",
            source="SWPC",
        )
        for m in swpc.recent_major_messages(sw)
    ]


class TermWx(App):
    CSS_PATH = "app.tcss"
    TITLE = "termwx"
    BINDINGS = [
        Binding("l", "location", "Location"),
        Binding("u", "units", "°F/°C"),
        Binding("w", "alerts", "Alerts"),
        Binding("x", "space", "Space Wx"),
        Binding("v", "aviation", "Aviation"),
        Binding("d", "discussion", "Discussion"),
        Binding("g", "drone", "Drone"),
        Binding("s", "favorite", "Save ★"),
        Binding("f", "next_favorite", "Next ★"),
        Binding("a", "autolocate", "Auto-locate", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "toggle_animation", "Pause anim", show=False),
        Binding("V", "visitor", "Visitor", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config: Config,
        http: Http | None = None,
        location_query: str | None = None,
        demo_alert: bool = False,
        save_config: bool = True,
    ):
        super().__init__()
        self.config = config
        self.http = http or Http()
        self.location_query = location_query
        self.demo_alert = demo_alert
        self.save_config = save_config
        self.location: Location | None = None
        self.weather: Weather | None = None
        self.alerts_result: AlertsResult | None = None
        self.space: SpaceWeather | None = None
        self.all_alerts: list[Alert] = []
        self.seen_alert_ids: set[str] = set()
        self.errors: dict[str, str] = {}
        self.aviation = AviationResult()
        self.discussion: ForecastDiscussion | None = None
        self.discussion_error = ""
        self.office: str | None = None
        self._office_loc: Location | None = None
        self.runway: tuple[str, int | None] = ("", None)  # (designator, magnetic heading)
        self._runway_auto = True
        self.drone: D.GoNoGo | None = None
        self.drone_ticks: set[str] = set()  # manual preflight items, this session and location only

    # ── layout ───────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        yield AlertBanner(id="banner")
        with VerticalScroll(id="body"):
            with Horizontal(id="columns"):
                with Vertical(id="left"):
                    with Horizontal(id="row1"):
                        yield DataPanel(R.current_panel, "locating…", "Now", id="current")
                        yield ScenePanel(animate=self.config.animate, id="scene")
                    yield DataPanel(R.hourly_panel, "loading hourly…", "Next hours", id="hourly")
                    yield DataPanel(R.forecast_panel, "loading forecast…", "3-Day Forecast", id="forecast")
                with Vertical(id="right"):
                    yield DataPanel(self._render_alerts, "checking alerts…", "NWS Alerts", id="alerts")
                    yield DataPanel(self._render_aviation, "finding aviation station…", "Aviation  v", id="aviation")
                    if self.config.drone.panel:
                        yield DataPanel(RD.drone_summary, "waiting for weather…", "Drone go/no-go  g", id="drone")
                    yield DataPanel(R.space_panel, "fetching space weather…", "Space Weather", id="space")
        yield Footer()

    def on_mount(self) -> None:
        self._apply_size_classes(self.size.width)
        self.update_topbar()
        self.set_interval(15, self._tick)
        self.set_interval(WEATHER_EVERY, lambda: self.load_weather())
        self.set_interval(ALERTS_EVERY, lambda: self.load_alerts())
        self.set_interval(SPACE_EVERY, lambda: self.load_space())
        self.set_interval(METAR_EVERY, self._metar_due)
        self.set_interval(TAF_EVERY, lambda: self.load_taf())
        self.set_interval(HAZARDS_EVERY, self._refresh_hazards)
        self.set_interval(WINDS_EVERY, lambda: self.load_winds())
        self.set_interval(AFD_EVERY, lambda: self.load_discussion())
        self._schedule_visitor()
        self.startup()

    def on_resize(self, event) -> None:
        self._apply_size_classes(event.size.width)

    def _apply_size_classes(self, width: int) -> None:
        self.screen.set_class(width < WIDE, "-narrow")
        self.screen.set_class(width < TINY, "-tiny")

    # ── location ─────────────────────────────────────────────────────────────
    @work(exclusive=True, group="startup")
    async def startup(self) -> None:
        loc: Location | None = None
        try:
            if self.location_query:
                results = await geo.search(self.http, self.location_query)
                if results:
                    loc = results[0]
                else:
                    self.notify(f"No place found for “{self.location_query}”", severity="warning")
            if loc is None:
                loc = self.config.location
            if loc is None:
                loc = await geo.autolocate(self.http)
        except FetchError as e:
            self.errors["location"] = str(e)
            self.update_topbar()
            self.notify(f"Could not determine location: {e}. Press l to search.", severity="error", timeout=10)
            self.action_location()
            return
        self.set_location(loc)

    def set_location(self, loc: Location) -> None:
        changed = not loc.same_place(self.location)
        self.location = loc
        self.errors.pop("location", None)
        self.config.location = loc
        self._save()
        if changed:
            self.weather = None
            self.alerts_result = None
            self.discussion, self.discussion_error = None, ""
            self.drone_ticks.clear()
            for pid in ("current", "hourly", "forecast", "alerts"):
                self.query_one(f"#{pid}", DataPanel).set_data(None)
            self._reset_aviation()
        self.update_topbar()
        self.load_weather()
        self.load_alerts()
        self.load_space()
        if changed or self.aviation.choice is None:
            self.resolve_station()
        else:
            self.refresh_aviation()
        self.load_discussion()

    def _save(self) -> None:
        if self.save_config:
            try:
                self.config.save()
            except OSError as e:
                self.notify(f"Could not save settings: {e}", severity="warning")

    # ── loaders ──────────────────────────────────────────────────────────────
    @work(exclusive=True, group="weather")
    async def load_weather(self) -> None:
        loc = self.location
        if loc is None:
            return
        try:
            w = await openmeteo.fetch_weather(self.http, loc, self.config.units)
        except (FetchError, KeyError, ValueError) as e:
            self.errors["weather"] = str(e)
            self.update_topbar()
            if self.weather is None:
                self.query_one("#current", DataPanel).update(Text(f"weather unavailable: {e}", style="#ff9f43"))
            return
        if not loc.same_place(self.location):
            return
        self.errors.pop("weather", None)
        self.weather = w
        for pid in ("current", "hourly", "forecast"):
            self.query_one(f"#{pid}", DataPanel).set_data(w)
        self._drone_changed()
        c = w.current
        self.query_one(ScenePanel).set_state(
            SceneState.from_weather(c.code, c.is_day, c.wind_speed, c.wind_dir, c.cloud_cover, w.units),
            wmo.condition(c.code).label,
        )
        self.update_topbar()

    @work(exclusive=True, group="alerts")
    async def load_alerts(self) -> None:
        loc = self.location
        if loc is None:
            return
        res = await nws.fetch_alerts(self.http, loc)
        if not loc.same_place(self.location):
            return
        self.alerts_result = res
        self.refresh_alerts()

    @work(exclusive=True, group="space")
    async def load_space(self) -> None:
        loc = self.location
        lat, lon = (loc.lat, loc.lon) if loc else (0.0, 0.0)
        try:
            sw = await swpc.fetch_space(self.http, lat, lon)
        except FetchError as e:
            self.errors["space"] = str(e)
            if self.space is None:
                self.query_one("#space", DataPanel).update(Text(f"space weather unavailable: {e}", style="#ff9f43"))
            return
        self.errors.pop("space", None)
        self.space = sw
        self.query_one("#space", DataPanel).set_data(sw)
        self.refresh_alerts()  # also re-evaluates the drone checklist

    # ── aviation ─────────────────────────────────────────────────────────────
    def _reset_aviation(self) -> None:
        self.aviation = AviationResult()
        self.runway, self._runway_auto = ("", None), True
        self._aviation_changed()

    @work(exclusive=True, group="av-station")
    async def resolve_station(self) -> None:
        """Pick the aviation station for this location (explicit override or nearest METAR site).

        Runs on location or station change only, never on the refresh cycle."""
        loc = self.location
        if loc is None:
            return
        explicit = self.config.aviation_station or None
        try:
            choice = await awc.choose_station(self.http, loc.lat, loc.lon, explicit)
        except AV_ERRORS as e:
            choice = StationChoice(None, "explicit" if explicit else "auto", error=str(e) or e.__class__.__name__,
                                   retryable=True)
        if not loc.same_place(self.location) or explicit != (self.config.aviation_station or None):
            return
        self.aviation = AviationResult(choice=choice, notam_status=faa_notam.status())
        self.runway, self._runway_auto = ("", None), True
        self._aviation_changed()
        self._load_aviation_products()

    def refresh_aviation(self, announce: bool = False) -> None:
        if announce:
            self.notify("Refreshing aviation data…", timeout=2)
        choice = self.aviation.choice
        if choice is None or (choice.station is None and choice.retryable):
            self.resolve_station()  # discovery failed on the network earlier; try again
            return
        self._load_aviation_products()

    def _load_aviation_products(self) -> None:
        self.load_metar()
        self.load_taf()
        self._refresh_hazards()
        self.load_winds()

    def _metar_due(self) -> None:
        choice = self.aviation.choice
        if choice is not None and choice.station is None and choice.retryable:
            self.resolve_station()  # came up offline: try discovery again
        else:
            self.load_metar()

    def _refresh_hazards(self) -> None:
        self.load_pireps()
        self.load_hazards()

    def _aviation_ref(self) -> tuple[float, float] | None:
        st = self.aviation.station
        if st:
            return st.lat, st.lon
        return (self.location.lat, self.location.lon) if self.location else None

    async def _fetch_into(self, av: AviationResult, attr: str, fetch) -> bool:
        """Run one product fetch into its own feed. Failures stay local to that product."""
        feed = getattr(av, attr)
        try:
            data, stale = await fetch()
        except AV_ERRORS as e:
            if av is self.aviation:
                feed.fail(str(e) or e.__class__.__name__)
                self._aviation_changed()
            return False
        if av is not self.aviation:
            return False
        feed.ok(data, stale, A.utcnow())
        self._aviation_changed()
        return True

    @work(exclusive=True, group="av-metar")
    async def load_metar(self) -> None:
        av, st = self.aviation, self.aviation.station
        if st is None:
            return
        if await self._fetch_into(av, "metars", lambda: awc.fetch_metars(self.http, st.icao)):
            if self._runway_auto:
                self._auto_runway()

    @work(exclusive=True, group="av-taf")
    async def load_taf(self) -> None:
        av, st = self.aviation, self.aviation.station
        if st is None:
            return
        await self._fetch_into(av, "taf", lambda: awc.fetch_taf(self.http, st.icao))

    @work(exclusive=True, group="av-pirep")
    async def load_pireps(self) -> None:
        av, st = self.aviation, self.aviation.station
        if st is None:
            return
        await self._fetch_into(av, "pireps", lambda: awc.fetch_pireps(self.http, st))

    @work(exclusive=True, group="av-hazards")
    async def load_hazards(self) -> None:
        av, ref = self.aviation, self._aviation_ref()
        if ref is None or av.choice is None:
            return
        await self._fetch_into(av, "hazards", lambda: awc.fetch_hazards(self.http, *ref))

    @work(exclusive=True, group="av-winds")
    async def load_winds(self) -> None:
        av, ref = self.aviation, self._aviation_ref()
        if ref is None or av.choice is None:
            return
        await self._fetch_into(av, "winds", lambda: awc.fetch_winds(self.http, *ref))

    def _aviation_changed(self) -> None:
        try:
            self.query_one("#aviation", DataPanel).set_data(self.aviation)
        except NoMatches:  # a fetch finished while the app was shutting down
            return
        if isinstance(self.screen, AviationScreen):
            self.screen.redraw(keep_list=True)
        self._drone_changed()

    def _render_aviation(self, av: AviationResult, width: int):
        return RA.aviation_summary(av, width, self.config.units)

    def set_aviation_station(self, ident: str) -> None:
        """Explicit station override; blank or "auto" returns to the nearest METAR station."""
        ident = ident.strip().upper()
        self.config.aviation_station = "" if ident in ("", "AUTO") else ident
        self._save()
        self._reset_aviation()
        self.resolve_station()

    # runway selection for the crosswind calculator (session only)
    def runway_ends(self) -> list[tuple[str, int]]:
        st = self.aviation.station
        return [end for r in (st.runways if st else []) for end in r.ends]

    def runway_choice(self) -> tuple[str, int | None]:
        return self.runway

    def set_runway(self, label: str | None, heading: int | None) -> None:
        self.runway = (label or "", heading)
        self._runway_auto = heading is None

    def cycle_runway(self, step: int) -> bool:
        ends = self.runway_ends()
        if not ends:
            return False
        idx = next((i for i, (lbl, _) in enumerate(ends) if lbl == self.runway[0]), -1)
        lbl, hdg = ends[(idx + step) % len(ends)] if idx >= 0 else ends[0]
        self.set_runway(lbl, hdg)
        return True

    def _auto_runway(self) -> None:
        """Preselect the published runway end with the most headwind (AWC/FAA runway data only)."""
        ends, m, st = self.runway_ends(), self.aviation.metar, self.aviation.station
        if not ends:
            return
        if m is None or m.wind_dir is None or not m.wind_speed_kt:
            self.runway = ends[0]
            return
        wdir = A.true_to_magnetic(m.wind_dir, st.magdec if st else None)
        self.runway = max(ends, key=lambda e: A.wind_components(e[1], wdir, m.wind_speed_kt)[0])

    # ── drone go/no-go ───────────────────────────────────────────────────────
    def _drone_changed(self) -> None:
        """Re-score the checklist from whatever data is loaded and redraw the panel and screen."""
        loc = self.location
        if loc is None:
            return
        res = self.alerts_result
        if self.demo_alert:
            res = AlertsResult(demo_alerts() + (res.alerts if res else []))
        self.drone = D.evaluate(loc, self.weather, res, self.aviation, self.space, self.config.drone,
                                self.drone_ticks, A.utcnow())
        try:
            self.query_one("#drone", DataPanel).set_data(self.drone)
        except NoMatches:  # panel turned off in config, or shutting down
            pass
        if isinstance(self.screen, DroneScreen):
            self.screen.redraw(keep_list=True)

    def toggle_drone_tick(self, item_id: str) -> None:
        self.drone_ticks ^= {item_id}
        self._drone_changed()

    def clear_drone_ticks(self) -> None:
        self.drone_ticks.clear()
        self._drone_changed()

    def set_drone_plan(self, window_hours: int | None = None, altitude_ft: int | None = None) -> None:
        lim = self.config.drone
        if window_hours is not None:
            lim.window_hours = window_hours
        if altitude_ft is not None:
            lim.altitude_ft = altitude_ft
        lim.clamped()
        self._save()
        self._drone_changed()

    # ── forecast discussion ──────────────────────────────────────────────────
    @work(exclusive=True, group="afd")
    async def load_discussion(self) -> None:
        loc = self.location
        if loc is None:
            return
        try:
            if self.office is None or not loc.same_place(self._office_loc):
                self.office = await nws.fetch_office(self.http, loc)
                self._office_loc = loc
            if self.office is None:
                disc, err = None, "not available outside NWS coverage"
            else:
                disc, err = await nws.fetch_discussion(self.http, self.office, self.discussion), ""
        except AV_ERRORS as e:
            disc, err = self.discussion, str(e) or e.__class__.__name__
            if disc is not None:
                disc.stale = True
        if not loc.same_place(self.location):
            return
        self.discussion, self.discussion_error = disc, err
        self.query_one("#alerts", DataPanel).redraw()
        if isinstance(self.screen, DiscussionScreen):
            self.screen.set_discussion(disc, err)

    def _render_alerts(self, res: AlertsResult, width: int):
        afd = RA.discussion_indicator(self.discussion, "")
        return Group(R.alerts_summary(res), afd) if afd.plain else R.alerts_summary(res)

    def _tick(self) -> None:
        """Keep ages honest: redraw everything that shows "N minutes ago"."""
        self.update_topbar()
        self.query_one("#aviation", DataPanel).redraw()
        self.query_one("#alerts", DataPanel).redraw()
        self._drone_changed()  # twilight and the window move with the clock

    # ── alerts ───────────────────────────────────────────────────────────────
    def refresh_alerts(self) -> None:
        res = self.alerts_result
        nws_alerts = list(res.alerts) if res else []
        extra = demo_alerts() if self.demo_alert else []
        space_alerts = swpc_as_alerts(self.space) if self.space else []
        all_alerts = nws.sort_alerts(extra + nws_alerts + space_alerts)
        self.all_alerts = all_alerts

        panel_res = AlertsResult(extra + nws_alerts, supported=True) if extra else res
        self.query_one("#alerts", DataPanel).set_data(panel_res)
        alerts_panel = self.query_one("#alerts", DataPanel)
        top = (extra + nws_alerts)[:1]
        alerts_panel.set_class(bool(top) and top[0].rank >= 3, "-hot")
        self.query_one(AlertBanner).set_alerts(all_alerts)

        self._drone_changed()

        new = [a for a in all_alerts if a.id not in self.seen_alert_ids]
        if new:
            self.seen_alert_ids.update(a.id for a in new)
            self.announce(new)

    def announce(self, alerts: list[Alert]) -> None:
        if self.config.bell:
            self.bell()
        for a in alerts[:3]:
            sev = "error" if a.rank >= 3 else "warning"
            self.notify(f"{escape(a.headline or a.event)}\n[dim]press w for details[/dim]", title=f"⚠ {escape(a.event)}", severity=sev, timeout=12)
        if len(alerts) > 3:
            self.notify(f"+{len(alerts) - 3} more alerts — press w", severity="warning")
        if self.config.desktop_notify and shutil.which("notify-send"):
            for a in alerts[:3]:
                self._desktop_notify(a)

    @work(group="notify")
    async def _desktop_notify(self, a: Alert) -> None:
        urgency = "critical" if a.rank >= 3 else "normal"
        where = f" – {self.location.label}" if self.location else ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", "-a", "termwx", "-u", urgency, f"⚠ {a.event}{where}", a.headline or a.event,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except OSError:
            pass

    # ── sky visitor ──────────────────────────────────────────────────────────
    def _schedule_visitor(self) -> None:
        if self.config.visitor:
            self.set_timer(random.uniform(VISITOR_MIN, VISITOR_MAX), self._visitor_due)

    def _visitor_due(self) -> None:
        self.send_visitor()
        self._schedule_visitor()

    @work(exclusive=True, group="visitor")
    async def send_visitor(self) -> None:
        scene = self.query_one(ScenePanel)
        if not scene.can_host_visitor:
            return
        quip = await quips.fetch_quip(self.http)
        scene.launch_visitor(quip)

    @on(AlertBanner.Pressed)
    def _banner_pressed(self) -> None:
        self.action_alerts()

    # ── top bar ──────────────────────────────────────────────────────────────
    def update_topbar(self) -> None:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(" termwx ", style="bold #0d1117 on #58a6ff")
        loc = self.location
        if loc:
            if self.config.is_favorite(loc):
                t.append(" ★", style="#ffd23f")
            t.append(f" {loc.label}", style="bold #e6edf3")
            if self.size.width >= 90:
                t.append(f"  {loc.coords}", style=R.DIM)
            now = R.local_now(loc.timezone)
            t.append(f"  │  {now.strftime('%a %b %-d')} {R.hhmm(now)} {now.strftime('%Z')}", style="#c9d1d9")
        else:
            t.append("  locating…", style=R.DIM)
        t.append(f"  │  {'°F' if self.config.units == U.IMPERIAL else '°C'}", style="#c9d1d9")
        if self.weather:
            stale = self.weather.stale or "weather" in self.errors
            t.append(f"  │  ⟳ {R.ago(self.weather.fetched_at)}", style="#ff9f43" if stale else R.DIM)
            if stale:
                t.append("  ⚠ offline – showing cached data", style="bold #ff9f43")
        elif "weather" in self.errors:
            t.append(f"  │  ⚠ {self.errors['weather']}", style="#ff9f43")
        if "location" in self.errors:
            t.append(f"  │  ⚠ location: {self.errors['location']}", style="#ff9f43")
        self.query_one("#topbar", Static).update(t)

    # ── actions ──────────────────────────────────────────────────────────────
    def action_location(self) -> None:
        def done(result: Location | str | None) -> None:
            if result == AUTO:
                self.action_autolocate()
            elif isinstance(result, Location):
                self.set_location(result)

        self.push_screen(LocationScreen(self.http, self.config.favorites, self.location), done)

    @work(exclusive=True, group="startup")
    async def action_autolocate(self) -> None:
        self.notify("Detecting location…", timeout=3)
        try:
            loc = await geo.autolocate(self.http)
        except FetchError as e:
            self.notify(f"Auto-detect failed: {e}", severity="error")
            return
        self.set_location(loc)

    def action_units(self) -> None:
        self.config.units = U.METRIC if self.config.units == U.IMPERIAL else U.IMPERIAL
        self._save()
        self.update_topbar()
        self.load_weather()

    def action_refresh(self) -> None:
        self.notify("Refreshing…", timeout=2)
        self.load_weather()
        self.load_alerts()
        self.load_space()
        self.refresh_aviation()
        self.load_discussion()

    def action_aviation(self) -> None:
        self.push_screen(AviationScreen())

    def action_discussion(self) -> None:
        err = self.discussion_error or ("loading…" if self.location else "")
        self.push_screen(DiscussionScreen(self.discussion, "" if self.discussion else err))

    def action_drone(self) -> None:
        self.push_screen(DroneScreen())

    def action_alerts(self) -> None:
        if not self.all_alerts:
            res = self.alerts_result
            msg = "NWS alerts cover US locations only" if res and not res.supported else "No active alerts"
            self.notify(msg)
            return
        self.push_screen(AlertsScreen(self.all_alerts))

    def action_space(self) -> None:
        self.push_screen(SpaceScreen(self.space))

    def action_favorite(self) -> None:
        if not self.location:
            return
        added = self.config.toggle_favorite(self.location)
        self._save()
        self.notify(f"{'★ Saved' if added else 'Removed'} {self.location.label}", timeout=3)
        self.update_topbar()

    def action_next_favorite(self) -> None:
        favs = self.config.favorites
        if not favs:
            self.notify("No favorites yet — press s to save this location", timeout=4)
            return
        idx = next((i for i, f in enumerate(favs) if f.same_place(self.location)), -1)
        self.set_location(favs[(idx + 1) % len(favs)])

    def action_toggle_animation(self) -> None:
        self.config.animate = not self.config.animate
        self.query_one(ScenePanel).set_animate(self.config.animate)
        self._save()

    def action_visitor(self) -> None:
        if not self.query_one(ScenePanel).can_host_visitor:
            self.notify("The sky needs to be visible and animated (p), with no visitor already out", timeout=4)
            return
        self.send_visitor()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def action_quit(self) -> None:
        await self.http.aclose()
        self.exit()
