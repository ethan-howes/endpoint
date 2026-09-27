#!/usr/bin/env python
"""Verify the accessible routing and curb access against the real demo area.

Unit tests prove each rule on a hand-built campus. This checks the rules hold on
the committed FIU data, where they can fail for reasons no fixture anticipates:

1. No link into the network cuts through a building wall (a rider leaving the
   building they are inside, by a door or its nearest side, is the one exception).
2. No route walks through a home or residence hall.
3. Walking through buildings actually happens by day and stops at night -- for
   a rider next to the Ernest R. Graham Center, whose doors are mapped.
4. S2 answers inside the orchestrator's 6 s budget in every mode, with margin
   (``--budget``, default 1.5 s).

And it reports how much of the area has known curb access, because "unknown"
is the honest answer for most spots and the number is worth knowing.

Usage (reads the committed fixtures)::

    MOCK=1 python -m scripts.verify_accessibility
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shapely.geometry import LineString, Point  # noqa: E402

from shared.config import NO_WALKTHROUGH_BUILDINGS, SETTINGS  # noqa: E402
from shared.geo import frame_for  # noqa: E402
from shared.models import Condition, LatLng, LegalSpotsRequest, RankRequest  # noqa: E402
from services.s1_legal_spots.service import legal_spots  # noqa: E402
from services.s2_weather_cover import service as s2  # noqa: E402
from services.s2_weather_cover import walk_network  # noqa: E402

TZ = ZoneInfo(SETTINGS.demo_tz)
DAY = datetime(2026, 9, 28, 12, 0, tzinfo=TZ)    # a Monday
NIGHT = datetime(2026, 9, 28, 23, 0, tzinfo=TZ)
WALL_TOLERANCE_M = 0.5


def grid(n: int) -> list[LatLng]:
    s, w, north, e = SETTINGS.demo_bbox
    return [
        LatLng(lat=s + (north - s) * (i + 0.5) / n, lng=w + (e - w) * (j + 0.5) / n)
        for i in range(n) for j in range(n)
    ]


async def network_for(rider: LatLng, spots) -> walk_network.Network | None:
    req = RankRequest(rider_location=rider, spots=spots, pickup_time=DAY)
    radius = s2.search_radius_m(req)
    bundle = await s2._walk_network(s2._tiles_covering(req, radius), radius, [],
                                    frame_for(rider.lat, rider.lng))
    return bundle[0] if bundle else None


def check_routes(net, rider: LatLng, spots, when, problems: list[str]) -> list[walk_network.Route]:
    frame = frame_for(rider.lat, rider.lng)
    rider_xy = frame.to_m(rider.lat, rider.lng)
    inside = net.buildings_containing(rider_xy)
    closed = walk_network.closed_buildings(net, when)
    search = walk_network.search(net, rider_xy, mode="length", closed=closed)
    routes = []
    for spot in spots:
        r = walk_network.route_to(net, search, frame.to_m(spot.stop_point.lat, spot.stop_point.lng))
        if r is None:
            continue
        routes.append(r)
        cut = any("cut through" in n for n in r.notes)
        # The rider's first leg may cross the wall of the building they are
        # leaving (its doors, or its nearest side), and no other.
        legs = [(r.points[-2], r.points[-1], None), (r.points[0], r.points[1], inside[0].shape if inside else None)]
        for a, b, own in legs:
            through = walk_network._wall_cut(net, a, b, own)
            if through > WALL_TOLERANCE_M and not cut:
                problems.append(f"{spot.spot_id}: link cuts {through:.1f} m through a wall")
        for bid in r.through_ids:
            if bid in closed:
                problems.append(f"{spot.spot_id}: walks through closed building {bid}")
            b = net.buildings[bid]
            if not b.walkthrough:
                problems.append(f"{spot.spot_id}: walks through non-walkthrough {b.name or bid}")
    return routes


async def main_async(args) -> int:
    problems: list[str] = []
    curb = collections.Counter()
    timings: dict[str, list[float]] = collections.defaultdict(list)
    riders = grid(args.grid)

    for rider in riders:
        resp = await legal_spots(LegalSpotsRequest(rider_location=rider))
        spots = [s for s in resp.spots if s.source != "fallback"]
        curb.update(s.curb_access.value for s in spots)
        if not spots:
            continue
        net = await network_for(rider, spots)
        if net is None:
            problems.append(f"{rider}: no walking network")
            continue
        for when in (DAY, NIGHT):
            check_routes(net, rider, spots, when, problems)
        for cond, t in ((Condition.NEUTRAL, DAY), (Condition.RAIN, DAY), (Condition.SUN, DAY)):
            req = RankRequest(rider_location=rider, spots=spots, pickup_time=t,
                              force_condition=cond, force_time=t)
            t0 = time.perf_counter()
            res = await s2.rank(req)
            timings[cond.value].append(time.perf_counter() - t0)
            if any("error" in f for f in res.fallbacks_used):
                problems.append(f"{rider} {cond.value}: {res.fallbacks_used}")

    # --- the Graham Center: through by day, round by night ---
    gc = None
    net_any = await network_for(riders[len(riders) // 2], [])
    for b in (net_any.buildings.values() if net_any else []):
        if (b.name or "").startswith("Ernest R. Graham"):
            gc = b
    day_in = night_in = 0.0
    if gc is None:
        problems.append("Graham Center not found in the building data")
    else:
        # Stand 15 m out from its westernmost door, facing away from the centre.
        frame = frame_for(*SETTINGS.demo_rider)
        c = gc.shape.centroid
        door = min(gc.doors, key=lambda d: net_any.xy[net_any.index[d]][0])
        dx, dy = net_any.xy[net_any.index[door]]
        ux, uy = dx - c.x, dy - c.y
        norm = (ux * ux + uy * uy) ** 0.5 or 1.0
        rx, ry = dx + 15 * ux / norm, dy + 15 * uy / norm
        lat, lng = frame.to_ll(rx, ry)
        rider = LatLng(lat=lat, lng=lng)
        spots = [s for s in (await legal_spots(LegalSpotsRequest(rider_location=rider))).spots
                 if s.source != "fallback"]
        net = await network_for(rider, spots)
        day = check_routes(net, rider, spots, DAY, problems)
        night = check_routes(net, rider, spots, NIGHT, problems)
        day_in = sum(r.indoor_m for r in day)
        night_in = sum(r.indoor_m for r in night)
        through_gc = sum(1 for r in day if gc.block_id in r.through_ids)
        print(f"Graham Center: {len(gc.doors)} doors; rider {rider}")
        print(f"  by day   {through_gc}/{len(day)} routes go through it, {day_in:.0f} m indoors in total")
        print(f"  by night {night_in:.0f} m indoors")
        if through_gc == 0:
            problems.append("no route walks through the Graham Center by day")
        if night_in > 0:
            problems.append("routes walk through buildings at 23:00")

    total = sum(curb.values()) or 1
    print(f"\ncurb access over {len(riders)} riders, {total} spots:")
    for k in ("flush", "lowered", "raised", "unknown"):
        print(f"  {k:8s} {curb[k]:4d}  {100 * curb[k] / total:5.1f}%")
    print("\nS2 time per call:")
    for mode, ts in timings.items():
        print(f"  {mode:8s} max {max(ts):.2f}s  median {sorted(ts)[len(ts) // 2]:.2f}s")
        if max(ts) > args.budget:
            problems.append(f"{mode} took {max(ts):.2f}s, over the {args.budget:g}s budget")

    print()
    if problems:
        print(f"FAIL: {len(problems)} problem(s)")
        for p in problems[:40]:
            print(f"  - {p}")
        return 1
    print("OK: no wall cuts, no residential or closed buildings walked through, "
          "Graham Center open by day and closed by night, all modes within budget")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grid", type=int, default=4, help="riders per side of the demo-area grid")
    ap.add_argument("--budget", type=float, default=1.5, help="max seconds per S2 call")
    args = ap.parse_args()
    if not SETTINGS.mock:
        print("note: MOCK is off, so this reads the disk cache or the network", file=sys.stderr)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
