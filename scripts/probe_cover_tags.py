"""Probe: what cover-ish tags actually exist in the demo area?

Run before finalising the cover query. ENDPOINT.md's query includes
`way["building"="roof"]`, which matches essentially nothing in OSM -- `building=*`
is the real tag -- so it reads like coverage and provides none. Worth knowing
which of the remaining candidates carry their weight before committing to them.
"""

from __future__ import annotations

import sys
import time
from collections import Counter

import httpx

from shared.config import SETTINGS
from shared.osm_cache import USER_AGENT

S, W, N, E = SETTINGS.demo_bbox

PROBES = {
    "a covered highway": 'way["highway"]["covered"~"^(yes|arcade)$"]',
    "man_made=awning": 'nwr["man_made"="awning"]',
    "man_made=canopy": 'nwr["man_made"="canopy"]',
    "awning=yes": 'nwr["awning"="yes"]',
    "roof:awning": 'nwr["roof:awning"="yes"]',
    "amenity=shelter": 'nwr["amenity"="shelter"]',
    "bus_stop + shelter=yes": 'node["highway"="bus_stop"]["shelter"="yes"]',
    "platform + covered": 'nwr["public_transport"="platform"]["covered"="yes"]',
    "tunnel=building_passage": 'way["tunnel"="building_passage"]',
    "covered=yes (any)": 'nwr["covered"="yes"]',
    "covered=arcade": 'nwr["covered"="arcade"]',
    "building": 'way["building"]',
    "building + levels": 'way["building"]["building:levels"]',
    "building + height": 'way["building"]["height"]',
    "natural=tree": 'node["natural"="tree"]',
    "natural=tree_row": 'way["natural"="tree_row"]',
    "leisure=park + trees": 'way["leisure"="park"]',
}


def probe(label: str, selector: str, tries: int = 4) -> Counter:
    q = f"[out:json][timeout:90];({selector}({S},{W},{N},{E}););out tags;"
    for attempt in range(tries):
        try:
            r = httpx.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": q},
                headers={"User-Agent": USER_AGENT},
                timeout=120.0,
            )
            r.raise_for_status()
            els = r.json().get("elements", [])
        except Exception as exc:  # noqa: BLE001
            if attempt == tries - 1:
                print(f"{label:28s} FAILED {type(exc).__name__}")
                return Counter()
            time.sleep(3 * (attempt + 1))
            continue

        kinds = Counter(e.get("type", "?") for e in els)
        print(f"{label:28s} {len(els):5d}  {dict(kinds)}")
        return Counter(
            (e.get("tags") or {}).get(k, "?")
            for e in els for k in ("kind", "shelter", "covered")
        )
    return Counter()


def main() -> int:
    print(f"demo box {S},{W},{N},{E}\n")
    for label, sel in PROBES.items():
        probe(label, sel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
