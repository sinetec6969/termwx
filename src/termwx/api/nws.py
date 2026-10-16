"""National Weather Service: active alerts and Area Forecast Discussions (US and territories)."""

from __future__ import annotations

import re
from datetime import datetime

from ..models import Alert, AlertsResult, DiscussionSection, ForecastDiscussion, Location
from .http import FetchError, Http

API = "https://api.weather.gov"
ALERTS_URL = f"{API}/alerts/active"
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


# ── Area Forecast Discussion ─────────────────────────────────────────────────
_SECTION = re.compile(r"^\.([A-Z][^\n]*?)\.\.\.(.*)$")
# Shown first, in this order; everything else follows in issued order.
SECTION_PRIORITY = ("AVIATION", "NEAR TERM", "SHORT TERM", "LONG TERM")


def parse_discussion_sections(text: str) -> list[DiscussionSection]:
    """Split an AFD into its ".TITLE..." sections, ending at "&&" or "$$"."""
    sections: list[DiscussionSection] = []
    title, body = None, []
    for line in text.replace("\r\n", "\n").split("\n"):
        m = _SECTION.match(line)
        if m:
            if title is not None:
                sections.append(DiscussionSection(title, "\n".join(body).strip()))
            title, body = m.group(1).strip(), [m.group(2).strip()] if m.group(2).strip() else []
        elif line.strip() in ("&&", "$$"):
            if title is not None:
                sections.append(DiscussionSection(title, "\n".join(body).strip()))
            title, body = None, []
        elif title is not None:
            body.append(line.rstrip())
    if title is not None:
        sections.append(DiscussionSection(title, "\n".join(body).strip()))
    return sections


def order_sections(sections: list[DiscussionSection]) -> list[DiscussionSection]:
    def rank(s: DiscussionSection) -> int:
        return next((i for i, k in enumerate(SECTION_PRIORITY) if s.key.startswith(k)), len(SECTION_PRIORITY))

    return sorted(sections, key=rank)  # stable: unlisted sections keep issued order


def parse_discussion(product: dict, office: str) -> ForecastDiscussion:
    text = (product.get("productText") or "").replace("\r\n", "\n").strip()
    issued = _dt(product.get("issuanceTime"))
    if issued is None:
        raise ValueError("discussion has no issuance time")
    return ForecastDiscussion(
        office=office,
        issued=issued,
        text=text,
        product_id=product.get("id") or "",
        sections=order_sections(parse_discussion_sections(text)),
    )


async def fetch_office(http: Http, loc: Location) -> str | None:
    """The Weather Forecast Office (CWA) responsible for a point; None outside NWS coverage."""
    if loc.country_code and loc.country_code.upper() not in NWS_COUNTRIES:
        return None
    try:
        data, _ = await http.get_json(f"{API}/points/{loc.lat:.4f},{loc.lon:.4f}", headers={"Accept": "application/geo+json"})
    except FetchError as e:
        if e.status in (400, 404):
            return None
        raise
    props = (data or {}).get("properties") or {}
    return props.get("cwa") or props.get("gridId") or None


async def fetch_discussion(
    http: Http, office: str, previous: ForecastDiscussion | None = None
) -> ForecastDiscussion | None:
    """Latest AFD for an office. Skips the product download when it hasn't changed."""
    listing, stale = await http.get_json(
        f"{API}/products/types/AFD/locations/{office}", headers={"Accept": "application/ld+json"}
    )
    graph = (listing or {}).get("@graph") or []
    if not graph:
        return None
    latest = max(graph, key=lambda g: g.get("issuanceTime") or "")
    if previous and previous.product_id == latest.get("id") and not previous.stale:
        previous.fetched_at, previous.stale = datetime.now(), stale
        return previous
    product, pstale = await http.get_json(f"{API}/products/{latest['id']}", headers={"Accept": "application/ld+json"})
    disc = parse_discussion(product or {}, office)
    disc.stale = stale or pstale
    return disc
