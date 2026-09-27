"""Legality rules: which generated candidates may actually be offered.

Pure functions over a ``StreetNetwork``. No I/O, no network, no config reads
beyond the imported constants -- so every rule here is unit-testable against a
hand-built fixture (see ``tests/test_legality.py``).

Two decisions worth stating explicitly, both made before implementation:

**Untagged curb sides are PERMISSIVE.** Measured on the demo area, explicit
parking side tags are almost absent (6 ``parking:left``, 2 ``parking:right``, 0
``parking:both``, 0 ``parking:lane:*`` across 422 ways), so a restrictive default
would return an empty map. A rider who uses a cane is better served by a spot we
flag ``unverified`` and confirm with vision on arrival than by being told there
is nowhere to stop. The permissiveness is contained by two things: the
``unverified``/``inferred_standard`` labelling, and S3's confirmation on arrival.

**S1 caps at ``likely``.** There is no official regulation feed for the Miami
demo area, so ``Confidence.VERIFIED`` is unreachable from this service by
construction. ``legality.py`` never constructs it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from shared.config import (
    MIN_CLEARANCE_M,
    PERMISSIVE_PARKING_VALUES,
    RESTRICTIVE_PARKING_VALUES,
    SETTINGS,
)
from shared.models import Confidence, LatLng, LegalityBasis, RestrictionKind

from .curb import Candidate
from .network import Road, Restriction, StreetNetwork

#: Precedence order for parking side keys, most specific first. A hit at an
#: earlier level wins outright, so ``parking:left=parallel`` beats a
#: whole-street ``parking:lane:both=no``.
_SIDE_KEY_TEMPLATES: tuple[str, ...] = (
    "parking:{side}",
    "parking:lane:{side}",
)

_BOTH_KEYS: tuple[str, ...] = (
    "parking:both",
    "parking:lane:both",
    "parking",
)

#: ``parking:<side>:restriction`` is a hard override checked before everything.
_RESTRICTION_KEYS: tuple[str, ...] = (
    "parking:{side}:restriction",
    "parking:both:restriction",
    "parking:restriction",
)

_HARD_NO_RESTRICTIONS: frozenset[str] = frozenset({"no_stopping", "no_parking"})


@dataclass(frozen=True)
class SideLegality:
    """The resolved legality of one curb side of one road."""

    verdict: Literal["permissive", "restrictive", "unknown"]
    basis: LegalityBasis
    #: Which tag decided it, for ``/spots/explain`` and rider-facing explanations.
    source_tag: str = ""


def resolve_side_legality(road: Road, side: str) -> SideLegality:
    """Decide whether ``side`` of ``road`` permits stopping for passenger pickup.

    Order:
      1. ``parking:<side>:restriction`` in {no_stopping, no_parking} -> restrictive
      2. ``parking:<side>`` then ``parking:lane:<side>``
      3. whole-street keys (``parking:both``, ``parking:lane:both``, ``parking``)
      4. nothing found -> permissive, but ``unknown`` basis

    Step 4 is the permissive default described in the module docstring. It is
    also why ENDPOINT.md's confidence rule ("if the side has an explicit parking
    tag, mark likely; otherwise unverified") was not sufficient: it fired on
    roughly 1% of segments, making the tier uninformative.
    """
    tags = road.tags

    for template in _RESTRICTION_KEYS:
        for key in (template.format(side=side), template.format(side="both")):
            raw = (tags.get(key) or "").strip().lower()
            if raw in _HARD_NO_RESTRICTIONS:
                return SideLegality("restrictive", LegalityBasis.TAGGED_PERMISSIVE, key)

    for template in _SIDE_KEY_TEMPLATES:
        key = template.format(side=side)
        raw = (tags.get(key) or "").strip().lower()
        if not raw:
            continue
        if raw in RESTRICTIVE_PARKING_VALUES:
            return SideLegality("restrictive", LegalityBasis.TAGGED_PERMISSIVE, key)
        if raw in PERMISSIVE_PARKING_VALUES:
            return SideLegality("permissive", LegalityBasis.TAGGED_PERMISSIVE, key)

    for key in _BOTH_KEYS:
        raw = (tags.get(key) or "").strip().lower()
        if not raw:
            continue
        if raw in RESTRICTIVE_PARKING_VALUES:
            return SideLegality("restrictive", LegalityBasis.TAGGED_PERMISSIVE, key)
        if raw in PERMISSIVE_PARKING_VALUES:
            return SideLegality("permissive", LegalityBasis.TAGGED_PERMISSIVE, key)

    # No parking tag at all. Still ``permissive``, but tagged as an inference
    # rather than a fact, and capped at ``likely`` confidence downstream.
    return SideLegality("permissive", LegalityBasis.INFERRED_STANDARD, "")


def cycleway_blocks(road: Road, side: str) -> str:
    """Return the blocking cycleway tag for ``side``, or "" if none.

    The value classification (which cycleway values actually obstruct a curb)
    lives in ``overpass._cycleway_obstructs`` and is shared with width estimation,
    so the two can never disagree about what counts as an obstruction.
    """
    from .overpass import _cycleway_obstructs  # local import avoids a cycle

    blocked, source_tag = _cycleway_obstructs(road.tags, side, road.oneway)
    return source_tag if blocked else ""


# --------------------------------------------------------------------------- #
# Restriction matching
# --------------------------------------------------------------------------- #

def blocking_restriction(
    candidate: Candidate,
    point_restrictions: list[Restriction],
    arc_restrictions: list[Restriction],
) -> Restriction | None:
    """Return the first restriction that blocks this candidate, if any.

    Arc restrictions are matched by scalar comparison on ``arc_m`` because a
    crossing's exclusion is "within 6 m ALONG this roadway", which is not a
    disc and not a general polygon. Comparing scalars keeps this cheap with
    hundreds of crossings in range.
    """
    for r in arc_restrictions:
        if r.covers_along(candidate.road.way_id, candidate.arc_m, candidate.side):
            return r

    for r in point_restrictions:
        if r.covers_point(candidate.x, candidate.y):
            return r

    return None


def clearance_to_restrictions(
    candidate: Candidate,
    point_restrictions: list[Restriction],
    arc_restrictions: list[Restriction],
) -> float:
    """Distance in meters to the nearest restriction boundary, ignoring sides.

    Reported as ``Spot.clearance_m``. On an ``unverified`` spot this is the most
    useful thing we can tell a rider -- "8 m from the nearest hydrant" -- and it
    is the tie-break used when deduplicating.
    """
    best = float("inf")

    for r in point_restrictions:
        if r._xy is None:
            continue
        d = ((candidate.x - r._xy[0]) ** 2 + (candidate.y - r._xy[1]) ** 2) ** 0.5
        best = min(best, d - r.buffer_m)

    for r in arc_restrictions:
        if r.road_key != candidate.road.way_id or r.anchor_m is None or r.arc_m is None:
            continue
        # A side-specific restriction only bounds the other side.
        if r.side is not None and r.side != candidate.side:
            continue
        along = abs(candidate.arc_m - r.anchor_m) - r.arc_m
        best = min(best, along)

    return best if best != float("inf") else -1.0


@dataclass(frozen=True)
class Verdict:
    """The outcome of judging one candidate."""

    candidate: Candidate
    accepted: bool
    reason: str = ""
    restriction: Restriction | None = None
    legality: SideLegality | None = None
    clearance_m: float = -1.0
    confidence: Confidence = Confidence.UNVERIFIED


def judge_candidate(
    candidate: Candidate,
    point_restrictions: list[Restriction],
    arc_restrictions: list[Restriction],
    traffic_side: str = "right",
) -> Verdict:
    """Apply every legality rule to one candidate.

    Order is cheapest-and-most-decisive first: side legality, then one-way (which
    ``curb.legal_sides`` already applied during generation, so it is re-checked
    here only as a guard), then the cycleway, then physical restrictions, then a
    final clearance margin so nothing survives sitting exactly on a buffer edge.
    """
    road = candidate.road

    legality = resolve_side_legality(road, candidate.side)
    if legality.verdict == "restrictive":
        return Verdict(
            candidate=candidate,
            accepted=False,
            reason=f"curb restricted by {legality.source_tag or 'parking tags'}",
            legality=legality,
        )

    blocked_tag = cycleway_blocks(road, candidate.side)
    if blocked_tag:
        return Verdict(
            candidate=candidate,
            accepted=False,
            reason=f"in-lane cycleway on this side ({blocked_tag})",
            legality=legality,
        )

    hit = blocking_restriction(candidate, point_restrictions, arc_restrictions)
    if hit is not None:
        return Verdict(
            candidate=candidate,
            accepted=False,
            reason=f"within {hit.buffer_m:g} m of {hit.label or hit.kind.value}",
            restriction=hit,
            legality=legality,
        )

    clearance = clearance_to_restrictions(candidate, point_restrictions, arc_restrictions)
    if clearance >= 0.0 and clearance < MIN_CLEARANCE_M:
        return Verdict(
            candidate=candidate,
            accepted=False,
            reason="sits on a restriction buffer edge",
            legality=legality,
            clearance_m=clearance,
        )

    # Confidence: S1 caps at LIKELY. An explicit permissive tag or a strong
    # structural inference earns it; nothing else does. VERIFIED and DETECTED
    # belong to official sources and S3 respectively, and are unreachable here.
    strong_inference = (
        road.width_known
        and road.highway in {"residential", "unclassified", "living_street", "tertiary", "secondary"}
    )
    if legality.basis == LegalityBasis.TAGGED_PERMISSIVE or strong_inference:
        confidence = Confidence.LIKELY
    else:
        confidence = Confidence.UNVERIFIED

    return Verdict(
        candidate=candidate,
        accepted=True,
        reason="",
        legality=legality,
        clearance_m=clearance,
        confidence=confidence,
    )


def explain_restriction_counts(network: StreetNetwork) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in network.restrictions:
        counts[r.kind.value] = counts.get(r.kind.value, 0) + 1
    return counts


__all__ = [
    "SideLegality",
    "Verdict",
    "resolve_side_legality",
    "cycleway_blocks",
    "blocking_restriction",
    "clearance_to_restrictions",
    "judge_candidate",
    "explain_restriction_counts",
    "LatLng",
    "RestrictionKind",
    "SETTINGS",
]
