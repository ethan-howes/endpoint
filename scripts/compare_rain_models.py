#!/usr/bin/env python
"""Compare the two rain rankings on real demo-area riders: gap model vs exposure model.

For each rider it asks S1 for legal spots (in-process, MOCK=1 fixtures), ranks them
both ways, and reports what each model's top pick means for the rider in metres
of rain -- measured the same way for both, along the walking network, so the
comparison is like for like.

    MOCK=1 python -m scripts.compare_rain_models

No network: S1 and S2 both read the committed fixtures.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("MOCK", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import DEFAULT_RADIUS_M  # noqa: E402
from shared.geo import frame_for  # noqa: E402
from shared.models import LatLng, LegalSpotsRequest  # noqa: E402
from services.s1_legal_spots import service as s1  # noqa: E402
from services.s2_weather_cover import rain_cover, rain_exposure, service as s2  # noqa: E402

#: Campus riders: the backend's DEMO_RIDER, a rider inside the Student Academic
#: Success Center, and the frontend's pickup presets.
RIDERS = {
    "DEMO_RIDER (default)": (25.7584, -80.3725),
    "inside SASC": (25.755785, -80.371621),
    "Graham Center": (25.75622, -80.3727),
    "Green Library": (25.7571, -80.37375),
    "Chemistry & Physics": (25.75853, -80.37202),
    "AHC 5": (25.75915, -80.37132),
    "PG5 Market Station": (25.75986, -80.37124),
    "Ryder Business": (25.75747, -80.37612),
    "Charles E. Perry (PC)": (25.75552, -80.37379),
    "School of Architecture": (25.75891, -80.37565),
}


async def compare(name: str, lat: float, lng: float) -> tuple:
    rider = LatLng(lat=lat, lng=lng)
    resp = await s1.legal_spots(LegalSpotsRequest(rider_location=rider, radius_m=DEFAULT_RADIUS_M))
    spots = resp.spots
    if not spots:
        return name, None
    frame = frame_for(lat, lng)
    radius = DEFAULT_RADIUS_M
    fallbacks: list[str] = []
    req = type("Req", (), {"spots": spots, "rider_location": rider})()
    tiles = s2._tiles_covering(req, radius)
    cover_map, shade_map = await s2._merge_maps(tiles, radius, fallbacks, frame, want_shade=True)
    path_map = await s2._merge_paths(tiles, radius, fallbacks, frame)
    if cover_map is None or path_map is None:
        return name, f"missing data: {fallbacks}"

    gap = rain_cover.rank_spots(spots, cover_map)
    net = rain_exposure.build_network(path_map, cover_map, shade_map)
    exp = rain_exposure.rank_by_exposure(spots, rider, net, frame)
    by_id = {r.spot.spot_id: r for r in exp}
    g, e = by_id[gap[0].spot.spot_id], exp[0]
    return name, (g, e)


async def main() -> int:
    print(f"{'rider':<24} {'gap model pick':>34}   {'exposure model pick':>34}   rain saved")
    for name, (lat, lng) in RIDERS.items():
        name, out = await compare(name, lat, lng)
        if out is None or isinstance(out, str):
            print(f"{name:<24} {out or 'no legal spots'}")
            continue
        g, e = out
        fmt = lambda r: f"{r.spot.spot_id} {r.wet_m:>5.0f} m wet / {r.spot.walk_distance_m:>4.0f} m walk"
        saved = (g.wet_m or 0) - (e.wet_m or 0)
        same = "  (same spot)" if g.spot.spot_id == e.spot.spot_id else ""
        print(f"{name:<24} {fmt(g):>34}   {fmt(e):>34}   {saved:>5.0f} m{same}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
