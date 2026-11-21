"""Satellite tracker screen (o): FM repeater and APRS satellites with live
az/el, Doppler-corrected frequencies, the next 24 h of passes and a sky plot.

Reads live state from `app.sats` (a SatState) and is redrawn by the app when
TLEs or passes change; live numbers tick every second while it's open.
"""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, OptionList, Static
from textual.widgets.option_list import Option

from . import orbit, render_sats as RS
from .render import DIM
from .screens_aviation import PromptScreen

NARROW = 110
LIST_EVERY = 5  # seconds between list re-sorts; the detail pane ticks every second


class SatScreen(Screen[None]):
    BINDINGS = [
        Binding("escape,o,q", "close", "Back"),
        Binding("U", "update_tles", "Update TLEs"),
        Binding("i", "interval", "TLE interval"),
        Binding("e", "min_elevation", "Min elev"),
        Binding("left_square_bracket", "pass_step(-1)", "◂ pass", show=False),
        Binding("right_square_bracket", "pass_step(1)", "pass ▸", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="sat-top"):
            yield Static(id="sat-header")
            yield Button("Update TLEs now", id="sat-update", compact=True)
        with Horizontal(id="sat-body"):
            yield OptionList(id="sat-list", classes="panel")
            with VerticalScroll(id="sat-detail-scroll", classes="panel"):
                yield Static(id="sat-detail")
        yield Static(Text("Doppler: tx = what to transmit, rx = where to listen · [ ] step through passes · "
                          "TLEs: CelesTrak · transmitters: SatNOGS DB", style=DIM), id="sat-foot")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#sat-list").border_title = "Satellites"
        self.query_one("#sat-detail-scroll").border_title = "Tracking"
        self._order: list[int] = []
        self._selected: int | None = None
        self._pass_index = 0
        self._ticks = 0
        self.set_class(self.size.width < NARROW, "-narrow")
        self.redraw()
        self.query_one("#sat-list", OptionList).focus()
        self.set_interval(1, self._tick)

    def on_resize(self, event) -> None:
        self.set_class(event.size.width < NARROW, "-narrow")

    # ── drawing ─────────────────────────────────────────────────────────────
    def _tick(self) -> None:
        self._ticks += 1
        if self._ticks % LIST_EVERY == 0:
            self.redraw()
        else:
            self._draw_header()
            self._draw_detail()

    def _draw_header(self) -> None:
        loc = self.app.location
        self.query_one("#sat-header", Static).update(
            RS.screen_header(self.app.sats, loc.label if loc else "", RS.utcnow()))
        self.query_one("#sat-update", Button).disabled = self.app.sats.updating

    def redraw(self) -> None:
        if not hasattr(self, "_order"):
            return  # data arrived before the screen finished composing; on_mount draws it
        st, now = self.app.sats, RS.utcnow()
        self._draw_header()
        obs = RS.observer(st)
        looks = {s.norad: orbit.look(s, obs, now, subpoint=False) for s in st.catalog.satellites}
        sats = sorted(st.catalog.satellites, key=lambda s: RS.sort_key(s, st, now, looks[s.norad]))
        ol = self.query_one("#sat-list", OptionList)
        order = [s.norad for s in sats]
        prompts = [RS.list_line(s, st, now, looks[s.norad]) for s in sats]
        if order == self._order:
            for i, prompt in enumerate(prompts):
                ol.replace_option_prompt_at_index(i, prompt)
        else:
            ol.clear_options()
            if sats:
                ol.add_options([Option(p, id=f"n{n}") for p, n in zip(prompts, order)])
            else:
                ol.add_option(Option(Text("waiting for satellite data…", style=DIM), disabled=True))
            self._order = order
            if self._selected in order:
                ol.highlighted = order.index(self._selected)
            elif order:
                ol.highlighted = 0
                self._selected = order[0]
        self._draw_detail()

    def _draw_detail(self) -> None:
        st = self.app.sats
        sat = st.sat(self._selected) if self._selected is not None else None
        out = self.query_one("#sat-detail", Static)
        if sat is None:
            out.update(Text("Select a satellite.", style=DIM) if st.catalog.satellites
                       else RS.status_line(st, RS.utcnow()))
            return
        now = RS.utcnow()
        out.update(RS.detail(sat, st, now, orbit.look(sat, RS.observer(st), now), self._pass_index))

    @on(OptionList.OptionHighlighted, "#sat-list")
    def _hl(self, event: OptionList.OptionHighlighted) -> None:
        oid = event.option.id
        if oid and oid.startswith("n") and int(oid[1:]) != self._selected:
            self._selected = int(oid[1:])
            self._pass_index = 0
            self._draw_detail()

    # ── actions ─────────────────────────────────────────────────────────────
    @on(Button.Pressed, "#sat-update")
    def action_update_tles(self) -> None:
        self.app.load_sats(force=True)

    def action_pass_step(self, step: int) -> None:
        self._pass_index = max(0, self._pass_index + step)
        self._draw_detail()

    def action_interval(self) -> None:
        def done(value: str | None) -> None:
            if value:
                try:
                    self.app.set_tle_interval(float(value.rstrip("hH ")))
                except ValueError:
                    self.notify(f"Not a number of hours: {value}", severity="warning")

        st = self.app.sats
        self.app.push_screen(PromptScreen(
            "Reload TLEs every … hours", "3", "2–168 h. CelesTrak updates about every 2 h; "
            "U or the button forces an update any time.", f"{st.refresh_hours:g}"), done)

    def action_min_elevation(self) -> None:
        def done(value: str | None) -> None:
            if value:
                try:
                    self.app.set_sat_min_elevation(float(value.rstrip("°")))
                except ValueError:
                    self.notify(f"Not a number of degrees: {value}", severity="warning")

        self.app.push_screen(PromptScreen(
            "Minimum pass elevation (°)", "10", "Passes peaking lower than this are hidden. 0–80°.",
            f"{self.app.sats.min_el:g}"), done)

    def action_close(self) -> None:
        self.dismiss(None)
