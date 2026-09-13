"""Modal and full screens: location picker, alert details, space weather, help."""

from __future__ import annotations

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import render as R
from .api import geo
from .api.http import FetchError, Http
from .models import Alert, Location, SpaceWeather

AUTO = "auto"


class LocationScreen(ModalScreen[Location | str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, http: Http, favorites: list[Location], current: Location | None):
        super().__init__()
        self.http = http
        self.favorites = favorites
        self.current = current
        self.results: list[Location] = []
        self._timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="location-box"):
            yield Static("[b]Change location[/b]  [dim]city, “City, ST”, ZIP, or lat,lon[/dim]", id="location-title")
            yield Input(placeholder="e.g. Denver, CO  ·  28202  ·  51.5,-0.12", id="location-input")
            yield OptionList(id="location-options")
            yield Static("[dim]↑↓ choose · enter select · esc cancel[/dim]", id="location-hint")

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one(Input).focus()

    def _rebuild(self, status: str = "") -> None:
        ol = self.query_one(OptionList)
        ol.clear_options()
        if self.results:
            for i, loc in enumerate(self.results):
                label = Text()
                label.append("⌕ ", style=R.ACCENT)
                label.append(loc.label, style="bold")
                if loc.country and loc.country_code != "US":
                    label.append(f"  {loc.country}", style=R.DIM)
                label.append(f"  {loc.coords}", style=R.DIM)
                ol.add_option(Option(label, id=f"res:{i}"))
        elif status:
            ol.add_option(Option(Text(status, style=R.DIM), id="status", disabled=True))
        ol.add_option(Option(Text("⌖ Detect my location (IP)", style="#9be15d"), id=AUTO))
        for i, fav in enumerate(self.favorites):
            label = Text()
            label.append("★ ", style="#ffd23f")
            label.append(fav.label)
            if self.current and fav.same_place(self.current):
                label.append("  (current)", style=R.DIM)
            ol.add_option(Option(label, id=f"fav:{i}"))
        if ol.option_count:
            first = next((i for i in range(ol.option_count) if not ol.get_option_at_index(i).disabled), 0)
            ol.highlighted = first

    @on(Input.Changed)
    def _changed(self, event: Input.Changed) -> None:
        if self._timer:
            self._timer.stop()
        text = event.value.strip()
        if len(text) < 2:
            self.results = []
            self._rebuild()
            return
        self._timer = self.set_timer(0.35, lambda: self._search(text))

    @work(exclusive=True, group="geocode")
    async def _search(self, text: str) -> None:
        self._rebuild("searching…")
        try:
            self.results = await geo.search(self.http, text)
        except FetchError as e:
            self.results = []
            self._rebuild(f"search failed: {e}")
            return
        self._rebuild("" if self.results else f"no matches for “{text}”")

    @on(Input.Submitted)
    async def _submitted(self, event: Input.Submitted) -> None:
        coords = geo.parse_coords(event.value)
        if coords:
            self.dismiss(coords)
            return
        ol = self.query_one(OptionList)
        if self.results and ol.highlighted is not None:
            self._choose(ol.get_option_at_index(ol.highlighted).id)
        elif event.value.strip():
            if self._timer:
                self._timer.stop()
            worker = self._search(event.value.strip())
            await worker.wait()
            if self.results:
                self.dismiss(self.results[0])

    def on_key(self, event) -> None:
        # Let arrow keys move through results while typing.
        if event.key in ("down", "up") and self.focused is self.query_one(Input):
            ol = self.query_one(OptionList)
            ol.action_cursor_down() if event.key == "down" else ol.action_cursor_up()
            event.stop()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self._choose(event.option.id)

    def _choose(self, oid: str | None) -> None:
        if oid == AUTO:
            self.dismiss(AUTO)
        elif oid and oid.startswith("res:"):
            self.dismiss(self.results[int(oid[4:])])
        elif oid and oid.startswith("fav:"):
            self.dismiss(self.favorites[int(oid[4:])])

    def action_cancel(self) -> None:
        self.dismiss(None)


def alert_detail(a: Alert) -> Group:
    color = R.alert_color(a)
    head = Text()
    head.append(f" {a.severity.upper()} ", style=f"bold #0d1117 on {color}")
    head.append(f"  {a.event}", style=f"bold {color}")
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style=R.LABEL)
    meta.add_column()
    if a.headline:
        meta.add_row("Headline", Text(a.headline, style="bold"))
    meta.add_row("When", R.when_text(a) or "--")
    meta.add_row("Urgency", f"{a.urgency or '--'} · certainty {a.certainty or '--'}")
    if a.sender:
        meta.add_row("Issued by", a.sender)
    if a.area:
        meta.add_row("Area", Text(a.area, style=R.DIM))
    parts = [head, Text(), meta, Text()]
    if a.description:
        parts += [Text("Details", style=f"bold {R.ACCENT}"), Text(a.description), Text()]
    if a.instruction:
        parts += [Text("What to do", style="bold #ffd23f"), Text(a.instruction)]
    return Group(*parts)


class AlertsScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,w,q", "close", "Close")]

    def __init__(self, alerts: list[Alert]):
        super().__init__()
        self.alerts = alerts

    def compose(self) -> ComposeResult:
        with Vertical(id="alerts-box"):
            yield Static(f"[b]Active alerts[/b] [dim]({len(self.alerts)})  ↑↓ select · esc close[/dim]", id="alerts-title")
            with Horizontal():
                options = []
                for a in self.alerts:
                    t = Text()
                    t.append("■ ", style=R.alert_color(a))
                    t.append(a.event, style="bold")
                    t.append(f"\n  {a.source} · {a.severity}", style=R.DIM)
                    options.append(Option(t, id=a.id))
                yield OptionList(*options, id="alerts-list")
                with VerticalScroll(id="alert-detail-scroll"):
                    yield Static(id="alert-detail")

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        if self.alerts:
            ol.highlighted = 0
            self._show(0)
        else:
            self.query_one("#alert-detail", Static).update(Text("No active alerts.", style=R.DIM))
        ol.focus()

    @on(OptionList.OptionHighlighted)
    def _hl(self, event: OptionList.OptionHighlighted) -> None:
        self._show(event.option_index)

    def _show(self, i: int) -> None:
        if 0 <= i < len(self.alerts):
            self.query_one("#alert-detail", Static).update(alert_detail(self.alerts[i]))
            self.query_one("#alert-detail-scroll").scroll_home(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)


class SpaceScreen(Screen[None]):
    BINDINGS = [Binding("escape,x,q", "close", "Back")]

    def __init__(self, sw: SpaceWeather | None):
        super().__init__()
        self.sw = sw

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="space-scroll"):
            yield Static(id="space-kp", classes="panel")
            with Horizontal(id="space-row"):
                yield Static(id="space-summary", classes="panel")
                yield Static(id="space-wind", classes="panel")
            with Horizontal(id="space-messages"):
                yield OptionList(id="swpc-list", classes="panel")
                with VerticalScroll(id="swpc-detail-scroll", classes="panel"):
                    yield Static(id="swpc-detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#space-kp").border_title = "Planetary Kp · observed █ / forecast ▓ · 3-hour bins"
        self.query_one("#space-summary").border_title = "Now"
        self.query_one("#space-wind").border_title = "Solar wind · last 3h"
        self.query_one("#swpc-list").border_title = "SWPC alerts, watches & warnings"
        self.query_one("#swpc-detail-scroll").border_title = "Message"
        ol = self.query_one("#swpc-list", OptionList)
        if self.sw:
            for m in self.sw.messages[:40]:
                t = Text(no_wrap=True, overflow="ellipsis")
                t.append(m.issued.astimezone().strftime("%m/%d %H:%M "), style=R.DIM)
                t.append(m.title, style=R.message_color(m))
                ol.add_option(Option(t))
            if self.sw.messages:
                ol.highlighted = 0
        self.call_after_refresh(self.redraw)
        ol.focus()

    def on_resize(self) -> None:
        self.redraw()

    def redraw(self) -> None:
        sw = self.sw
        if sw is None:
            self.query_one("#space-kp", Static).update(Text("space weather not loaded yet", style=R.DIM))
            return
        kp_w = (self.query_one("#space-kp").content_size.width or 80) - 3
        col = 2 if kp_w >= 100 else 1
        pts = sw.kp[-(kp_w // col) :]
        chart = R.kp_chart(pts, rows=6, col_width=col)
        lines = []
        for i, line in enumerate(chart):
            prefix = Text("   ")
            if i in (0, 2, 4):
                prefix = Text(f"{9 - i * 1.5:>2.0f} ", style=R.LABEL)
            lines.append(Text.assemble(prefix, line))
        legend = Text()
        for lo, name in ([(0, "quiet"), (4, "active"), (5, "G1"), (6, "G2"), (7, "G3"), (8, "G4+")]):
            legend.append("█", style=R.kp_color(lo))
            legend.append(f" {name}  ", style=R.DIM)
        self.query_one("#space-kp", Static).update(Group(*lines, legend))
        self.query_one("#space-summary", Static).update(R.space_panel(sw, 50))

        ww = max(20, (self.query_one("#space-wind").content_size.width or 50) - 12)
        wind = []
        if sw.speed_history:
            hist = _resample(sw.speed_history, ww)
            wind.append(Text("Speed km/s", style=R.LABEL))
            wind.append(Text(f"{min(hist):>5.0f} ", style=R.DIM) + Text(R.sparkline(hist), style="#f0883e") + Text(f" {max(hist):.0f}", style=R.DIM))
        if sw.bz_history:
            hist = _resample(sw.bz_history, ww)
            m = max(10.0, max(abs(v) for v in hist))
            up, down = Text(), Text()
            for v in hist:
                if v >= 0:
                    up.append(R.BLOCKS[max(1, round(v / m * 8))] if v > 0.2 else " ", style="#3fb950")
                    down.append(" ")
                else:
                    up.append(" ")
                    lvl = max(1, round(-v / m * 8))
                    down.append("█" if lvl >= 8 else "▔" if lvl <= 2 else "▀", style="#f85149")
            wind += [
                Text(),
                Text("Bz nT (north ▲ / south ▼, south drives storms)", style=R.LABEL),
                Text(f"{m:>+5.0f} ", style=R.DIM) + up,
                Text(f"{-m:>+5.0f} ", style=R.DIM) + down,
            ]
        if not wind:
            wind = [Text("no real-time solar wind data", style=R.DIM)]
        self.query_one("#space-wind", Static).update(Group(*wind))
        ol = self.query_one("#swpc-list", OptionList)
        self._show_message(ol.highlighted or 0)

    @on(OptionList.OptionHighlighted, "#swpc-list")
    def _hl(self, event: OptionList.OptionHighlighted) -> None:
        self._show_message(event.option_index)

    def _show_message(self, i: int) -> None:
        if self.sw and 0 <= i < len(self.sw.messages):
            self.query_one("#swpc-detail", Static).update(Text(self.sw.messages[i].body))

    def action_close(self) -> None:
        self.dismiss(None)


def _resample(values: list[float], n: int) -> list[float]:
    if len(values) <= n:
        return values
    step = len(values) / n
    return [values[int(i * step)] for i in range(n)]


HELP = """\
[b #58a6ff]termwx[/] — ultimate terminal weather

[b]l[/]  change location (search city, ZIP, or lat,lon)
[b]a[/]  auto-detect location from IP
[b]s[/]  save / unsave current location as favorite
[b]f[/]  jump to next favorite
[b]u[/]  toggle °F / °C
[b]w[/]  active alert details (also click the banner)
[b]x[/]  space weather dashboard
[b]r[/]  refresh everything now
[b]p[/]  pause / resume animation
[b]q[/]  quit

[dim]Data: Open-Meteo (forecast, AQI, geocoding) · NWS api.weather.gov (alerts)
NOAA SWPC (space weather) · ipinfo.io (auto-locate)

Refresh: weather 10m · alerts 2m · space 5m[/dim]

[dim]press any key to close[/dim]"""


class HelpScreen(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        yield Static(HELP, id="help-box")

    def on_key(self, event) -> None:
        event.stop()
        self.dismiss(None)

    def on_click(self) -> None:
        self.dismiss(None)
