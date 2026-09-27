"""The street network S1 works against, and the seam for future regulation sources.

Two ideas carry the design here:

1. **Ingestion is separated from derivation.** This module plus ``overpass.py``
   do I/O and produce a frozen ``StreetNetwork``. ``curb.py``, ``legality.py``
   and ``ranking.py`` are pure functions over that value with no I/O at all, so
   the legality rules are unit-testable without a network and, if ingestion ever
   earns its own service, it can split out without a contract change.

2. **Restrictions are typed by geometry, not uniformly buffered.** A fire hydrant
   is a point obstacle and a disc is the right exclusion. A crosswalk is not: a
   6 m disc around a crossing node also deletes curb on the cross street and
   around the corner, and the Miami demo area has 786 crossings in 3 km2, so
   uniform discs empty the map. Crossings and bus stops get a linear extent
   measured ALONG the roadway instead. See ``Restriction.covers_along``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from shapely.geometry import LineString, Point

from shared.config import (
    BUFFER_BUS_STOP_M,
    BUFFER_CROSSWALK_M,
    BUFFER_FIRE_HYDRANT_M,
    BUFFER_INTERSECTION_M,
    BUFFER_STOP_SIGN_M,
    BUFFER_TRAFFIC_SIGNAL_M,
)
from shared.geo import LocalFrame, Polyline
from shared.models import RestrictionKind


# --------------------------------------------------------------------------- #
# Roads
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Road:
    """One OSM way we might stop on, in projected meters.

    ``nodes`` is the OSM way's ordered node id list. We keep it (rather than only
    the geometry) because a node id appearing in two or more ways is exactly how
    an intersection is identified, and ENDPOINT.md section 6 S1's own query never
    fetches it -- the rule as written is not implementable from the query as
    written. ``out geom`` happens to return both, so this costs nothing.
    """

    way_id: str
    polyline: Polyline
    highway: str
    name: str | None
    ref: str | None
    oneway: bool
    #: ``oneway=-1``: legal travel runs opposite to the way's digitization
    #: direction, which flips which side a vehicle may stop on.
    oneway_reversed: bool
    #: Offset from the centerline to each side's curb, in meters.
    left_offset_m: float
    right_offset_m: float
    width_known: bool
    lanes: int | None
    #: Ordered OSM node ids, index-aligned with the polyline vertices. Empty when
    #: the source did not return them, which disables intersection detection.
    nodes: tuple[str, ...] = ()
    tags: dict[str, str] = field(default_factory=dict)

    def offset_for(self, side: str) -> float:
        return self.left_offset_m if side == "left" else self.right_offset_m

    def is_two_way(self) -> bool:
        return not self.oneway


# --------------------------------------------------------------------------- #
# Restrictions
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Restriction:
    """Something that makes a curb unusable.

    ``arc_m`` is the along-roadway extent, measured from ``anchor_m`` on
    ``road_key``. It is ``None`` for pure point obstacles. Keeping both in one
    type means ``legality.py`` has a single thing to iterate, regardless of
    whether the source today is OSM or tomorrow is a CDS Curbs API.
    """

    kind: RestrictionKind
    source_id: str
    #: Disc radius, for point obstacles.
    buffer_m: float
    #: Road this restriction is measured along, for linear obstacles.
    road_key: str | None = None
    arc_m: float | None = None
    anchor_m: float | None = None
    #: Restricts one side only, or both.
    side: str | None = None
    label: str = ""

    def covers_along(self, road_key: str, arc_m: float, side: str) -> bool:
        """Does this restriction block a stop at ``arc_m`` on ``road_key``?"""
        if self.side is not None and self.side != side:
            return False
        if self.road_key is None or self.arc_m is None or self.anchor_m is None:
            return False
        if road_key != self.road_key:
            return False
        return abs(arc_m - self.anchor_m) <= self.arc_m

    def covers_point(self, px: float, py: float) -> bool:
        """Does this point obstacle contain the point?"""
        if self.road_key is not None or self._xy is None:
            return False
        return math.hypot(px - self._xy[0], py - self._xy[1]) <= self.buffer_m

    # Populated for point obstacles only; kept out of __init__ so the frozen
    # dataclass stays hashable and cheap.
    _xy: tuple[float, float] | None = None

    def with_xy(self, xy: tuple[float, float]) -> "Restriction":
        return Restriction(
            kind=self.kind,
            source_id=self.source_id,
            buffer_m=self.buffer_m,
            road_key=self.road_key,
            arc_m=self.arc_m,
            anchor_m=self.anchor_m,
            side=self.side,
            label=self.label,
            _xy=xy,
        )


# --------------------------------------------------------------------------- #
# Parking lots
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ParkingLot:
    """An ``amenity=parking`` polygon.

    Access matters: the real data has lots tagged ``access=customers``, and a
    robotaxi picking up a non-customer there is a legality bug, not a nitpick.
    ``permitted`` is pre-resolved during ingestion so ``legality.py`` stays pure.
    """

    lot_id: str
    shape: LineString  # closed ring, in meters
    name: str | None
    permitted: bool
    reason: str = ""


# --------------------------------------------------------------------------- #
# Kerbs
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Kerb:
    """A mapped kerb: where the sidewalk meets the roadway, and how high it is.

    ``on_centerline`` marks the older tagging, ``kerb=*`` placed on a
    ``highway=crossing`` node in the middle of the road. It describes both ends
    of the crossing, so it applies to both sides rather than to whichever side a
    float's sign happens to put it on.
    """

    node_id: str
    kind: str  # "flush" | "lowered" | "raised"
    x: float
    y: float
    on_centerline: bool = False


# --------------------------------------------------------------------------- #
# The network document
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StreetNetwork:
    """Immutable snapshot of the OSM data for one bbox. Produced by ingestion,
    consumed by pure derivation functions."""

    frame: LocalFrame
    bbox: tuple[float, float, float, float]
    roads: list[Road] = field(default_factory=list)
    restrictions: list[Restriction] = field(default_factory=list)
    lots: list[ParkingLot] = field(default_factory=list)
    kerbs: list[Kerb] = field(default_factory=list)
    source: str = "osm"
    generated_at: float = 0.0
    endpoint: str = "unknown"
    #: The cache entry this document came from, so the request path can check
    #: freshness without recomputing tile and query strings.
    cache_id: str = ""

    def road_by_key(self, key: str) -> Road | None:
        return self._road_index().get(key)

    def _road_index(self) -> dict[str, Road]:
        idx = getattr(self, "_road_idx_cache", None)
        if idx is None:
            idx = {r.way_id: r for r in self.roads}
            object.__setattr__(self, "_road_idx_cache", idx)
        return idx

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {
            "roads": len(self.roads),
            "restrictions": len(self.restrictions),
            "parking_lots": len(self.lots),
        }
        for r in self.restrictions:
            counts[r.kind.value] = counts.get(r.kind.value, 0) + 1
        counts["lots_permitted"] = sum(1 for l in self.lots if l.permitted)
        counts["kerbs"] = len(self.kerbs)
        return counts

    def point_restrictions(self) -> list[Restriction]:
        return [r for r in self.restrictions if r.road_key is None]

    def arc_restrictions(self) -> list[Restriction]:
        return [r for r in self.restrictions if r.road_key is not None]


# --------------------------------------------------------------------------- #
# Regulation sources
# --------------------------------------------------------------------------- #

@runtime_checkable
class RegulationSource(Protocol):
    """A source of legal stopping constraints.

    Today the only implementation is ``OsmRegulationSource`` (in ``overpass.py``).
    The seam exists because official regulation data was investigated and is NOT
    available for the Miami demo area: Miami-Dade County is an OMF "SMART Curb"
    showcase city, but that programme is a commercial-freight initiative with no
    public endpoint, and no Miami open data portal publishes a curb regulation
    layer. When a CDS Curbs API, a CurbLR feed, or a city GIS layer appears, it
    becomes a second ``RegulationSource`` producing the same ``Restriction`` and
    ``ParkingLot`` types -- ``legality.py`` and everything downstream stay
    untouched.

    ``tile`` is the cache unit and ``radius_m`` says how much beyond it the
    document must reach, so an implementation can decide its own cache
    granularity without the service layer knowing how it fetches.
    """

    name: str

    async def fetch(
        self, tile: tuple[float, float, float, float], radius_m: float
    ) -> "StreetNetwork":
        ...


__all__ = [
    "Road",
    "Restriction",
    "ParkingLot",
    "Kerb",
    "StreetNetwork",
    "RegulationSource",
    "BUFFER_FIRE_HYDRANT_M",
    "BUFFER_CROSSWALK_M",
    "BUFFER_INTERSECTION_M",
    "BUFFER_STOP_SIGN_M",
    "BUFFER_TRAFFIC_SIGNAL_M",
    "BUFFER_BUS_STOP_M",
    "Point",
]
