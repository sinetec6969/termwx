"""National Weather Service active alerts (US and territories)."""

from __future__ import annotations

from datetime import datetime

from ..models import Alert, AlertsResult, Location
from .http import FetchError, Http

ALERTS_URL = "https://api.weather.gov/alerts/active"
NWS_COUNTRIES = {"US", "PR", "GU", "VI", "AS", "MP", "UM"}


def _dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def parse_alerts(data: dict) -> list[Alert]:
    seen: set[str] = set()
    alerts: list[Alert] = []
    for feat in data.get("features") or []:
        p = feat.get("properties") or {}
        aid = p.get("id") or feat.get("id") or ""
        if aid in seen or p.get("status", "Actual") != "Actual":
            continue
        seen.add(aid)
        alerts.append(
            Alert(
                id=aid,
                event=p.get("event") or "Alert",
                severity=p.get("severity") or "Unknown",
                headline=p.get("headline") or p.get("event") or "",
                description=(p.get("description") or "").strip(),
                instruction=(p.get("instruction") or "").strip(),
                urgency=p.get("urgency") or "",
                certainty=p.get("certainty") or "",
                area=p.get("areaDesc") or "",
                sender=p.get("senderName") or "",
                onset=_dt(p.get("onset") or p.get("effective")),
                ends=_dt(p.get("ends")),
                expires=_dt(p.get("expires")),
            )
        )
    return sort_alerts(alerts)


def sort_alerts(alerts: list[Alert]) -> list[Alert]:
    return sorted(alerts, key=lambda a: (-a.rank, a.onset.timestamp() if a.onset else 0))


async def fetch_alerts(http: Http, loc: Location) -> AlertsResult:
    if loc.country_code and loc.country_code.upper() not in NWS_COUNTRIES:
        return AlertsResult([], supported=False)
    try:
        data, stale = await http.get_json(
            ALERTS_URL,
            {"point": f"{loc.lat:.4f},{loc.lon:.4f}"},
            headers={"Accept": "application/geo+json"},
        )
    except FetchError as e:
        if e.status in (400, 404):
            return AlertsResult([], supported=False)
        return AlertsResult([], error=str(e), stale=True)
    return AlertsResult(parse_alerts(data), stale=stale)
