"""Turn judged candidates into the ranked, capped, deduplicated response.

Ordering is fully deterministic. Ties are broken down to ``spot_id`` rather than
left to dict or set iteration order, because ENDPOINT.md section 6 S1 step 7 only
says "deduplicate within 5 m ... and return the nearest 30" without saying which
survives. Non-deterministic output would mean identical requests returning
different spots, which breaks caching, makes the recorded demo unreproducible,
and makes the tests flaky.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from shared.config import (
    DEFAULT_RADIUS_M,
    DEDUPE_RADIUS_M,
    DETOUR_FACTOR,
    DETOUR_FACTOR_ACROSS_STREET,
    MAX_SPOTS,
    SETTINGS,
)
from shared.geo import LocalFrame
from shared.models import Confidence, CurbAccess, LatLng, LegalityBasis, Spot, SpotType

from . import curb_access, lots
from .legality import Verdict
from .lots import LotStop
from .network import Kerb, StreetNetwork

#: Higher is better, for the dedupe tie-break. The ordering itself lives on
#: ``Confidence.rank`` in shared/models.py: it is a property of the tier, not of
#: this module, and a second copy here is free to drift.
_CONFIDENCE_RANK = {c: c.rank for c in Confidence}


@dataclass
class ScoredCandidate:
    """A candidate with its derived rider-facing numbers."""

    verdict: Verdict
    walk_distance_m: float
    straight_distance_m: float
    across_street: bool

    @property
    def rank_key(self) -> tuple[int, float, str]:
        """Dedupe preference: confidence, then clearance, then a stable tiebreak."""
        return (
            -_CONFIDENCE_RANK[self.verdict.confidence],
            -(self.verdict.clearance_m if self.verdict.clearance_m >= 0 else 0.0),
            self.verdict.candidate.road.way_id,
        )


def is_across_street(frame: LocalFrame, rider_xy: tuple[float, float], sc: ScoredCandidate) -> bool:
    """Is the stop point on the far side of the road from the rider?

    Reaching a spot across the street means crossing a live carriageway, which
    is materially harder than walking along your own side -- especially with a
    cane or walker. ENDPOINT.md section 6 S1 step 5 applies one flat 1.3 detour
    factor to everything, which quietly under-prices exactly the curb a
    mobility-needs rider would rather not be asked to cross to.
    """
    cand = sc.verdict.candidate
    arc, _ = cand.road.polyline.nearest_arc(rider_xy[0], rider_xy[1])
    signed = cand.road.polyline.signed_offset(arc, rider_xy[0], rider_xy[1])
    rider_side = "left" if signed > 0 else "right"
    return rider_side != cand.side


def walk_distance(
    frame: LocalFrame, rider: LatLng, straight_m: float, across: bool
) -> float:
    factor = DETOUR_FACTOR_ACROSS_STREET if across else DETOUR_FACTOR
    return round(straight_m * factor, 1)


def dedupe(scored: list[ScoredCandidate], radius_m: float = DEDUPE_RADIUS_M) -> list[ScoredCandidate]:
    """Collapse candidates that are effectively the same stopping place.

    Sampling every 10 m along a curb means duplicates mostly arise across parallel
    streets, a lot and its street, or the two sides of a divided road. We keep the
    best of each cluster -- highest confidence, then the most clearance from any
    restriction, then the shortest walk. Preferring clearance is the safety-
    conscious choice: of two spots 6 m apart, the one 9 m from the hydrant is the
    better one to offer.
    """
    kept: list[ScoredCandidate] = []
    for sc in sorted(scored, key=lambda s: s.rank_key):
        cand = sc.verdict.candidate
        clash = False
        for k in kept:
            kc = k.verdict.candidate
            if math.hypot(cand.x - kc.x, cand.y - kc.y) < radius_m:
                clash = True
                break
        if not clash:
            kept.append(sc)
    return kept


def to_spots(
    frame: LocalFrame,
    ordered: list[ScoredCandidate],
    prefix: str = "s1",
    kerbs: Sequence[Kerb] = (),
) -> list[Spot]:
    """Build the wire model, assigning the sequential ids ENDPOINT.md shows.

    Ids are assigned after sorting and capping, so they are stable for a given
    rider location and match the documented ``s1_0042`` shape. Curb access is
    assessed here, after the cap, so it costs 30 lookups rather than thousands.
    """
    spots: list[Spot] = []
    for i, sc in enumerate(ordered, start=1):
        cand = sc.verdict.candidate
        road = cand.road
        lat, lng = frame.to_ll(cand.x, cand.y)
        access, ramp_m, access_source = curb_access.assess(cand, list(kerbs))

        notes: list[str] = []
        if not road.width_known:
            notes.append("road width estimated (no width or lanes tag)")
        if sc.across_street:
            notes.append("across the street from the rider")
        if sc.verdict.legality and sc.verdict.legality.basis == LegalityBasis.INFERRED_STANDARD:
            notes.append("legality inferred; no parking restriction is mapped")
        if access is CurbAccess.UNKNOWN:
            notes.append("no curb ramp or flush curb mapped near this spot")

        spots.append(
            Spot(
                spot_id=f"{prefix}_{i:04d}",
                stop_point=LatLng(lat=round(lat, 6), lng=round(lng, 6)),
                street_name=road.name or road.ref,
                side=cand.side,  # type: ignore[arg-type]
                curb_bearing_deg=round(cand.curb_bearing_deg, 1),
                spot_type=SpotType.CURB,
                walk_distance_m=sc.walk_distance_m,
                source="osm",
                confidence=sc.verdict.confidence,
                notes=notes,
                segment_id=road.way_id,
                clearance_m=(
                    round(sc.verdict.clearance_m, 1) if sc.verdict.clearance_m >= 0 else None
                ),
                legality_basis=(
                    sc.verdict.legality.basis
                    if sc.verdict.legality
                    else LegalityBasis.UNKNOWN
                ),
                curb_access=access,
                ramp_distance_m=ramp_m,
                curb_access_source=access_source,
            )
        )
    return spots


def rank(
    frame: LocalFrame,
    rider: LatLng,
    scored: list[ScoredCandidate],
    radius_m: float = DEFAULT_RADIUS_M,
    max_spots: int = MAX_SPOTS,
    kerbs: Sequence[Kerb] = (),
    lot_stops: Sequence[LotStop] = (),
    network: StreetNetwork | None = None,
) -> tuple[list[Spot], int, bool]:
    """Deduplicate, sort, cap. Returns ``(spots, total_before_cap, truncated)``.

    Kerb candidates and parking-lot stops compete on walk distance for the same
    ``max_spots`` slots, and ids are assigned over the merged order.
    """
    # Overpass `around:` returns ways with any node in range, so a road can
    # contribute candidates well outside the requested radius. Filter explicitly.
    rx, ry = frame.to_m(rider.lat, rider.lng)
    in_radius = [
        sc
        for sc in scored
        if math.hypot(sc.verdict.candidate.x - rx, sc.verdict.candidate.y - ry) <= radius_m
    ]

    deduped = dedupe(in_radius)
    merged: list[tuple[float, int, str, object]] = [
        (sc.walk_distance_m, 0, sc.verdict.candidate.road.way_id, sc) for sc in deduped
    ]
    if network is not None:
        merged += [(ls.walk_m, 1, ls.lot_id, ls) for ls in lot_stops]
    merged.sort(key=lambda t: t[:3])
    total = len(merged)
    capped = merged[:max_spots]

    spots: list[Spot] = []
    for i, (_, kind, _, item) in enumerate(capped, start=1):
        sid = f"s1_{i:04d}"
        if kind == 0:
            spots.append(to_spots(frame, [item], kerbs=kerbs)[0].model_copy(update={"spot_id": sid}))
        else:
            spots.append(lots.to_spot(frame, network, item, sid))
    return spots, total, total > len(capped)


__all__ = [
    "ScoredCandidate",
    "is_across_street",
    "walk_distance",
    "dedupe",
    "to_spots",
    "rank",
    "SETTINGS",
]
