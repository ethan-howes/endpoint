"""Curb access: can a rider get between the sidewalk and the car at this stop?

Pure, like the rest of derivation: a candidate and the network's kerbs in, a
``CurbAccess`` out. Nothing here decides legality -- a spot with a raised kerb is
still a legal place to stop -- it only reports what a rider with a walker or a
cane will meet at the car door, so S2 can rank by it and the rider can be told.

The rule, in order:

1. Consider kerbs within ``CURB_RAMP_MAX_DISTANCE_M`` of the stop point on the
   **same side of the road**. A ramp across the street is no help: reaching it
   means crossing the road the car is stopped on.
2. The nearest flush or lowered kerb wins, and its distance is reported.
3. Otherwise a raised kerb in range makes the spot ``raised``.
4. Otherwise ``unknown`` -- nothing is mapped, which is not the same as no ramp.
"""

from __future__ import annotations

import math

from shared.config import CURB_RAMP_MAX_DISTANCE_M
from shared.models import CurbAccess

from .curb import Candidate
from .network import Kerb

#: A kerb this close to the centreline cannot be assigned a side by its offset
#: sign, which float noise decides. Real ``barrier=kerb`` nodes sit at the road
#: edge, metres out.
_MIN_SIDE_OFFSET_M = 1.0

_STEP_FREE = {"flush": CurbAccess.FLUSH, "lowered": CurbAccess.LOWERED}


def _same_side(candidate: Candidate, kerb: Kerb) -> bool:
    if kerb.on_centerline:
        return True  # tags both ends of its crossing
    poly = candidate.road.polyline
    arc, _ = poly.nearest_arc(kerb.x, kerb.y)
    offset = poly.signed_offset(arc, kerb.x, kerb.y)
    if abs(offset) < _MIN_SIDE_OFFSET_M:
        return False
    return ("left" if offset > 0 else "right") == candidate.side


def assess(
    candidate: Candidate,
    kerbs: list[Kerb],
    max_distance_m: float = CURB_RAMP_MAX_DISTANCE_M,
) -> tuple[CurbAccess, float | None, str | None]:
    """``(curb_access, distance_m, source)`` for one stop point."""
    best_free: tuple[float, Kerb] | None = None
    best_raised: float | None = None
    for k in kerbs:
        d = math.hypot(k.x - candidate.x, k.y - candidate.y)
        if d > max_distance_m or not _same_side(candidate, k):
            continue
        if k.kind in _STEP_FREE:
            if best_free is None or d < best_free[0]:
                best_free = (d, k)
        elif best_raised is None or d < best_raised:
            best_raised = d

    if best_free is not None:
        d, k = best_free
        return _STEP_FREE[k.kind], round(d, 1), "osm"
    if best_raised is not None:
        return CurbAccess.RAISED, round(best_raised, 1), "osm"
    return CurbAccess.UNKNOWN, None, None


__all__ = ["assess"]
