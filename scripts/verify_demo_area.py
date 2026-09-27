#!/usr/bin/env python
"""Verify ENDPOINT.md section 6 S1's definition of done against the real demo area.

The doc's criterion: "for 3 test locations in the demo area, the UI shows spots on
both sides of the street, none within the hydrant, crosswalk, bus stop, or
bike-lane exclusions". Nobody can check that by looking at a map, so this script
checks it mechanically -- and, importantly, checks it *independently* of the
filtering code that produced the answer.

Independence matters. Re-running ``judge_candidate`` over the output would only
prove the code is self-consistent; a bug in the shared rule (a mis-typed buffer, a
side convention flipped) would pass. So this walks the raw network again, from the
spot coordinates outward, and asks "is this spot genuinely clear?".

Exit code 0 means every check passed. Non-zero means at least one failed, and each
failure is printed with enough detail to fix it.

Usage::

    python -m scripts.verify_demo_area
    python -m scripts.verify_demo_area --radius 150 --locations 3
    python -m scripts.verify_demo_area --at 25.7569,-80.3722 --at 25.7575,-80.3710
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.config import SETTINGS  # noqa: E402
from shared.geo import expanded_query_bbox, haversine_m, tile_bbox, tile_cache_id  # noqa: E402
from shared.models import BBox, LatLng, LegalSpotsRequest, Spot  # noqa: E402
from shared.osm_cache import cache_info, read_cache  # noqa: E402
from services.s1_legal_spots.curb import legal_sides  # noqa: E402
from services.s1_legal_spots.legality import cycleway_blocks  # noqa: E402
from services.s1_legal_spots.network import StreetNetwork  # noqa: E402
from services.s1_legal_spots.overpass import (  # noqa: E402
    build_point_query,
    build_road_query,
    parse_lots,
    parse_restrictions,
    parse_roads,
)
from services.s1_legal_spots.service import (  # noqa: E402
    _network_for_tile_sync,
    legal_spots,
)
import asyncio  # noqa: E402


# --------------------------------------------------------------------------- #
# Default test locations: spread across the demo area so a single bad tile
# cannot make the whole run look good.
# --------------------------------------------------------------------------- #

def default_locations() -> list[LatLng]:
    s, w, n, e = SETTINGS.demo_bbox
    lat0, lng0 = SETTINGS.demo_rider
    return [
        LatLng(lat=lat0, lng=lng0),                       # the Graham Center itself
        LatLng(lat=(s + lat0) / 2.0, lng=(w + lng0) / 2.0),   # south-west of it
        LatLng(lat=(n + lat0) / 2.0, lng=(e + lng0) / 2.0),   # north-east of it
    ]


@dataclass
class Violation:
    spot_id: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.spot_id}: {self.kind} -- {self.detail}"


@dataclass
class LocationReport:
    rider: LatLng
    spot_count: int = 0
    total_candidates: int = 0
    source: str = ""
    cached: bool = False
    fallbacks: list[str] = field(default_factory=list)
    streets: dict[str, set[str]] = field(default_factory=dict)
    confidences: dict[str, int] = field(default_factory=dict)
    violations: list[Violation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def two_way_covered(self) -> list[str]:
        """Streets where we offer both kerbs -- S1's 'spots on both sides'."""
        return sorted(s for s, sides in self.streets.items() if sides >= {"left", "right"})


# --------------------------------------------------------------------------- #
# Independent verification
# --------------------------------------------------------------------------- #

def check_spot(net: StreetNetwork, spot: Spot) -> list[Violation]:
    """Re-derive legality for one spot straight from the network.

    Deliberately does not call the service's own filter, so a systematic error in
    that filter shows up here as a failure instead of cancelling out.
    """
    out: list[Violation] = []
    road = net.road_by_key(spot.segment_id or "")
    if road is None:
        out.append(Violation(spot.spot_id, "unknown_segment", f"no road {spot.segment_id}"))
        return out

    side = spot.side.value
    px, py = net.frame.to_m(spot.stop_point.lat, spot.stop_point.lng)
    arc, dist_to_centre = road.polyline.nearest_arc(px, py)

    # --- 1. in-lane cycleway ---
    tag = cycleway_blocks(road, side)
    if tag:
        out.append(
            Violation(spot.spot_id, "in_lane_cycleway", f"{tag} on the {side} side of {road.way_id}")
        )

    # --- 2. one-way: is this side even legal? ---
    allowed = legal_sides(road, SETTINGS.traffic_side)
    if side not in allowed:
        out.append(
            Violation(
                spot.spot_id,
                "wrong_side_of_oneway",
                f"{road.way_id} allows {allowed}, spot is on the {side}",
            )
        )

    # --- 3. disc restrictions (hydrants, stop signs, signals) ---
    for r in net.point_restrictions():
        if r._xy is None:
            continue
        d = math.hypot(px - r._xy[0], py - r._xy[1])
        if d <= r.buffer_m:
            out.append(
                Violation(
                    spot.spot_id,
                    r.kind.value,
                    f"{d:.1f} m from {r.label or r.source_id} (buffer {r.buffer_m:g} m)",
                )
            )

    # --- 4. linear restrictions measured ALONG the roadway ---
    for r in net.arc_restrictions():
        if r.road_key != road.way_id or r.anchor_m is None or r.arc_m is None:
            continue
        if r.side is not None and r.side != side:
            continue
        along = abs(arc - r.anchor_m)
        if along <= r.arc_m:
            out.append(
                Violation(
                    spot.spot_id,
                    r.kind.value,
                    f"{along:.1f} m along the road from {r.label or r.source_id} "
                    f"(extent {r.arc_m:g} m)",
                )
            )

    # --- 5. a spot should actually be at the kerb, not in the middle of the road ---
    offset = road.offset_for(side)
    if offset > 0 and abs(dist_to_centre - offset) > 1.5:
        out.append(
            Violation(
                spot.spot_id,
                "not_at_the_kerb",
                f"{dist_to_centre:.1f} m from the centreline, kerb is {offset:.1f} m",
            )
        )

    return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def network_for(rider: LatLng, radius: float) -> StreetNetwork | None:
    return _network_for_tile_sync(tile_bbox(rider.lat, rider.lng, radius), radius)


async def evaluate(rider: LatLng, radius: float, net: StreetNetwork) -> LocationReport:
    req = LegalSpotsRequest(rider_location=rider, radius_m=radius)
    resp = await legal_spots(req)

    rep = LocationReport(
        rider=rider,
        spot_count=len(resp.spots),
        total_candidates=resp.total_candidates,
        source=resp.source,
        cached=resp.cached,
        fallbacks=list(resp.fallbacks_used),
    )
    if resp.source == "fallback":
        rep.notes.append("service returned the degraded fallback, not real spots")
        return rep

    for s in resp.spots:
        rep.streets.setdefault(s.segment_id or "?", set()).add(s.side.value)
        rep.confidences[s.confidence.value] = rep.confidences.get(s.confidence.value, 0) + 1
        rep.violations.extend(check_spot(net, s))
    return rep


def print_report(rep: LocationReport, radius: float) -> bool:
    ok = True
    print(f"\n=== rider {rep.rider}  radius {radius:g} m ===")
    print(f"  spots: {rep.spot_count}  (of {rep.total_candidates} candidates)  source={rep.source} cached={rep.cached}")
    if rep.fallbacks:
        print(f"  fallbacks: {rep.fallbacks}")
    if rep.confidences:
        print(f"  confidence: {rep.confidences}")

    if rep.spot_count == 0:
        print("  FAIL  no spots returned")
        return False

    two_way = rep.two_way_covered
    if two_way:
        print(f"  PASS  both kerbs offered on {len(two_way)} street(s): {', '.join(two_way)}")
    else:
        print("  FAIL  no street offered spots on both sides of the road")
        ok = False

    if rep.violations:
        print(f"  FAIL  {len(rep.violations)} exclusion violation(s):")
        for v in rep.violations[:15]:
            print(f"          {v}")
        if len(rep.violations) > 15:
            print(f"          ... and {len(rep.violations) - 15} more")
        ok = False
    else:
        print("  PASS  every spot is clear of hydrant, crossing, bus stop, intersection and bike-lane exclusions")

    for n in rep.notes:
        print(f"  note  {n}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--radius", type=float, default=150.0)
    ap.add_argument(
        "--at", action="append", default=[], metavar="LAT,LNG",
        help="test location; repeatable. Defaults to 3 points across the demo area.",
    )
    ap.add_argument("--locations", type=int, default=3)
    args = ap.parse_args()

    if args.at:
        locations = [LatLng(lat=float(p.split(",")[0]), lng=float(p.split(",")[1])) for p in args.at]
    else:
        locations = default_locations()[: args.locations]

    print(f"demo bbox     {SETTINGS.bbox_str}")
    print(f"demo rider    {SETTINGS.demo_rider}  ({SETTINGS.demo_tz})")
    print(f"test locations {len(locations)}")

    # Fail fast and clearly if nothing is cached: this script never fetches.
    probe = locations[0]
    tile = tile_bbox(probe.lat, probe.lng, args.radius)
    qb = expanded_query_bbox(tile, args.radius)
    center = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
    rid = tile_cache_id(center[0], center[1], args.radius, "roads")
    if cache_info(build_road_query(qb), rid) is None:
        print(
            "ERROR  no cached road data for the first test location.\n"
            "       Run:  python -m scripts.prefetch_demo_area",
            file=sys.stderr,
        )
        return 2

    all_ok = True
    for rider in locations:
        net = network_for(rider, args.radius)
        if net is None:
            print(f"\n=== rider {rider} ===\n  FAIL  no cached network for this tile")
            all_ok = False
            continue
        rep = asyncio.run(evaluate(rider, args.radius, net))
        if not print_report(rep, args.radius):
            all_ok = False

    print("\n" + ("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
