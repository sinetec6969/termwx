"""Sample alerts for --demo-alert, re-timed to start now."""

from __future__ import annotations

from datetime import datetime, timedelta

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
