"""The termwx Textual application."""

from __future__ import annotations

import asyncio
import shutil

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Static

from . import render as R, units as U, wmo
from .api import geo, nws, openmeteo, swpc
from .api.http import FetchError, Http
from .config import Config
from .demo import demo_alerts
from .models import Alert, AlertsResult, Location, SpaceWeather, Weather
from .scene import SceneState
from .screens import AUTO, AlertsScreen, HelpScreen, LocationScreen, SpaceScreen
from .widgets import AlertBanner, DataPanel, ScenePanel

WEATHER_EVERY = 600
ALERTS_EVERY = 120
SPACE_EVERY = 300

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
        Binding("s", "favorite", "Save ★"),
        Binding("f", "next_favorite", "Next ★"),
        Binding("a", "autolocate", "Auto-locate", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "toggle_animation", "Pause anim", show=False),
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
                    yield DataPanel(lambda d, w: R.alerts_summary(d), "checking alerts…", "NWS Alerts", id="alerts")
                    yield DataPanel(R.space_panel, "fetching space weather…", "Space Weather", id="space")
        yield Footer()

    def on_mount(self) -> None:
        self._apply_size_classes(self.size.width)
        self.update_topbar()
        self.set_interval(15, self.update_topbar)
        self.set_interval(WEATHER_EVERY, lambda: self.load_weather())
        self.set_interval(ALERTS_EVERY, lambda: self.load_alerts())
        self.set_interval(SPACE_EVERY, lambda: self.load_space())
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
            for pid in ("current", "hourly", "forecast", "alerts"):
                self.query_one(f"#{pid}", DataPanel).set_data(None)
        self.update_topbar()
        self.load_weather()
        self.load_alerts()
        self.load_space()

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
        self.refresh_alerts()

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

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def action_quit(self) -> None:
        await self.http.aclose()
        self.exit()
