"""Regenerate src/termwx/data/fd_sites.json: coordinates for winds-aloft (FD) sites.

The AWC windtemp product only names its sites by 3-character identifier, so
termwx ships a small lookup table built from AWC's own station metadata.
Run occasionally (sites rarely change):

    .venv/bin/python scripts/build_fd_sites.py

Sites that cannot be matched to an AWC station are left out; termwx simply
won't pick them as the nearest winds-aloft site.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from termwx.api.http import USER_AGENT

API = "https://aviationweather.gov/api/data"
REGIONS = ["us", "alaska", "hawaii", "other_pac"]
# Big boxes for the non-CONUS regions; matched on FAA/IATA id instead of K+id.
BOXES = {
    "alaska": ["51,-180,58,-150", "58,-170,64,-140", "64,-170,72,-140", "54,-150,61,-129"],
    "hawaii": ["18,-161,23,-154"],
    "other_pac": ["-15,-172,-13,-169", "13,144,16,146", "5,130,10,172", "18,-170,30,-160"],
}
OUT = Path(__file__).resolve().parents[1] / "src" / "termwx" / "data" / "fd_sites.json"


def site_ids(text: str) -> list[str]:
    ids, in_table = [], False
    for line in text.splitlines():
        if line.startswith("FT"):
            in_table = True
            continue
        if in_table and re.match(r"^[A-Z0-9]{3}\b", line):
            ids.append(line[:3])
    return ids


def main() -> None:
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30)

    def get(path: str, **params):
        time.sleep(1)  # stay far below the 100 req/min limit
        r = client.get(f"{API}/{path}", params=params)
        r.raise_for_status()
        return r if r.status_code != 204 else None

    sites: dict[str, dict] = {}
    for region in REGIONS:
        r = get("windtemp", region=region, level="low", fcst="06")
        ids = site_ids(r.text if r else "")
        if region == "us":
            for chunk in range(0, len(ids), 100):
                part = ids[chunk : chunk + 100]
                r = get("stationinfo", ids=",".join("K" + i for i in part), format="json")
                for s in r.json() if r else []:
                    sid = s["icaoId"][1:]
                    if sid in part:
                        sites[sid] = {"lat": s["lat"], "lon": s["lon"], "region": region}
        else:
            wanted = set(ids)
            for box in BOXES[region]:
                r = get("stationinfo", bbox=box, format="json")
                for s in r.json() if r else []:
                    for key in ("faaId", "iataId"):
                        sid = s.get(key)
                        if sid in wanted and sid not in sites:
                            sites[sid] = {"lat": s["lat"], "lon": s["lon"], "region": region}
        missing = [i for i in ids if i not in sites]
        print(f"{region}: {len(ids)} sites, {len(missing)} unmatched {missing}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dict(sorted(sites.items())), separators=(",", ":")) + "\n")
    print(f"wrote {len(sites)} sites to {OUT}")


if __name__ == "__main__":
    main()
