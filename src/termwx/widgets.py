"""Textual widgets wrapping the shared Rich renderers."""

from __future__ import annotations

from typing import Any, Callable

from rich.console import RenderableType
from rich.text import Text
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from . import render as R
from .models import Alert
from .scene import SceneState, render_scene


class DataPanel(Static):
    """A bordered panel that re-renders its data to fit its width."""

    def __init__(self, renderer: Callable[[Any, int], RenderableType], placeholder: str, title: str, **kw):
        super().__init__(Text(placeholder, style=R.DIM), **kw)
        self.renderer = renderer
        self.placeholder = placeholder
        self.data: Any = None
        self.border_title = title
        self.add_class("panel")

    def set_data(self, data: Any) -> None:
        self.data = data
        self.redraw()

    def redraw(self) -> None:
        if self.data is None:
            self.update(Text(self.placeholder, style=R.DIM))
            return
        width = self.content_size.width or 60
        self.update(self.renderer(self.data, width))

    def on_resize(self) -> None:
        self.redraw()


class ScenePanel(Widget):
    """Animated weather scene."""

    tick: reactive[int] = reactive(0, layout=False)

    def __init__(self, animate: bool = True, **kw):
        super().__init__(**kw)
        self.state: SceneState | None = None
        self.animate = animate
        self.border_title = "Sky"
        self.add_class("panel")

    def on_mount(self) -> None:
        self._timer = self.set_interval(1 / 8, self._advance, pause=not self.animate)

    def _advance(self) -> None:
        if self.state is not None and self.display:
            self.tick += 1

    def set_state(self, state: SceneState, label: str) -> None:
        self.state = state
        self.border_title = label
        self.refresh()

    def set_animate(self, on: bool) -> None:
        self.animate = on
        self._timer.resume() if on else self._timer.pause()

    def render(self) -> RenderableType:
        if self.state is None:
            return Text("…", style=R.DIM)
        size = self.content_size
        return render_scene(self.state, size.width, size.height, self.tick)


class AlertBanner(Static):
    """Full-width strip that appears only when alerts are active."""

    class Pressed(Message):
        pass

    def __init__(self, **kw):
        super().__init__("", **kw)
        self.alerts: list[Alert] = []
        self._blink = False

    def on_mount(self) -> None:
        self.set_interval(0.7, self._pulse)

    def set_alerts(self, alerts: list[Alert]) -> None:
        self.alerts = alerts
        for sev in ("extreme", "severe", "moderate", "minor", "unknown"):
            self.remove_class(f"sev-{sev}")
        if not alerts:
            self.remove_class("-show")
            return
        self.add_class("-show", f"sev-{alerts[0].severity.lower()}")
        self.update(R.banner_text(alerts))

    def _pulse(self) -> None:
        if self.alerts and self.alerts[0].rank >= 3:
            self._blink = not self._blink
            self.set_class(self._blink, "-blink")
        elif self._blink:
            self._blink = False
            self.remove_class("-blink")

    def on_click(self, event: Click) -> None:
        self.post_message(self.Pressed())
