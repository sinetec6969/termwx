"""termwx command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import Config
from .units import IMPERIAL, METRIC


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="termwx", description="Ultimate terminal weather.")
    p.add_argument("-l", "--location", help='place to show: "City, ST", ZIP, or "lat,lon"')
    p.add_argument("-u", "--units", choices=[IMPERIAL, METRIC], help="override saved units")
    p.add_argument("--once", action="store_true", help="print a snapshot and exit (no TUI)")
    p.add_argument("--demo-alert", action="store_true", help="inject sample alerts to preview alert handling")
    p.add_argument("--no-anim", action="store_true", help="disable the animated scene")
    args = p.parse_args(argv)

    cfg = Config.load()
    units = args.units or cfg.units

    if args.once:
        from .once import snapshot

        return asyncio.run(snapshot(cfg, args.location, units, demo=args.demo_alert))

    from .app import TermWx

    if args.units:
        cfg.units = args.units
    if args.no_anim:
        cfg.animate = False
    app = TermWx(cfg, location_query=args.location, demo_alert=args.demo_alert)
    app.run()
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
