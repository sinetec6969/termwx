"""`termwx setup`: first-run questions in the plain terminal, not the TUI, so the
installer can run it under `curl | sh` with answers read from /dev/tty.

Asks for the location (ZIP, city or lat,lon, or IP detection), units, the
aviation airport (auto = nearest, or a pinned ICAO) and notification/animation
preferences, then writes config.toml. Current settings are the defaults, so a
re-run only changes what you touch. Nothing is saved if you quit part-way.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Callable

from rich.console import Console
from rich.markup import escape

from . import units as U
from .api import awc, geo
from .api.http import FetchError, Http
from .config import Config
from .models import Location
from .render import ACCENT, DIM
from .render_aviation import fmt_distance

MAX_MATCHES = 5
Ask = Callable[[str], str]


class _Prompter:
    def __init__(self, ask: Ask, console: Console):
        self.ask, self.console = ask, console

    def line(self, prompt: str) -> str:
        self.console.print(prompt, end="")
        return self.ask("").strip()

    def yes(self, question: str, default: bool) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            ans = self.line(f"{question} [{DIM}]{escape(hint)}[/] ").lower()
            if not ans:
                return default
            if ans in ("y", "yes"):
                return True
            if ans in ("n", "no"):
                return False
            self.console.print(f"[{DIM}]Please answer y or n.[/]")


def _heading(console: Console, step: int, title: str) -> None:
    console.print()
    console.print(f"[bold {ACCENT}]{step}. {title}[/]")


async def _location(p: _Prompter, http: Http, current: Location | None) -> Location:
    keep = f"Enter = keep {escape(current.label)}" if current else "Enter = detect from my IP address"
    while True:
        text = p.line(f'ZIP code, city ("Denver, CO") or lat,lon  [{DIM}]{keep}[/]\n> ')
        if not text:
            if current:
                return current
            try:
                loc = await geo.autolocate(http)
            except (FetchError, KeyError, ValueError) as e:
                p.console.print(f"[yellow]Couldn't detect your location ({escape(str(e) or 'offline')}). Type a place instead.[/]")
                continue
            if p.yes(f"Detected [bold]{escape(loc.label)}[/] ({loc.coords}). Use it?", True):
                return loc
            continue
        try:
            results = await geo.search(http, text, count=MAX_MATCHES)
        except FetchError as e:
            p.console.print(f"[yellow]Location search failed ({escape(str(e))}). Offline? Enter coordinates as lat,lon.[/]")
            continue
        if not results:
            p.console.print(f"[yellow]No place matches “{escape(text)}”. Try a ZIP code, “City, ST” or lat,lon.[/]")
            continue
        if len(results) == 1:
            loc = results[0]
            if geo.parse_coords(text) or p.yes(f"Found [bold]{escape(loc.label)}[/] ({loc.coords}). Use it?", True):
                return loc
            continue
        for i, loc in enumerate(results[:MAX_MATCHES], 1):
            p.console.print(f"  [bold]{i}[/]. {escape(loc.label)}  [{DIM}]{loc.coords}[/]")
        ans = p.line(f"Pick 1–{min(len(results), MAX_MATCHES)}  [{DIM}]Enter = 1 · anything else = search again[/]\n> ")
        if not ans:
            return results[0]
        if ans.isdigit() and 1 <= int(ans) <= min(len(results), MAX_MATCHES):
            return results[int(ans) - 1]


def _units(p: _Prompter, default: str) -> str:
    letter = "F" if default == U.IMPERIAL else "C"
    while True:
        ans = p.line(f"Temperatures in °F or °C?  [{DIM}]Enter = {letter}[/]  ").upper().lstrip("°")
        if not ans:
            return default
        if ans in ("F", "IMPERIAL"):
            return U.IMPERIAL
        if ans in ("C", "METRIC"):
            return U.METRIC
        p.console.print(f"[{DIM}]Type F or C.[/]")


async def _airport(p: _Prompter, http: Http, loc: Location, current: str, units: str) -> str:
    try:
        rows, auto = await awc.nearby_stations(http, loc.lat, loc.lon)
    except FetchError as e:
        p.console.print(f"[yellow]Couldn't look up airports ({escape(str(e))}).[/] "
                        f"Keeping {'auto (nearest)' if not current else current}; change it later in the app with v → c.")
        return current
    if rows:
        p.console.print("Nearest airports with weather reports:")
        for i, (st, nm, brg) in enumerate(rows, 1):
            tags = ("  [green]TAF[/]" if st.has_taf else "") + (f"  [{ACCENT}]← auto picks this[/]" if st.icao == auto else "")
            p.console.print(f"  [bold]{i}[/]. [bold]{st.icao}[/]  {escape(st.name)}  "
                            f"[{DIM}]{fmt_distance(nm, units)} {U.compass(brg)}[/]{tags}")
    else:
        p.console.print("[yellow]No airport with weather reports within 75 NM.[/] You can still type an ICAO code.")
    p.console.print(f"[{DIM}]auto = always the nearest airport to whatever location you're viewing (recommended).[/]")
    keep = f"Enter = keep {current}" if current else "Enter = auto"
    while True:
        ans = p.line(f"Airport: a number, an ICAO code like KCLT, or auto  [{DIM}]{keep}[/]\n> ").upper()
        if not ans:
            return current
        if ans == "AUTO":
            return ""
        if ans.isdigit():
            if 1 <= int(ans) <= len(rows):
                return rows[int(ans) - 1][0].icao
            p.console.print(f"[{DIM}]Pick a number from the list.[/]")
            continue
        try:
            st = await awc.lookup_station(http, ans)
        except FetchError as e:
            p.console.print(f"[yellow]Couldn't check {escape(ans)} ({escape(str(e))}); saving it anyway.[/]")
            return ans
        if st is None:
            p.console.print(f"[yellow]{escape(ans)} isn't in the Aviation Weather Center's station list.[/]")
            continue
        p.console.print(f"Using [bold]{st.icao}[/] {escape(st.name)}.")
        return st.icao


async def run(cfg: Config, http: Http, ask: Ask = input, console: Console | None = None) -> int:
    """The interactive flow. Returns 0 when saved, 1 on EOF, 130 on Ctrl-C."""
    console = console or Console(highlight=False)
    p = _Prompter(ask, console)
    try:
        console.print(f"[bold #0d1117 on {ACCENT}] termwx setup [/]  "
                      f"[{DIM}]Enter keeps the value shown · Ctrl-C quits without saving[/]")
        _heading(console, 1, "Location")
        loc = await _location(p, http, cfg.location)
        console.print(f"Location: [bold]{escape(loc.label)}[/] [{DIM}]{loc.coords}[/]")

        _heading(console, 2, "Units")
        moved = cfg.location is None or not loc.same_place(cfg.location)
        default = (U.IMPERIAL if loc.country_code == "US" else U.METRIC) if moved and loc.country_code else cfg.units
        units = _units(p, default)

        _heading(console, 3, "Airport")
        station = await _airport(p, http, loc, cfg.aviation_station, units)

        _heading(console, 4, "Preferences")
        notify = p.yes("Desktop notifications for new weather alerts?", cfg.desktop_notify)
        bell = p.yes("Ring the terminal bell for new alerts?", cfg.bell)
        animate = p.yes("Animated sky?", cfg.animate)
        visitor = p.yes("Sky visitor (quotes, facts, jokes and headlines)?", cfg.visitor)
    except KeyboardInterrupt:
        console.print("\nSetup cancelled — nothing was saved.")
        return 130
    except EOFError:
        console.print("\nNo more input — setup cancelled, nothing was saved.")
        return 1

    if moved:
        cfg.location = loc
    cfg.units, cfg.aviation_station = units, station
    cfg.desktop_notify, cfg.bell, cfg.animate, cfg.visitor = notify, bell, animate, visitor
    cfg.save()
    console.print()
    console.print(f"[green]✔[/] Saved to [bold]{escape(str(cfg.path))}[/]")
    console.print(f"  {escape(loc.label)} · {'°F' if units == U.IMPERIAL else '°C'} · airport {station or 'auto (nearest)'}")
    console.print("Run [bold]termwx[/] to start · [bold]termwx setup[/] to change these later.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="termwx setup", description="Set your location, units, airport and preferences.").parse_args(argv)
    if not sys.stdin.isatty():
        print("termwx setup needs an interactive terminal. To pick a place without it, run: termwx -l \"City, ST\"",
              file=sys.stderr)
        return 2

    async def go() -> int:
        http = Http()
        try:
            return await run(Config.load(), http)
        finally:
            await http.aclose()

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:  # Ctrl-C while a request was in flight
        print("\nSetup cancelled — nothing was saved.")
        return 130
