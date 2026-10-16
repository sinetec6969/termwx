"""Aviation screen (v), forecast discussion screen (d) and the small prompt
used to pick an airport or runway heading.

Screens read live state from the app (`app.aviation`, `app.discussion`) and
are redrawn by the app whenever a product refreshes.
"""

from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import aviation as A, render_aviation as RA
from .api import faa_notam
from .aviation_models import AviationResult
from .models import ForecastDiscussion
from .render import ACCENT, DIM, LABEL

NARROW = 110


class PromptScreen(ModalScreen[str | None]):
    """One-line text prompt. Dismisses with the text, or None on escape."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", hint: str = "", value: str = ""):
        super().__init__()
        self.title_text, self.placeholder, self.hint, self.value = title, placeholder, hint, value

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Static(self.title_text, id="prompt-title")
            yield Input(value=self.value, placeholder=self.placeholder, id="prompt-input")
            yield Static(f"[dim]{self.hint}[/dim]" if self.hint else "", id="prompt-hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class AviationScreen(Screen[None]):
    BINDINGS = [
        Binding("escape,v,q", "close", "Back"),
        Binding("c", "change_airport", "Airport"),
        Binding("h", "runway_heading", "Rwy heading"),
        Binding("left_square_bracket", "runway(-1)", "◀ Rwy", show=False),
        Binding("right_square_bracket", "runway(1)", "Rwy ▶", show=False),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="av-header")
        with VerticalScroll(id="av-scroll"):
            with Horizontal(id="av-row1", classes="av-row"):
                yield Static(id="av-metar", classes="panel")
                yield Static(id="av-tools", classes="panel")
            yield Static(id="av-taf", classes="panel")
            with Horizontal(id="av-row2", classes="av-row"):
                yield OptionList(id="av-hazards", classes="panel")
                with VerticalScroll(id="av-detail-scroll", classes="panel"):
                    yield Static(id="av-detail")
            with Horizontal(id="av-row3", classes="av-row"):
                yield Static(id="av-winds", classes="panel")
                yield Static(id="av-notes", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        titles = {
            "#av-metar": "METAR",
            "#av-tools": "Field tools",
            "#av-taf": "TAF timeline",
            "#av-hazards": "Hazards & PIREPs",
            "#av-detail-scroll": "Details",
            "#av-winds": "Winds aloft",
            "#av-notes": "Notes",
        }
        for sel, title in titles.items():
            self.query_one(sel).border_title = title
        self._items: list[tuple[str, object]] = []
        self.set_class(self.size.width < NARROW, "-narrow")
        self.redraw()
        self.query_one("#av-hazards", OptionList).focus()
        self.set_interval(30, self.redraw_ages)

    def on_resize(self, event) -> None:
        self.set_class(event.size.width < NARROW, "-narrow")

    @property
    def av(self) -> AviationResult:
        return getattr(self.app, "aviation", None) or AviationResult()

    @property
    def units(self) -> str:
        return self.app.config.units

    # ── drawing ─────────────────────────────────────────────────────────────
    def redraw_ages(self) -> None:
        self.redraw(keep_list=True)

    def redraw(self, keep_list: bool = False) -> None:
        if not hasattr(self, "_items"):
            return  # data arrived before the screen finished composing; on_mount draws it
        now = A.utcnow()
        av = self.av
        self.query_one("#av-header", Static).update(RA.station_header(av, self.units, now))
        width = self.query_one("#av-metar").content_size.width or 60
        self.query_one("#av-metar", Static).update(RA.metar_panel(av, self.units, width, now))
        label, heading = self.app.runway_choice()
        self.query_one("#av-tools", Static).update(RA.derived_panel(av, label, heading, now))
        st = av.station
        self.query_one("#av-taf", Static).update(RA.taf_panel(av.taf, bool(st and st.has_taf), now))
        self.query_one("#av-winds", Static).update(RA.winds_panel(av.winds, now))
        self.query_one("#av-notes", Static).update(self._notes(av))
        self._rebuild_list(now, keep_list)

    def _notes(self, av: AviationResult) -> Group:
        lines = [
            Text("NOTAMs  ", style=LABEL) + Text(faa_notam.status(), style=DIM),
        ]
        hz = av.hazards.data
        if hz and hz.errors:
            lines.append(Text("Partial advisories: " + "; ".join(hz.errors), style="#ff9f43"))
        if av.pireps.error:
            lines.append(Text(f"PIREPs: {av.pireps.error}", style="#ff9f43"))
        lines += [
            Text(),
            Text(RA.DISCLAIMER, style="bold #c9d1d9"),
            Text("Data: aviationweather.gov (NOAA/NWS Aviation Weather Center).", style=DIM),
            Text(),
            Text("c airport · h runway heading · [ ] cycle runways · r refresh · esc back", style=DIM),
        ]
        return Group(*lines)

    def _rebuild_list(self, now, keep: bool) -> None:
        av = self.av
        ol = self.query_one("#av-hazards", OptionList)
        prev = ol.highlighted
        items: list[tuple[str, object]] = []
        options: list[Option] = []

        def header(text: str, note: str = "") -> None:
            t = Text(text, style=f"bold {ACCENT}")
            if note:
                t.append(f"  {note}", style=DIM)
            options.append(Option(t, disabled=True))

        hazards = A.relevant_hazards(av.hazards.data.items, now) if av.hazards.data else []
        note = "⚠ cached" if av.hazards.stale else ""
        header(f"Advisories within {A.HAZARD_NEAR_NM} NM", note)
        if hazards:
            for h in hazards:
                items.append(("hazard", h))
                options.append(Option(RA.hazard_card(h, now), id=f"i{len(items) - 1}"))
        else:
            msg = "loading…" if not av.hazards.loaded else (av.hazards.error or "none in effect")
            options.append(Option(Text(f"  {msg}", style=DIM), disabled=True))

        pireps = A.recent_pireps(av.pireps.data, now) if av.pireps.data else []
        header(f"PIREPs · {A.PIREP_RADIUS_NM} NM · {A.PIREP_MAX_AGE_H}h", "⚠ cached" if av.pireps.stale else "")
        if pireps:
            for p in pireps:
                items.append(("pirep", p))
                options.append(Option(RA.pirep_line(p, now), id=f"i{len(items) - 1}"))
        else:
            msg = "loading…" if not av.pireps.loaded else (av.pireps.error or "no recent reports")
            options.append(Option(Text(f"  {msg}", style=DIM), disabled=True))

        self._items = items
        ol.clear_options()
        ol.add_options(options)
        selectable = [i for i, o in enumerate(options) if not o.disabled]
        if selectable:
            ol.highlighted = prev if keep and prev in selectable else selectable[0]
            self._show(ol.highlighted)
        else:
            self.query_one("#av-detail", Static).update(Text("Nothing to show.", style=DIM))

    @on(OptionList.OptionHighlighted, "#av-hazards")
    def _hl(self, event: OptionList.OptionHighlighted) -> None:
        self._show(event.option_index)

    def _show(self, index: int | None) -> None:
        ol = self.query_one("#av-hazards", OptionList)
        if index is None:
            return
        oid = ol.get_option_at_index(index).id
        if not oid or not oid.startswith("i"):
            return
        kind, obj = self._items[int(oid[1:])]
        now = A.utcnow()
        detail = RA.hazard_detail(obj, now) if kind == "hazard" else RA.pirep_detail(obj, now)
        self.query_one("#av-detail", Static).update(detail)
        self.query_one("#av-detail-scroll").scroll_home(animate=False)

    # ── actions ─────────────────────────────────────────────────────────────
    def action_close(self) -> None:
        self.dismiss(None)

    def action_refresh(self) -> None:
        self.app.refresh_aviation(announce=True)

    def action_change_airport(self) -> None:
        current = self.app.config.aviation_station or ""

        def done(value: str | None) -> None:
            if value is not None:
                self.app.set_aviation_station(value)

        self.app.push_screen(
            PromptScreen(
                "[b]Aviation station[/b]  [dim]ICAO identifier, e.g. KCLT[/dim]",
                placeholder="ICAO, or blank for the nearest station",
                hint="enter to apply · blank or “auto” = nearest METAR station · esc cancel",
                value=current,
            ),
            done,
        )

    def action_runway_heading(self) -> None:
        def done(value: str | None) -> None:
            if value is None:
                return
            if not value:
                self.app.set_runway(None, None)
                self.redraw(keep_list=True)
                return
            hdg = A.parse_runway_heading(value)
            if hdg is None:
                self.app.notify(f"“{value}” is not a runway (e.g. 18, 36L) or heading (e.g. 175)", severity="warning")
                return
            ident = value.upper().replace("RWY", "").strip()
            self.app.set_runway(ident if len(ident.rstrip("LRC")) <= 2 else "", hdg)
            self.redraw(keep_list=True)

        self.app.push_screen(
            PromptScreen(
                "[b]Runway[/b]  [dim]number (18, 36L) or magnetic heading (175)[/dim]",
                placeholder="e.g. 18",
                hint="enter to apply · blank to clear · esc cancel",
            ),
            done,
        )

    def action_runway(self, step: int) -> None:
        if self.app.cycle_runway(step):
            self.redraw(keep_list=True)
        else:
            self.app.notify("No runway data for this airport — press h to enter a heading", timeout=4)


class DiscussionScreen(Screen[None]):
    """NWS Area Forecast Discussion, verbatim, aviation section first."""

    BINDINGS = [Binding("escape,d,q", "close", "Back")]

    def __init__(self, disc: ForecastDiscussion | None, error: str = ""):
        super().__init__()
        self.disc = disc
        self.error = error

    def compose(self) -> ComposeResult:
        yield Static(id="afd-header")
        with Horizontal(id="afd-body"):
            yield OptionList(id="afd-sections", classes="panel")
            with VerticalScroll(id="afd-text-scroll", classes="panel"):
                yield Static(id="afd-text")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#afd-sections").border_title = "Sections"
        self.query_one("#afd-text-scroll").border_title = "Discussion"
        self.set_discussion(self.disc, self.error)
        self.query_one("#afd-sections", OptionList).focus()

    def set_discussion(self, disc: ForecastDiscussion | None, error: str = "") -> None:
        self.disc, self.error = disc, error
        head = Text(no_wrap=True, overflow="ellipsis")
        ol = self.query_one("#afd-sections", OptionList)
        prev = ol.highlighted
        ol.clear_options()
        if disc is None:
            head.append(" Area Forecast Discussion ", style="bold #0d1117 on #58a6ff")
            head.append(f"  {error or 'not available for this location'}", style=DIM)
            self.query_one("#afd-header", Static).update(head)
            self.query_one("#afd-text", Static).update(
                Text("NWS forecast discussions cover US locations. Nothing to show.", style=DIM)
            )
            return
        now = A.utcnow()
        head.append(f" AFD {disc.office} ", style="bold #0d1117 on #58a6ff")
        head.append(f"  issued {disc.issued.strftime('%a %d %b %H%MZ')}", style="#e6edf3")
        head.append(f"  ({RA.fmt_age(disc.issued, now)} ago)", style=DIM)
        if disc.stale:
            head.append("  ⚠ offline – cached copy", style="bold #ff9f43")
        head.append("   official NWS text · not summarized", style=DIM)
        self.query_one("#afd-header", Static).update(head)
        for s in disc.sections:
            t = Text(no_wrap=True, overflow="ellipsis")
            aviation = s.key.startswith("AVIATION")
            t.append(("✈ " if aviation else "") + s.title, style=f"bold {ACCENT}" if aviation else "#e6edf3")
            ol.add_option(Option(t))
        ol.add_option(Option(Text("Full product text", style=LABEL)))
        ol.highlighted = prev if prev is not None and prev < ol.option_count else 0
        self._show(ol.highlighted)

    @on(OptionList.OptionHighlighted, "#afd-sections")
    def _hl(self, event: OptionList.OptionHighlighted) -> None:
        self._show(event.option_index)

    def _show(self, i: int | None) -> None:
        if self.disc is None or i is None:
            return
        if i < len(self.disc.sections):
            s = self.disc.sections[i]
            body = Group(Text(f".{s.title}...", style=f"bold {ACCENT}"), Text(), Text(s.body or "(empty)"))
        else:
            body = Text(self.disc.text)
        self.query_one("#afd-text", Static).update(body)
        self.query_one("#afd-text-scroll").scroll_home(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)
