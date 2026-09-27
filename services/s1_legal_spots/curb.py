"""Candidate generation: turn roads into candidate stop points.

The one significant simplification in this service. ENDPOINT.md section 6 S1
step 1 says to "offset the centerline to each side ... to approximate the curb
line" and then sample along that offset line. Building offset polylines is where
self-intersection artifacts, miter spikes at sharp corners, and
shorter-than-offset segments live.

Nothing downstream actually needs a continuous curb line. The UI draws dots,
``curb_bearing_deg`` is just the normal at the sample point, side exclusions are
tag-driven, and S2 derives wait points from cover features rather than from the
curb. So we sample points directly off the centerline instead:

    walk the centerline by arc length in SAMPLE_STEP_M increments
      -> take the containing segment's unit tangent
      -> place the point at that side's half-width offset
      -> validate it is still inside the road corridor

That last check is the safety net that makes the simplification safe. Any offset
blowup -- a hairpin, a 2 m fragment, a degenerate tangent -- lands far from its
own centerline and gets rejected by ``CORRIDOR_TOL_M``. It costs one distance
computation per candidate and removes an entire class of geometry bugs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shared.config import CORRIDOR_TOL_M, SAMPLE_STEP_M
from shared.geo import offset_point, side_normal

from .network import Road


@dataclass(frozen=True)
class Candidate:
    """A generated stop point, before legality filtering.

    ``arc_m`` is retained because arc-positioned restrictions (crossings, bus
    stops, intersections) are matched against it. Keeping arc rather than a
    projected point means the restriction test is a scalar comparison instead of
    a geometry operation, which matters when there are hundreds of each.
    """

    road: Road
    side: str  # "left" | "right"
    arc_m: float
    x: float
    y: float
    #: Compass bearing from the stop point toward the sidewalk (degrees
    #: clockwise from north). S3 uses this to aim the camera.
    curb_bearing_deg: float
    offset_m: float


def _normal_bearing_deg(tx: float, ty: float, side: str) -> float:
    """Compass bearing of the outward normal on ``side`` of a roadway.

    Converting to a compass bearing (clockwise from north) is
    ``degrees(atan2(east, north))``. The normal itself comes from
    ``shared.geo.side_normal`` so that the direction the point is *placed* and the
    direction S3 is told to *aim* are the same vector by construction.
    """
    nx, ny = side_normal(tx, ty, side)
    return math.degrees(math.atan2(nx, ny)) % 360.0


def legal_sides(road: Road, traffic_side: str = "right") -> tuple[str, ...]:
    """Which sides of this road a vehicle may stop on.

    On a two-way street, both. On a one-way, only the side traffic runs on --
    and ``oneway=-1`` means that side is the way's LEFT, not its right. Getting
    this backwards puts the car in oncoming traffic, and with 71% of Miami ways
    tagged ``oneway`` it is not an edge case.

    ENDPOINT.md section 6 S1 does not consider one-way at all.
    """
    if not road.oneway:
        return ("left", "right")

    # Side, in the way's own frame, that traffic runs on.
    if traffic_side == "right":
        travel_side = "left" if road.oneway_reversed else "right"
    else:
        travel_side = "right" if road.oneway_reversed else "left"
    return (travel_side,)


def generate_candidates(
    road: Road,
    traffic_side: str = "right",
    step_m: float = SAMPLE_STEP_M,
) -> list[Candidate]:
    """Sample candidate stop points along one road.

    Sampling starts a half-step in, which biases points toward the middle of each
    span rather than onto its endpoints, where intersections already exclude
    them anyway.
    """
    if road.polyline.length < step_m:
        return []

    out: list[Candidate] = []
    for side in legal_sides(road, traffic_side):
        offset = road.offset_for(side)
        if offset <= 0:
            continue

        s = step_m / 2.0
        while s < road.polyline.length:
            x, y, tx, ty = road.polyline.point_at(s)
            # `side` is essential here, not cosmetic: it is what puts the point on
            # this side's kerb instead of the opposite one.
            px, py = offset_point(x, y, tx, ty, offset, side=side)

            # Corridor validation, both directions. A legitimate candidate sits
            # exactly `offset` from its own centerline point, so its distance to
            # the *nearest* point on the whole way should be `offset` too.
            #
            #   much greater -> the offset blew up (hairpin, degenerate tangent).
            #   much smaller  -> the way doubles back near here (parking-aisle
            #                    loops, parallel service roads) and we would be
            #                    offering a spot in the travel lane.
            #
            # Both are one comparison on a value we compute anyway. The second
            # case fired on 0.30% of candidates across the demo area and was the
            # only real violation in the section 6 definition-of-done check.
            corridor = road.polyline.distance_to(px, py)
            if corridor > offset + CORRIDOR_TOL_M or corridor < offset - CORRIDOR_TOL_M:
                s += step_m
                continue

            out.append(
                Candidate(
                    road=road,
                    side=side,
                    arc_m=s,
                    x=px,
                    y=py,
                    curb_bearing_deg=_normal_bearing_deg(tx, ty, side),
                    offset_m=offset,
                )
            )
            s += step_m

    return out
