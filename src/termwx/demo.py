"""Sample alerts (and a TFR) for --demo-alert, re-timed to start now."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from . import aviation as A
from .aviation_models import Tfr, TfrArea
from .models import Alert


def demo_alerts() -> list[Alert]:
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    return [
        Alert(
            id="demo-svr-tstorm",
            event="Severe Thunderstorm Warning",
            severity="Severe",
            urgency="Immediate",
            certainty="Observed",
            headline="[DEMO] Severe Thunderstorm Warning until " + (now + timedelta(minutes=45)).strftime("%-I:%M %p"),
            description=(
                "[DEMO – not a real alert]\n\n"
                "At this time, a severe thunderstorm was located 8 miles west of your area, moving east at 35 mph.\n\n"
                "HAZARD...60 mph wind gusts and quarter size hail.\n\n"
                "SOURCE...Radar indicated.\n\n"
                "IMPACT...Hail damage to vehicles is expected. Expect wind damage to roofs, siding, and trees."
            ),
            instruction=(
                "For your protection move to an interior room on the lowest floor of a building."
            ),
            area="Demo County",
            sender="termwx demo",
            onset=now,
            ends=now + timedelta(minutes=45),
            source="DEMO",
        ),
        Alert(
            id="demo-heat",
            event="Heat Advisory",
            severity="Moderate",
            urgency="Expected",
            certainty="Likely",
            headline="[DEMO] Heat Advisory until " + (now + timedelta(hours=7)).strftime("%-I:%M %p"),
            description=(
                "[DEMO – not a real alert]\n\n"
                "* WHAT...Heat index values up to 109 expected.\n\n"
                "* IMPACTS...Hot temperatures and high humidity may cause heat illnesses."
            ),
            instruction="Drink plenty of fluids, stay in an air-conditioned room, stay out of the sun, and check up on relatives and neighbors.",
            area="Demo County",
            sender="termwx demo",
            onset=now,
            ends=now + timedelta(hours=7),
            source="DEMO",
        ),
    ]


def demo_tfr(lat: float, lon: float) -> Tfr:
    """A 3 NM hazard TFR centered 1 NM east of the point, so the point is inside it."""
    now = A.utcnow().replace(second=0, microsecond=0)
    clat, clon = lat, lon + 1 / (60 * math.cos(math.radians(lat)))
    ring = [(clat + 3 / 60 * math.cos(math.radians(b)),
             clon + 3 / 60 * math.sin(math.radians(b)) / math.cos(math.radians(clat))) for b in range(0, 361, 10)]
    end = now + timedelta(hours=3)
    t = Tfr("DEMO", "HAZARDS", "[DEMO] Demo County, wildfire firefighting operations", polygons=[ring],
            valid_from=now, valid_to=end, rule="91.137(a)(2)", areas=[TfrArea("Area A", 0, "AGL", 3000, "MSL")],
            raw="[DEMO – not a real NOTAM]\n\n!FDC 6/0000 ZZZ DEMO..AIRSPACE DEMO COUNTY..TEMPORARY FLIGHT RESTRICTIONS "
                "WI AN AREA DEFINED AS 3NM RADIUS SFC-3000FT TO PROVIDE A SAFE ENVIRONMENT FOR FIRE FIGHTING AIRCRAFT "
                f"OPS. {now:%y%m%d%H%M}-{end:%y%m%d%H%M}")
    A.locate_tfrs([t], lat, lon)
    return t
