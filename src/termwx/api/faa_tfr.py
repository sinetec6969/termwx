"""FAA temporary flight restrictions from tfr.faa.gov. No API key.

Two published FAA data exports are used, not the web pages:

- the GeoServer WFS layer behind the TFR map: GeoJSON outlines of every
  active TFR in a single request (about 80 nationwide), filtered here;
- the per-NOTAM XNOTAM XML: validity (UTC), altitudes per area, the 14 CFR
  section and the full NOTAM text. Only TFRs near the point get this request.

Stadium (§99.7 blanket) restrictions and most other NOTAMs aren't in this
feed, so a TFR check here never replaces a full NOTAM check.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

from .. import aviation as A
from ..aviation_models import Tfr, TfrArea
from .awc import _int, _ts
from .http import FetchError, Http

WFS_URL = "https://tfr.faa.gov/geoserver/TFR/ows"
WFS_PARAMS = {
    "service": "WFS",
    "version": "1.1.0",
    "request": "GetFeature",
    "typeName": "TFR:V_TFR_LOC",
    "outputFormat": "application/json",
}
DETAIL_URL = "https://tfr.faa.gov/download/detail_{}.xml"
MAX_DETAILS = 8  # detail requests per refresh; farther TFRs keep just their title
_REFS = {"HEI": "AGL", "ALT": "MSL"}


def _ring(coords) -> list[tuple[float, float]]:
    """GeoJSON [lon, lat] pairs → (lat, lon)."""
    return [(float(p[1]), float(p[0])) for p in coords or [] if len(p) >= 2]


def parse_tfr_list(geojson) -> list[Tfr]:
    """WFS FeatureCollection → one Tfr per NOTAM; a NOTAM's several areas arrive as separate features."""
    out: dict[str, Tfr] = {}
    for f in (geojson or {}).get("features") or []:
        p = f.get("properties") or {}
        notam_id = str(p.get("NOTAM_KEY") or f.get("id", "").rpartition(".")[2]).split("-")[0]
        if not notam_id:
            continue
        geom = f.get("geometry") or {}
        if geom.get("type") == "Polygon":
            rings = [geom["coordinates"][0]]  # outer ring; TFR outlines have no holes
        elif geom.get("type") == "MultiPolygon":
            rings = [poly[0] for poly in geom["coordinates"]]
        else:
            rings = []
        t = out.get(notam_id)
        if t is None:
            t = out[notam_id] = Tfr(notam_id, str(p.get("LEGAL") or "").strip(), str(p.get("TITLE") or "").strip(),
                                    state=str(p.get("STATE") or ""), facility=str(p.get("CNS_LOCATION_ID") or ""))
        t.polygons += [r for r in map(_ring, rings) if len(r) >= 3]
    return list(out.values())


def detail_url(notam_id: str) -> str:
    """'6/9169' → …/detail_6_9169.xml"""
    return DETAIL_URL.format(notam_id.replace("/", "_"))


def _alt(code: str | None, value: str | None, uom: str | None) -> tuple[int | None, str]:
    v = _int(value)
    if (uom or "").upper() == "FL":
        return (None if v is None else v * 100), "FL"
    return v, _REFS.get((code or "").upper(), (code or "").upper())


def parse_tfr_detail(text: str, t: Tfr) -> None:
    """Fill validity, rule, areas and text from an XNOTAM document. Times are UTC
    (they match the NOTAM's own YYMMDDHHMM-YYMMDDHHMM group whatever codeTimeZone says)."""
    root = ET.fromstring(text.lstrip("﻿"))
    n = root.find(".//Not")
    if n is None:
        raise ValueError("no NOTAM in TFR detail")
    t.valid_from, t.valid_to = _ts(n.findtext("dateEffective")), _ts(n.findtext("dateExpire"))
    t.rule = (n.findtext("TfrNot/codeType") or "").strip()
    t.areas = []
    for a in n.iter("aseTFRArea"):
        base, base_ref = _alt(a.findtext("codeDistVerLower"), a.findtext("valDistVerLower"), a.findtext("uomDistVerLower"))
        top, top_ref = _alt(a.findtext("codeDistVerUpper"), a.findtext("valDistVerUpper"), a.findtext("uomDistVerUpper"))
        t.areas.append(TfrArea((a.findtext("txtName") or "").strip(), base, base_ref, top, top_ref))
        t.scheduled = t.scheduled or (a.findtext("isScheduledTfrArea") or "").strip().lower() == "true"
    t.raw = (n.findtext("txtDescrTraditional") or n.findtext("txtDescrUSNS") or "").strip()


async def fetch_tfrs(http: Http, lat: float, lon: float, near_nm: float = A.TFR_NEAR_NM) -> tuple[list[Tfr], bool]:
    """TFRs over or within `near_nm` of the point, nearest first, with details for
    the closest few. A failed detail request leaves that TFR with just its outline."""
    data, stale = await http.get_json(WFS_URL, WFS_PARAMS)
    tfrs = parse_tfr_list(data)
    A.locate_tfrs(tfrs, lat, lon)
    near = sorted((t for t in tfrs if t.contains_point or (t.distance_nm is not None and t.distance_nm <= near_nm)),
                  key=lambda t: (not t.contains_point, t.distance_nm or 0))
    results = await asyncio.gather(*(http.get_text_cached(detail_url(t.notam_id)) for t in near[:MAX_DETAILS]),
                                   return_exceptions=True)
    for t, res in zip(near, results):
        if isinstance(res, BaseException):
            if not isinstance(res, FetchError):
                raise res
            t.detail_error = str(res) or "detail unavailable"
            continue
        text, s = res
        stale = stale or s
        try:
            parse_tfr_detail(text, t)
        except (ET.ParseError, ValueError) as e:
            t.detail_error = f"unexpected detail format ({e})"
    for t in near[MAX_DETAILS:]:
        t.detail_error = "details not fetched (too many nearby)"
    return near, stale
