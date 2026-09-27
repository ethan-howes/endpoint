"""Parking lots as pickup spots (ENDPOINT.md section 6 S1 step 4).

One spot per permitted lot, not one per 10 m of aisle. Aisles are driving lanes
with parked cars on both sides and no kerb, so sampling them as kerbs gave the
wrong label, side and bearing, and at FIU filled 11 of a rider's 30 slots with
near-duplicates of one car park.

Where in the lot the car stops, in order of preference:

1. **An accessible parking space** (``parking_space=disabled``), the one nearest
   the rider. It is the one place where a step-free, at-grade approach is the
   point of the tag, so the spot is ``flush`` and ``likely``.
2. **The nearest point on one of the lot's aisles**, where a car can actually
   stand.
3. **The nearest point on the lot's edge**, when no aisle is mapped (28 of 42
   FIU lots).

Access was resolved at ingestion (``ParkingLot.permitted``): a customers-only or
private lot is never offered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import Point, Polygon

from shared.config import CURB_RAMP_MAX_DISTANCE_M, DETOUR_FACTOR
from shared.geo import LocalFrame
from shared.models import Confidence, CurbAccess, LatLng, LegalityBasis, Spot, SpotType

from .network import StreetNetwork


@dataclass(frozen=True)
class LotStop:
    lot_id: str
    name: str | None
    x: float
    y: float
    walk_m: float
    accessible: bool
    placed_on: str  # "accessible_space" | "aisle" | "edge"
    #: Compass bearing from the stop toward the rider: which way S3 looks.
    bearing_deg: float
    tagged_access: bool


def _bearing(fx: float, fy: float, tx: float, ty: float) -> float:
    return math.degrees(math.atan2(tx - fx, ty - fy)) % 360.0


def lot_stops(network: StreetNetwork, rider_xy: tuple[float, float], radius_m: float) -> list[LotStop]:
    rx, ry = rider_xy
    rider = Point(rider_xy)
    aisles = [r for r in network.roads if (r.tags.get("service") or "") == "parking_aisle"]
    out: list[LotStop] = []
    for lot in network.lots:
        if not lot.permitted:
            continue
        poly = Polygon(lot.shape.coords).buffer(0)
        if poly.is_empty:
            continue

        spaces = [s for _, s in network.accessible_spaces if poly.buffer(2.0).contains(s.centroid)]
        if spaces:
            c = min(spaces, key=lambda s: s.centroid.distance(rider)).centroid
            pt, placed = (c.x, c.y), "accessible_space"
        else:
            inside = [a.polyline.as_shapely().intersection(poly) for a in aisles]
            inside = [g for g in inside if not g.is_empty and g.length > 0]
            if inside:
                best = min(inside, key=lambda g: g.distance(rider))
                p = best.interpolate(best.project(rider))
                pt, placed = (p.x, p.y), "aisle"
            else:
                p = poly.exterior.interpolate(poly.exterior.project(rider))
                pt, placed = (p.x, p.y), "edge"

        straight = math.hypot(pt[0] - rx, pt[1] - ry)
        if straight > radius_m:
            continue
        out.append(LotStop(
            lot_id=lot.lot_id, name=lot.name, x=pt[0], y=pt[1],
            walk_m=round(straight * DETOUR_FACTOR, 1),
            accessible=placed == "accessible_space", placed_on=placed,
            bearing_deg=_bearing(pt[0], pt[1], rx, ry),
            tagged_access=bool(lot.reason),
        ))
    return out


def _nearest_step_free(network: StreetNetwork, x: float, y: float) -> tuple[CurbAccess, float | None]:
    best = None
    for k in network.kerbs:
        if k.kind not in ("flush", "lowered"):
            continue
        d = math.hypot(k.x - x, k.y - y)
        if d <= CURB_RAMP_MAX_DISTANCE_M and (best is None or d < best[0]):
            best = (d, k.kind)
    if best is None:
        return CurbAccess.UNKNOWN, None
    return CurbAccess(best[1]), round(best[0], 1)


def to_spot(frame: LocalFrame, network: StreetNetwork, ls: LotStop, spot_id: str) -> Spot:
    lat, lng = frame.to_ll(ls.x, ls.y)
    notes = [f"inside {ls.name}" if ls.name else "inside a parking lot"]
    if ls.accessible:
        notes.append("at an accessible parking space")
        access, ramp, source = CurbAccess.FLUSH, 0.0, "osm:parking_space=disabled"
    else:
        access, ramp = _nearest_step_free(network, ls.x, ls.y)
        source = "osm" if access is not CurbAccess.UNKNOWN else None
    # An accessible-space tag or an explicit access tag is a fact; an untagged
    # lot open to anyone is an inference.
    tagged = ls.accessible or ls.tagged_access
    return Spot(
        spot_id=spot_id,
        stop_point=LatLng(lat=round(lat, 6), lng=round(lng, 6)),
        street_name=ls.name,
        side=None,
        curb_bearing_deg=round(ls.bearing_deg, 1),
        spot_type=SpotType.PARKING_LOT,
        walk_distance_m=ls.walk_m,
        source="osm",
        confidence=Confidence.LIKELY if tagged else Confidence.UNVERIFIED,
        notes=notes,
        segment_id=ls.lot_id,
        legality_basis=LegalityBasis.TAGGED_PERMISSIVE if tagged else LegalityBasis.INFERRED_STANDARD,
        curb_access=access,
        ramp_distance_m=ramp,
        curb_access_source=source,
    )


__all__ = ["LotStop", "lot_stops", "to_spot"]
