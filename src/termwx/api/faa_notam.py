"""NOTAM provider seam (not implemented).

The FAA's machine-to-machine NOTAM service requires registration and API
credentials, and termwx's core promise is "no API keys". NOTAM search pages
must not be scraped. So NOTAMs are not fetched today; this module only reports
that clearly, and marks where a credentialed provider would plug in:

- credentials come from environment variables only (never config.toml, never
  committed): FAA_NOTAM_CLIENT_ID / FAA_NOTAM_CLIENT_SECRET
- the provider must fail independently, like every other aviation product
"""

from __future__ import annotations

import os

ENV_ID = "FAA_NOTAM_CLIENT_ID"
ENV_SECRET = "FAA_NOTAM_CLIENT_SECRET"


def configured() -> bool:
    return bool(os.environ.get(ENV_ID) and os.environ.get(ENV_SECRET))


def status() -> str:
    if configured():
        return "NOTAM credentials found, but no NOTAM provider is implemented yet — check official sources"
    return "NOTAM provider not configured — check official NOTAM sources before flight"
