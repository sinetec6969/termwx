"""Drone go/no-go screen (g): automatic weather checks and the manual preflight list.

Like the aviation screen, it reads live state from the app (`app.drone`,
`app.drone_ticks`) and is redrawn by the app whenever any product refreshes.
"""

from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from . import drone as D, render_drone as RD
from .api import faa_notam
from .render import ACCENT, DIM
from .screens_aviation import PromptScreen

NARROW = 110


class DroneScreen(Screen[None]):
    BINDINGS = [
        Binding("escape,g,q", "close", "Back"),
        Binding("space", "toggle", "Tick"),
        Binding("plus,equals_sign", "window(1)", "+1h"),
        Binding("minus", "window(-1)", "−1h"),
        Binding("t", "altitude", "Altitude"),
        Binding("x", "clear", "Clear ticks"),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="drone-header")
        with Horizontal(id="drone-body"):
            yield OptionList(id="drone-list", classes="panel")
            with VerticalScroll(id="drone-detail-scroll", classes="panel"):
                yield Static(id="drone-detail")
        yield Static(Text(RD.DISCLAIMER, style=DIM), id="drone-foot")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#drone-list").border_title = "Checklist"
        self.query_one("#drone-detail-scroll").border_title = "Details"
        self._rows: list[tuple[str, object]] = []
        self.set_class(self.size.width < NARROW, "-narrow")
        self.redraw()
        self.query_one("#drone-list", OptionList).focus()

    def on_resize(self, event) -> None:
        self.set_class(event.size.width < NARROW, "-narrow")
        self.redraw(keep_list=True)

    # ── drawing ─────────────────────────────────────────────────────────────
    def redraw(self, keep_list: bool = False) -> None:
        if not hasattr(self, "_rows"):
            return  # data arrived before the screen finished composing; on_mount draws it
        r, loc = self.app.drone, self.app.location
        self.query_one("#drone-header", Static).update(RD.screen_header(r, loc.label if loc else ""))
        ol = self.query_one("#drone-list", OptionList)
        width = ol.content_size.width or 80
        prev = ol.highlighted
        rows: list[tuple[str, object]] = []
        options: list[Option] = []

        def header(text: str, note: str = "") -> None:
            t = Text(text, style=f"bold {ACCENT}")
            if note:
                t.append(f"  {note}", style=DIM)
            options.append(Option(t, disabled=True))

        header("Weather", "scored automatically")
        if r is None:
            options.append(Option(Text("  waiting for weather…", style=DIM), disabled=True))
        else:
            for item in r.items:
                rows.append(("check", item))
                options.append(Option(RD.check_line(item, width), id=f"i{len(rows) - 1}"))
        header("Preflight", "space to tick")
        for m in D.MANUAL_ITEMS:
            rows.append(("manual", m))
            options.append(Option(RD.manual_line(m, m.id in self.app.drone_ticks), id=f"i{len(rows) - 1}"))

        self._rows = rows
        ol.clear_options()
        ol.add_options(options)
        selectable = [i for i, o in enumerate(options) if not o.disabled]
        ol.highlighted = prev if keep_list and prev in selectable else selectable[0]
        self._show(ol.highlighted)

    @on(OptionList.OptionHighlighted, "#drone-list")
    def _hl(self, event: OptionList.OptionHighlighted) -> None:
        self._show(event.option_index)

    @on(OptionList.OptionSelected, "#drone-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.action_toggle()

    def _row(self, index: int | None) -> tuple[str, object] | None:
        if index is None:
            return None
        oid = self.query_one("#drone-list", OptionList).get_option_at_index(index).id
        if not oid or not oid.startswith("i"):
            return None
        return self._rows[int(oid[1:])]

    def _show(self, index: int | None) -> None:
        row = self._row(index)
        if row is None:
            return
        kind, obj = row
        if kind == "check":
            detail = RD.check_detail(obj)
        else:
            note = faa_notam.status() if obj.id == "notams" else ""
            detail = RD.manual_detail(obj, obj.id in self.app.drone_ticks, note)
        self.query_one("#drone-detail", Static).update(Group(detail))
        self.query_one("#drone-detail-scroll").scroll_home(animate=False)

    # ── actions ─────────────────────────────────────────────────────────────
    def action_close(self) -> None:
        self.dismiss(None)

    def action_toggle(self) -> None:
        row = self._row(self.query_one("#drone-list", OptionList).highlighted)
        if row is None or row[0] != "manual":
            return
        self.app.toggle_drone_tick(row[1].id)

    def action_clear(self) -> None:
        self.app.clear_drone_ticks()

    def action_window(self, step: int) -> None:
        limits = self.app.config.drone
        self.app.set_drone_plan(window_hours=limits.window_hours + step)

    def action_altitude(self) -> None:
        def done(value: str | None) -> None:
            if not value:
                return
            try:
                ft = int(value.lower().replace("ft", "").replace(",", "").strip())
            except ValueError:
                self.app.notify(f"“{value}” is not an altitude in feet", severity="warning")
                return
            if not 0 < ft <= D.MAX_ALTITUDE_FT:
                self.app.notify(f"Part 107 altitude is 1–{D.MAX_ALTITUDE_FT} ft AGL", severity="warning")
                return
            self.app.set_drone_plan(altitude_ft=ft)

        self.app.push_screen(
            PromptScreen(
                "[b]Planned altitude[/b]  [dim]feet AGL, up to 400[/dim]",
                placeholder="e.g. 250",
                hint="cloud clearance needs this + 500 ft · enter to apply · esc cancel",
                value=str(self.app.config.drone.altitude_ft),
            ),
            done,
        )

    def action_refresh(self) -> None:
        self.app.action_refresh()
