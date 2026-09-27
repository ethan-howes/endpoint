"""Cover features and shade-casting geometry: raw Overpass JSON -> frozen maps.

The S1 ingestion boundary, for the data S2 cares about. Same two ideas as
``services/s1_legal_spots/overpass.py``, deliberately:

1. **Ingestion is separated from derivation.** This module does I/O and produces
   frozen values. ``rain_cover`` and ``sun_shade`` are pure functions over them,
   so the ranking rules are testable without a network.

2. **A source protocol, not a hard-coded dependency.** ``OsmCoverSource`` is the
   only implementation today, for the same reason S1 has ``OsmRegulationSource``:
   the preferred data -- a municipal shelter dataset, a transit agency CSV -- is
   not published for the demo area, and this is the seam it arrives through.

Two documents, two cache entries, two queries. Cover features are small and
needed in rain mode; buildings are large and needed only in sun mode. Folding
them into one query would mean every rain call paid for 52 building polygons it
would never look at, and the two would be forced to share a cache lifetime.

What the demo area actually contains, measured rather than assumed (see
``scripts/probe_cover_tags.py``): 60 ``covered=yes`` ways, 77
``tunnel=building_passage`` ways, 3 ``amenity=shelter``, and **zero** awnings or
canopies. So the rain demo rests entirely on covered walkways and building
passages, and the `awning` kind in the shared model is currently unexercised.
That is worth knowing before the definition-of-done is read as a bug.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import unary_union

from shared.config import SETTINGS
from shared.fixtures import cache_key
from shared.geo import LocalFrame, expanded_query_bbox, tile_cache_id
from shared.models import Confidence, CoverFeature
from shared.osm_cache import afetch_overpass, cache_info

FeatureKind = Literal[
    "awning", "canopy", "covered_walkway", "shelter", "building_passage",
]

#: A cover feature's kind, from its tags. Ordered by specificity, because several
#: of these can be true of the same element and the more specific one is the more
#: useful description: a ``covered=yes`` footway that is also
#: ``tunnel=building_passage`` is a passage, and saying "covered walkway" would
#: lose the fact that it is enclosed on both sides.
_KIND_RULES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    # (kind, tag predicate, provides_sun)
    ("building_passage", re.compile(r'^building_passage$'), True),
    ("shelter", re.compile(r'^shelter$'), True),
    ("awning", re.compile(r'^awning$'), True),
    ("canopy", re.compile(r'^canopy$'), True),
    ("covered_walkway", re.compile(r'^covered_walkway$'), True),
)


def _classify(tags: dict[str, str]) -> tuple[str, bool] | None:
    """(kind, provides_sun) for a tagged element, or None if it is not cover.

    Written as an explicit ladder rather than a dict of tag-value lookups because
    the classification genuinely is a precedence question, and burying that in a
    dict comprehension is how it silently becomes "last match wins".
    """
    if tags.get("tunnel") == "building_passage":
        return "building_passage", True
    if tags.get("man_made") == "awning":
        return "awning", True
    if tags.get("man_made") == "canopy":
        return "canopy", True
    if tags.get("amenity") == "shelter":
        return "shelter", True
    if tags.get("shelter") == "yes" and tags.get("highway") == "bus_stop":
        return "shelter", True
    if tags.get("public_transport") == "platform" and tags.get("covered") == "yes":
        return "covered_walkway", True
    if tags.get("covered") in ("yes", "arcade") and tags.get("highway"):
        return "covered_walkway", True
    return None


# --------------------------------------------------------------------------- #
# Cover features
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Cover:
    """Something overhead, in projected meters.

    The geometry is what it is in OSM: a way is a LineString (a covered walkway or
    a building passage -- being on the way *is* being covered), a node is a Point
    (a bus-stop shelter, buffered to a plausible footprint at ingest because
    distance-to-a-point would over-report the gap by half the shelter's length),
    and a closed way is a Polygon (a shelter or platform with a mapped outline).
    """

    feature_id: str
    kind: FeatureKind
    shape: Any  # LineString | Polygon
    provides_sun: bool
    confidence: Confidence = Confidence.UNVERIFIED
    name: str | None = None

    def covers_point(self, x: float, y: float) -> bool:
        return self.shape.distance(Point(x, y)) <= SETTINGS.cover_gap_free_m

    def as_model(self) -> CoverFeature:
        """The shared contract shape, for the response and the map overlay."""
        return CoverFeature(
            feature_id=self.feature_id,
            kind=self.kind,
            geometry_wkt=self.shape.wkt,
            provides=["rain", "sun"] if self.provides_sun else ["rain"],
            source="osm",
            confidence=self.confidence,
        )


@dataclass(frozen=True)
class CoverMap:
    """Immutable snapshot of everything overhead in one bbox."""

    frame: LocalFrame
    bbox: tuple[float, float, float, float]
    covers: list[Cover] = field(default_factory=list)
    source: str = "osm"
    generated_at: float = 0.0
    cache_id: str = ""

    def for_condition(self, sun: bool) -> list[Cover]:
        return [c for c in self.covers if c.provides_sun or not sun]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.covers:
            counts[c.kind] = counts.get(c.kind, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
# Shade-casting geometry
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ShadeBlock:
    """A building or a tree, with the height needed to cast a shadow from it.

    ``height_estimated`` is carried rather than smoothed over, because it is the
    difference between "this building is 6 m because somebody tagged it" and
    "this building is 6 m because that is the default". In the demo area 3 of 52
    buildings carry ``building:levels``, so the honest answer is mostly the
    default, and a shadow cast from a guessed height can be confidently wrong in
    the wrong direction -- over a kerb that is actually in full sun.
    """

    block_id: str
    shape: Any  # Polygon for buildings, Point for trees
    height_m: float
    kind: Literal["building", "tree"]
    crown_r: float | None = None
    height_estimated: bool = True
    # --- buildings only, for walking through them (walk_network.py) ---
    #: The ``building=*`` value: "university", "dormitory", "yes", ...
    building: str = ""
    name: str | None = None
    opening_hours: str = ""
    access: str = ""
    #: OSM node ids of the outline, so a mapped entrance on it is a door.
    node_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.kind == "tree" and self.crown_r is None:
            object.__setattr__(self, "crown_r", SETTINGS.default_tree_crown_m)


@dataclass(frozen=True)
class ShadeMap:
    """Buildings and trees for one bbox."""

    frame: LocalFrame
    bbox: tuple[float, float, float, float]
    blocks: list[ShadeBlock] = field(default_factory=list)
    source: str = "osm"
    generated_at: float = 0.0
    cache_id: str = ""

    @property
    def buildings(self) -> list[ShadeBlock]:
        return [b for b in self.blocks if b.kind == "building"]

    @property
    def trees(self) -> list[ShadeBlock]:
        return [b for b in self.blocks if b.kind == "tree"]

    def summary(self) -> dict[str, int]:
        est = sum(1 for b in self.blocks if b.height_estimated)
        return {
            "blocks": len(self.blocks),
            "buildings": len(self.buildings),
            "trees": len(self.trees),
            "heights_estimated": est,
        }


# --------------------------------------------------------------------------- #
# Source protocol
# --------------------------------------------------------------------------- #

@runtime_checkable
class CoverSource(Protocol):
    """Where overhead protection comes from.

    Same shape as S1's ``RegulationSource`` and for the same reason: the
    authoritative source (a city shelter dataset, a transit agency feed) does not
    exist for the demo area, and this is the seam it arrives through without
    ``rain_cover.py`` or ``sun_shade.py`` changing.
    """

    name: str

    async def fetch_covers(
        self, tile: tuple[float, float, float, float], radius_m: float
    ) -> CoverMap: ...

    async def fetch_shade(
        self, tile: tuple[float, float, float, float], radius_m: float
    ) -> ShadeMap: ...


# --------------------------------------------------------------------------- #
# Query construction
# --------------------------------------------------------------------------- #

def build_cover_query(bbox: tuple[float, float, float, float]) -> str:
    """The config query with its bbox filled in.

    A ``{bbox}`` placeholder rather than string surgery on a multi-statement
    query. The first version tried to split the config string on its last ``;``
    to find the selector union, and silently put the bbox on ``out body geom``
    instead -- a query that is syntactically valid, returns zero elements, and
    looks exactly like an area with no cover. Overpass QL fails by being empty
    rather than by raising, so the placeholder is the only version where a
    mistake is visible in the string.
    """
    s, w, n, e = bbox
    return SETTINGS.cover_query.strip().format(bbox=f"{s},{w},{n},{e}")


def build_shade_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    return SETTINGS.shade_query.strip().format(bbox=f"{s},{w},{n},{e}")


# --------------------------------------------------------------------------- #
# Element parsing
# --------------------------------------------------------------------------- #

def _element_lines(el: dict[str, Any]) -> list[tuple[float, float]] | None:
    """Coordinates for a way/relation, in the order given.

    Prefers ``geometry`` over ``nodes``: with ``out geom`` Overpass returns
    ``nodes`` as bare ids, which would need a second round-trip to resolve, and
    ``geometry`` already has the resolved ``lat``/``lon``. Both are present for
    ways; only ``geometry`` is reliable for relations.
    """
    geom = el.get("geometry")
    if isinstance(geom, list) and len(geom) >= 2:
        pts = [(float(g["lon"]), float(g["lat"])) for g in geom if g.get("lat") is not None]
        if len(pts) >= 2:
            return pts
    return None


def _height_m(tags: dict[str, str]) -> tuple[float, bool]:
    """Building height, and whether it was measured or assumed.

    ENDPOINT.md's order: ``height`` tag, else ``building:levels * 3 + 1``, else
    6 m. Kept as written, with one guard: ``height`` values like ``12;15`` or
    ``approx 12`` appear in real data and must not be fed to ``float()``, because
    a crash here loses every building in the bbox, not just the malformed one.
    """
    raw = tags.get("height")
    if raw:
        m = re.match(r"^\s*([\d.]+)", raw)
        if m:
            return float(m.group(1)), False

    levels = tags.get("building:levels")
    if levels:
        m = re.match(r"^\s*([\d.]+)", levels)
        if m:
            return float(m.group(1)) * SETTINGS.default_floor_height_m + 1.0, False

    return SETTINGS.default_building_height_m, True


def _crown_radius(tags: dict[str, str]) -> float | None:
    raw = tags.get("diameter_crown")
    if raw:
        m = re.match(r"^\s*([\d.]+)", raw)
        if m:
            return float(m.group(1)) / 2.0
    return None


# --------------------------------------------------------------------------- #
# The OSM implementation
# --------------------------------------------------------------------------- #

def parse_covers(
    tile: tuple[float, float, float, float],
    radius_m: float,
    payload: dict[str, Any],
    cache_id: str,
    frame: LocalFrame | None = None,
) -> CoverMap:
    """Overpass JSON -> a ``CoverMap``. No I/O, so it runs on a cache hit or a
    fixture exactly as it does on a live response.

    Separated from ``fetch_covers`` for the same reason S1's ``parse_network`` is:
    the three ways a payload arrives (live, disk cache, committed fixture) must
    produce the same document, and a parser that lives inside the fetcher is a
    parser the cache path cannot reach.

    ``frame`` defaults to the tile's own, which is right for a single tile.
    **Pass one explicitly when merging several tiles.** ``LocalFrame.to_m``
    returns *absolute* UTM easting/northing and uses its anchor only to select
    the EPSG, so two anchors inside one zone produce bit-identical coordinates
    and threading a single frame is a no-op numerically. That is the reassuring
    half. The other half is what the single frame buys: across a UTM zone
    boundary the two frames do not differ by a small offset, they differ by
    600 km (measured: the same point is 800 934 E in zone 17N and 199 066 E in
    zone 18N, an apparent 601 869 m away), so merging per-tile frames produces
    cover hundreds of kilometres from the spots it is supposed to be ranking.
    Miami is nowhere near a boundary, so the demo area never hits this -- which
    is exactly why it needs a single frame rather than a warning.
    """
    query_bbox = expanded_query_bbox(tile, radius_m)
    if frame is None:
        frame = LocalFrame(*_bbox_centre(query_bbox))
    covers: list[Cover] = []

    for el in payload.get("elements") or []:
        tags = el.get("tags") or {}
        verdict = _classify(tags)
        if verdict is None:
            continue
        kind, provides_sun = verdict

        geom = _geometry_in(frame, el)
        if geom is None:
            continue

        covers.append(
            Cover(
                feature_id=f"osm_{el.get('type')}_{el.get('id')}",
                kind=kind,  # type: ignore[arg-type]
                shape=geom,
                provides_sun=provides_sun,
                # Every OSM cover feature is unverified: there is no official
                # shelter register for the demo area to check them against.
                # Marking them `likely` would overstate what a tag means.
                confidence=Confidence.UNVERIFIED,
                name=tags.get("name") or tags.get("ref"),
            )
        )

    return CoverMap(
        frame=frame,
        bbox=query_bbox,
        covers=covers,
        generated_at=time.time(),
        cache_id=cache_id,
    )


def parse_shade(
    tile: tuple[float, float, float, float],
    radius_m: float,
    payload: dict[str, Any],
    cache_id: str,
    frame: LocalFrame | None = None,
) -> ShadeMap:
    query_bbox = expanded_query_bbox(tile, radius_m)
    if frame is None:
        frame = LocalFrame(*_bbox_centre(query_bbox))
    blocks: list[ShadeBlock] = []

    for el in payload.get("elements") or []:
        tags = el.get("tags") or {}
        el_id = f"osm_{el.get('type')}_{el.get('id')}"

        if tags.get("natural") in ("tree", "tree_row"):
            # Nodes only, and the query asks for nodes only. `tree_shadow` reads
            # `shape.x`/`shape.y` as a single trunk, so a way here would raise
            # inside the ranking rather than cast a shadow -- and the blanket
            # handler in `rank` would report it as "sun mode failed", pointing at
            # the wrong layer entirely. Skipping explicitly keeps a future
            # `tree_row` selector from being added without noticing this.
            if el.get("type") != "node":
                continue
            pt = _node_in(frame, el)
            if pt is None:
                continue
            height = tags.get("height")
            try:
                h = float(height) if height else SETTINGS.default_tree_height_m
            except (TypeError, ValueError):
                h = SETTINGS.default_tree_height_m
            blocks.append(
                ShadeBlock(
                    block_id=el_id, shape=pt, height_m=h, kind="tree",
                    crown_r=_crown_radius(tags), height_estimated=height is None,
                )
            )
            continue

        if not tags.get("building"):
            continue
        # A building mapped as an unclosed way is a LineString, which casts no
        # usable shadow footprint; a polygon does. Keeping the geometry honest
        # about which it is means the shading code can reject a zero-area block
        # for what it is -- an unclosed mapping -- rather than for a mystery.
        geom = _geometry_in(frame, el)
        if geom is None or getattr(geom, "area", 0.0) <= 0.0:
            continue
        h, est = _height_m(tags)
        blocks.append(
            ShadeBlock(
                block_id=el_id, shape=geom, height_m=h, kind="building",
                height_estimated=est,
                building=(tags.get("building") or "").strip().lower(),
                name=tags.get("name"),
                opening_hours=(tags.get("opening_hours") or "").strip(),
                access=(tags.get("access") or "").strip().lower(),
                node_ids=tuple(int(n) for n in (el.get("nodes") or [])),
            )
        )

    return ShadeMap(
        frame=frame,
        bbox=query_bbox,
        blocks=blocks,
        generated_at=time.time(),
        cache_id=cache_id,
    )


class OsmCoverSource:
    """OSM via Overpass, cached on disk by ``shared/osm_cache``.

    Ingestion only. Everything about *what the cover is worth* lives in
    ``scoring.py`` and the two ranking modules, and nothing here is allowed to
    make a judgement -- which is why no threshold appears in this file.
    """

    name = "osm"

    async def fetch_covers(
        self,
        tile: tuple[float, float, float, float],
        radius_m: float,
        *,
        frame: LocalFrame | None = None,
        use_cache: bool = True,
        store: bool = True,
    ) -> CoverMap:
        query_bbox = expanded_query_bbox(tile, radius_m)
        cache_id = tile_cache_id(*_bbox_centre(tile), radius_m, "cover")
        payload = await afetch_overpass(
            build_cover_query(query_bbox), cache_key("s2", "cover", cache_id),
            use_cache=use_cache, store=store,
        )
        return parse_covers(tile, radius_m, payload, cache_id, frame)

    async def fetch_shade(
        self,
        tile: tuple[float, float, float, float],
        radius_m: float,
        *,
        frame: LocalFrame | None = None,
        use_cache: bool = True,
        store: bool = True,
    ) -> ShadeMap:
        query_bbox = expanded_query_bbox(tile, radius_m)
        cache_id = tile_cache_id(*_bbox_centre(tile), radius_m, "shade")
        payload = await afetch_overpass(
            build_shade_query(query_bbox), cache_key("s2", "shade", cache_id),
            use_cache=use_cache, store=store,
        )
        return parse_shade(tile, radius_m, payload, cache_id, frame)


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

def _bbox_centre(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    """Centre of a bbox. Doubles as the interior point for ``tile_cache_id``.

    A tile's edges are rounded multiples of its size, so the centre is too, and
    flooring it lands back in the same tile. An edge-adjacent point would not --
    ``tile_cache_id`` re-derives the tile from whatever point it is given, so a
    point on the far edge writes the document under the *neighbour's* key and the
    cache appears to work while serving the wrong area.
    """
    s, w, n, e = bbox
    return ((s + n) / 2.0, (w + e) / 2.0)


def _node_in(frame: LocalFrame, el: dict[str, Any]) -> Point | None:
    if el.get("lat") is None or el.get("lon") is None:
        return None
    x, y = frame.to_m(float(el["lat"]), float(el["lon"]))
    return Point(x, y)


def _geometry_in(frame: LocalFrame, el: dict[str, Any]) -> LineString | Polygon | None:
    """Project an element into the local frame, degrading node -> disc and
    open way -> line.

    Three cases, decided by the geometry rather than by the element type:

    - a **node** has no area, so it is buffered to a disc. A bus-stop shelter
      tagged on a node is the common case; left as a bare point, ``distance`` to
      it measures to the shelter's centre, so a rider standing at its edge is
      reported as 2 m further from cover than they are -- and with
      ``cover_gap_free_m`` at 1.5 m that is the difference between "under the
      awning" and "not quite".
    - a **closed way** is an area: a shelter or platform with a mapped outline,
      a building passage drawn as a ring. Polygonal, because that is what it is.
    - an **open way** is a line, and must stay one. ``way["highway"]["covered"]``
      is a covered walkway -- being *on* the way is being covered -- so its
      geometry is the centreline, not an area. `shapely.Polygon` on two points
      yields an empty polygon, and the element then silently disappears.

    That last distinction is not a detail. Measured over the captured demo-area
    responses, 529 of 547 classified cover features are open ways and only 18 are
    closed, so deciding by element type -- which passes ``is_area=True`` for
    every way -- discarded 97 % of the cover, including all 100 ``covered=yes``
    highways. The rain ranking was scoring against an almost empty map and the
    demo showed nothing. Ring closure is the thing that actually distinguishes
    them, and it is the one property OSM guarantees.
    """
    if el.get("type") == "node":
        pt = _node_in(frame, el)
        if pt is None:
            return None
        return pt.buffer(SETTINGS.node_cover_radius_m)

    lines = _element_lines(el)
    if not lines:
        return None
    pts = [frame.to_m(lat, lng) for lng, lat in lines]
    if len(pts) < 2:
        return None

    if not _is_closed_ring(pts):
        return LineString(pts)

    try:
        poly = Polygon(pts)
    except ValueError:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if (not poly.is_empty and poly.area > 0) else None


def _is_closed_ring(pts: list[tuple[float, float]]) -> bool:
    """Whether a projected ring returns to its first point.

    Compared with a tolerance rather than for exact equality: the first and last
    coordinate of a closed way come from the same OSM node, but they arrive as
    two separate ``lat``/``lon`` pairs that have been through a projection, so
    exact equality holds only by luck. Half a millimetre is well below anything
    that matters at 1.5 m cover gaps and comfortably above the float noise.
    """
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    return abs(x0 - x1) <= 1e-3 and abs(y0 - y1) <= 1e-3


def union_shapes(shapes: list[Any]) -> Any:
    """One geometry from many, or None if there were none.

    ``unary_union`` on an empty list returns an empty GEOMETRYCOLLECTION, which
    answers ``contains`` and ``intersects`` sensibly but has a ``wkt`` of
    ``GEOMETRYCOLLECTION EMPTY`` -- fine to hand to a map, useless for the
    ``shade_geojson`` overlay a UI will try to render. Normalised to None so
    callers have one thing to check.
    """
    real = [s for s in shapes if s is not None and not s.is_empty]
    if not real:
        return None
    return unary_union(real)


__all__ = [
    "Cover", "CoverMap", "CoverSource", "OsmCoverSource",
    "ShadeBlock", "ShadeMap", "build_cover_query", "build_shade_query",
    "union_shapes",
]
