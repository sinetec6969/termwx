"""FAA NOTAM Management Service (NMS) API, opt-in.

NMS access is free but not self-serve: request credentials by email at
NOTAMS@faa.gov. termwx's core stays key-free, so this provider is only used
when both environment variables are set:

- credentials come from FAA_NOTAM_CLIENT_ID / FAA_NOTAM_CLIENT_SECRET only
  (never config.toml, never committed, never written to the disk cache)
- FAA_NOTAM_ENV=staging|fit selects a test server (default: production)
- the provider fails independently, like every other aviation product

Auth is OAuth 2.0 client credentials; the bearer token lives in memory only.
"""

from __future__ import annotations

import os
import time

import httpx

from ..aviation_models import Notam
from .awc import _ts
from .http import FetchError, Http

ENV_ID = "FAA_NOTAM_CLIENT_ID"
ENV_SECRET = "FAA_NOTAM_CLIENT_SECRET"
ENV_SERVER = "FAA_NOTAM_ENV"
HOSTS = {
    "production": "https://api-nms.aim.faa.gov",
    "staging": "https://api-staging.cgifederal-aim.com",
    "fit": "https://api-fit.cgifederal-aim.com",
}
TOKEN_MARGIN_S = 60

# (credentials key, token, monotonic expiry)
_token: tuple[str, str, float] | None = None


def configured() -> bool:
    return bool(os.environ.get(ENV_ID) and os.environ.get(ENV_SECRET))


def status() -> str:
    if configured():
        return "FAA NOTAM Management Service"
    return "not configured — free FAA NMS access: email NOTAMS@faa.gov, then set FAA_NOTAM_CLIENT_ID / FAA_NOTAM_CLIENT_SECRET"


def host() -> str:
    return HOSTS.get(os.environ.get(ENV_SERVER, "").strip().lower(), HOSTS["production"])


async def _bearer(http: Http) -> str:
    global _token
    cid, secret = os.environ.get(ENV_ID, ""), os.environ.get(ENV_SECRET, "")
    key = f"{host()}|{cid}"
    if _token and _token[0] == key and time.monotonic() < _token[2]:
        return _token[1]
    try:
        resp = await http.client.post(f"{host()}/v1/auth/token", data={"grant_type": "client_credentials"},
                                      auth=(cid, secret))
    except httpx.HTTPError as e:
        raise FetchError(f"NMS sign-in failed: {e or e.__class__.__name__}") from e
    if resp.status_code >= 400:
        raise FetchError(f"NMS sign-in failed (HTTP {resp.status_code}) — check FAA_NOTAM_CLIENT_ID/SECRET", resp.status_code)
    try:
        body = resp.json()
        token = body["access_token"]
        expires_in = float(body.get("expires_in") or 1800)
    except (ValueError, KeyError, TypeError) as e:
        raise FetchError("NMS sign-in: unexpected token response") from e
    _token = (key, token, time.monotonic() + max(expires_in - TOKEN_MARGIN_S, 30))
    return token


def forget_token() -> None:
    global _token
    _token = None


# TODO: verify against a real NMS response. This follows the GeoJSON shape the
# legacy FAA NOTAM API used (properties.coreNOTAMData.notam), which NMS is
# documented to keep, and falls back to flat properties.
def parse_notams(data) -> list[Notam]:
    body = data.get("data", data) if isinstance(data, dict) else None
    items = body.get("geojson", body.get("items")) if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise ValueError("no NOTAM list in NMS response")
    out = []
    for f in items:
        props = (f or {}).get("properties") or f or {}
        n = (props.get("coreNOTAMData") or {}).get("notam") or props
        number, text = str(n.get("number") or n.get("id") or ""), str(n.get("text") or "").strip()
        if not number and not text:
            continue
        out.append(Notam(
            number=number,
            location=str(n.get("icaoLocation") or n.get("location") or ""),
            text=text,
            classification=str(n.get("classification") or ""),
            feature=str(n.get("featureType") or n.get("feature") or ""),
            valid_from=_ts(n.get("effectiveStart")),
            valid_to=_ts(n.get("effectiveEnd")),  # "PERM" parses to None
        ))
    return out


async def fetch_notams(http: Http, icao: str) -> tuple[list[Notam], bool]:
    """NOTAMs for one airport. Raises FetchError when unconfigured or on any failure."""
    if not configured():
        raise FetchError("NOTAM provider not configured")
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {await _bearer(http)}", "nmsResponseFormat": "GEOJSON",
                   "Accept": "application/json"}
        try:
            # cache=False: authorized responses never go to the disk cache
            data, _ = await http.get_json(f"{host()}/nmsapi/v1/notams", {"location": icao}, headers=headers, cache=False)
        except FetchError as e:
            if e.status == 401 and attempt == 1:
                forget_token()  # revoked or expired early; sign in again once
                continue
            raise
        try:
            notams = parse_notams(data)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise FetchError(f"NMS: unexpected response format ({e})") from e
        return sorted(notams, key=lambda n: n.valid_from.timestamp() if n.valid_from else 0, reverse=True), False
    raise FetchError("NMS: not authorized")
