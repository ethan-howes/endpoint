"""Overpass ingestion: raw OSM JSON -> a frozen ``StreetNetwork``.

This is the only I/O boundary in S1. Everything downstream is a pure function
over the resulting ``StreetNetwork``.

Two queries, not one. The road query is deliberately narrow -- it asks for
highways and nothing else. Buildings, trees and cover features belong to S2, and
pulling them in here would make S1's cached document large and blur ownership of
the data. Splitting also means the road document, which is needed on every
request, is cached and refreshed independently of the point features.
"""

from __future__ import annotations


import re
import time
from typing import Any

from shapely.geometry import LineString

from shared.config import (
    BUFFER_BUS_STOP_M,
    BUFFER_CROSSWALK_M,
    BUFFER_FIRE_HYDRANT_M,
    BUFFER_INTERSECTION_M,
    BUFFER_STOP_SIGN_M,
    BUFFER_TRAFFIC_SIGNAL_M,
    CYCLEWAY_WIDTH_M,
    DEFAULT_WIDTH_BY_HIGHWAY,
    IN_LANE_CYCLEWAY_VALUES,
    KERB_VALUE_KIND,
    NO_STOP_SERVICE_VALUES,
    ON_KERB_PARKING_VALUES,
    VEHICLE_ACCESS_ALLOW,
    VEHICLE_ACCESS_DENY,
    VEHICLE_ACCESS_KEYS,
    LANE_WIDTH_M,
    NON_OBSTRUCTING_CYCLEWAY_VALUES,
    PARKING_LANE_WIDTH_M,
    PERMISSIVE_PARKING_VALUES,
    RESTRICTIVE_PARKING_VALUES,
    SETTINGS,
    STOPPABLE_HIGHWAY_CLASSES,
)
from shared.geo import LocalFrame, Polyline, expanded_query_bbox, tile_cache_id
from shared.models import RestrictionKind
from shared.osm_cache import afetch_overpass, cache_info

from .network import Kerb, ParkingLot, Restriction, Road, StreetNetwork

#: Highways worth fetching. Wider than ENDPOINT.md section 6 S1's list because a
#: campus pickup often happens on a service road or the edge of a pedestrian
#: plaza, but still excludes motorways/trunk links where stopping is impossible.
ROAD_QUERY_FILTER = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential"
    "|living_street|service|pedestrian|footway|path|cycleway|track"
)

_WIDTH_RANGE_RE = re.compile(r"^\s*([\d.]+)\s*-\s*([\d.]+)\s*$")


# --------------------------------------------------------------------------- #
# Query construction
# --------------------------------------------------------------------------- #

def build_road_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    return (
        f"[out:json][timeout:60];\n"
        f'way["highway"~"^({ROAD_QUERY_FILTER})$"]({s},{w},{n},{e});\n'
        f"out body geom;"
    )


def build_point_query(bbox: tuple[float, float, float, float]) -> str:
    """Restrictions, parking, and the kerb nodes that decide ``Spot.curb_access``.

    Kerbs are ``barrier=kerb`` nodes, usually carrying ``kerb=lowered|flush|raised``;
    a bare ``kerb=*`` on a crossing node is the older form of the same fact, so
    both are selected. Accessible parking spaces are fetched here too, as their
    own polygons, because they are the one place a mapped kerb-free pickup is
    guaranteed by the tag rather than inferred.
    """
    s, w, n, e = bbox
    return (
        f"[out:json][timeout:60];\n"
        "(\n"
        f'  node["emergency"="fire_hydrant"]({s},{w},{n},{e});\n'
        f'  node["highway"~"^(crossing|bus_stop|traffic_signals|stop)$"]({s},{w},{n},{e});\n'
        f'  way["amenity"="parking"]({s},{w},{n},{e});\n'
        f'  node["barrier"="kerb"]({s},{w},{n},{e});\n'
        f'  node["kerb"]({s},{w},{n},{e});\n'
        f'  way["amenity"="parking_space"]["parking_space"="disabled"]({s},{w},{n},{e});\n'
        ");\n"
        f"out body geom;"
    )


# --------------------------------------------------------------------------- #
# Tag interpretation
# --------------------------------------------------------------------------- #

def parse_width(raw: str | None) -> float | None:
    """Parse an OSM ``width`` tag. Handles ranges like ``"3.5-4"`` by midpoint.

    Measured on the Miami demo area: present on 0 of 422 ways. Kept because
    other cities tag it, but it is never the primary width signal.
    """
    if not raw:
        return None
    raw = raw.strip().rstrip(";").strip()
    m = _WIDTH_RANGE_RE.match(raw)
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2.0
    try:
        v = float(raw.split(";")[0])
    except ValueError:
        return None
    return v if 0.5 <= v <= 60.0 else None


def parse_lanes(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        v = int(raw.strip().split(";")[0])
    except ValueError:
        return None
    return v if 1 <= v <= 20 else None


def parse_oneway(tags: dict[str, str]) -> tuple[bool, bool]:
    """Return ``(is_oneway, is_reversed)``.

    ``oneway=-1`` means the legal direction of travel is OPPOSITE to the way's
    digitization direction, which flips which side of the street a vehicle may
    stop on. Getting this wrong on a one-way puts the car in oncoming traffic.
    """
    raw = (tags.get("oneway") or "").strip().lower()
    if raw in {"yes", "true", "1"}:
        return True, False
    if raw in {"-1", "reverse"}:
        return True, True
    if raw in {"no", "false", "0"}:
        return False, False
    if raw in {"alternating", "reversible"}:
        # Time-dependent; treat as one-way in the way's own direction, which is
        # the common case, and never as two-way (that would allow stopping on
        # the wrong side half the time).
        return True, False
    if (tags.get("junction") or "").strip().lower() in {"roundabout", "circular"}:
        return True, False
    return False, False


def _cycleway_obstructs(tags: dict[str, str], side: str, oneway: bool) -> tuple[bool, str]:
    """Does an in-lane cycleway sit on ``side``, making that curb unstappable?

    ENDPOINT.md section 6 S1 says "exclude that side of the road" for any
    ``cycleway=*lane`` tag. That is too broad in two ways that matter:

    * ``opposite`` / ``opposite_lane`` belongs to the OTHER carriageway of a
      divided road, so it does not touch our curb at all.
    * ``separate`` is an off-roadway path.

    Both are common on exactly the wide, busy roads where deleting the curb
    would leave the rider with nowhere to go. Treating them as obstructions
    silently removes legal stopping places.
    """
    side_key = f"cycleway:{side}"
    raw = (tags.get(side_key) or "").strip().lower()
    source = side_key

    if not raw:
        both = (tags.get("cycleway:both") or "").strip().lower()
        bare = (tags.get("cycleway") or "").strip().lower()
        if both and both not in NON_OBSTRUCTING_CYCLEWAY_VALUES:
            raw, source = both, "cycleway:both"
        elif bare:
            if bare in NON_OBSTRUCTING_CYCLEWAY_VALUES:
                return False, ""
            # A bare `cycleway=lane` on a ONE-WAY road is conventionally the
            # right-hand side in right-hand traffic. On a two-way street the
            # standard reading is both sides.
            if oneway:
                if side == "right":
                    raw, source = bare, "cycleway"
                else:
                    return False, ""
            else:
                raw, source = bare, "cycleway"

    if not raw:
        return False, ""
    if raw in NON_OBSTRUCTING_CYCLEWAY_VALUES:
        return False, ""
    if raw in IN_LANE_CYCLEWAY_VALUES or raw == "shared":
        return True, source
    return False, ""


def _has_parking_lane(tags: dict[str, str], side: str) -> bool:
    """A parking lane in the roadway on ``side``, which moves that kerb outward.
    On-kerb parking sits on the footway and does not."""
    for key in (f"parking:lane:{side}", f"parking:{side}"):
        raw = (tags.get(key) or "").strip().lower()
        if raw in PERMISSIVE_PARKING_VALUES and raw not in ON_KERB_PARKING_VALUES:
            return True
    return False


def road_stoppable(tags: dict[str, str]) -> tuple[bool, str]:
    """May a robotaxi stop on this road to pick up a member of the public?

    ``(False, reason)`` for a pedestrian street or plaza (unless tagged open to
    cars), a drive-through, emergency route or car-park aisle, or a road whose
    vehicle access excludes the public. Access is read from the most specific
    key down, so ``access=private`` + ``motor_vehicle=destination`` is allowed.
    """
    highway = (tags.get("highway") or "").strip().lower()
    if (tags.get("area") or "").strip().lower() == "yes":
        return False, "area=yes (a plaza, not a street)"
    service = (tags.get("service") or "").strip().lower()
    if service in NO_STOP_SERVICE_VALUES:
        return False, f"service={service}"
    for key in VEHICLE_ACCESS_KEYS:
        raw = (tags.get(key) or "").strip().lower()
        if raw in VEHICLE_ACCESS_DENY:
            return False, f"{key}={raw}"
        if raw in VEHICLE_ACCESS_ALLOW:
            return True, ""
    if highway == "pedestrian":
        return False, "highway=pedestrian with no motor-vehicle access"
    return True, ""


def estimate_offsets(tags: dict[str, str]) -> tuple[float, float, bool]:
    """Return ``(left_offset_m, right_offset_m, width_known)``.

    Per side, not a flat half-width. A bike lane or parking lane on one side
    pushes that curb outward while the other side stays put, and ``curb_bearing_deg``
    (which S3 uses to aim the camera) is derived from this.

    Precedence: explicit ``width`` -> ``lanes`` -> per-class default. The middle
    option does the real work: ``width`` was absent on 422/422 Miami ways while
    ``lanes`` was present on 376/422. ENDPOINT.md's "else 4 m per side" would put
    the curb in the middle of a travel lane on every multi-lane road.
    """
    highway = (tags.get("highway") or "").strip().lower()
    oneway, _ = parse_oneway(tags)

    travel = parse_width(tags.get("width"))
    width_known = travel is not None
    if travel is None:
        lanes = parse_lanes(tags.get("lanes"))
        if lanes is not None:
            travel = lanes * LANE_WIDTH_M
            width_known = True
        else:
            travel = DEFAULT_WIDTH_BY_HIGHWAY.get(highway, 8.0)
            width_known = False

    base = travel / 2.0
    left = right = base

    if _cycleway_obstructs(tags, "left", oneway)[0]:
        left += CYCLEWAY_WIDTH_M
    if _cycleway_obstructs(tags, "right", oneway)[0]:
        right += CYCLEWAY_WIDTH_M
    if _has_parking_lane(tags, "left"):
        left += PARKING_LANE_WIDTH_M
    if _has_parking_lane(tags, "right"):
        right += PARKING_LANE_WIDTH_M

    return round(left, 3), round(right, 3), width_known


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _clean_coords(geom: list[dict[str, Any]]) -> list[tuple[float, float]] | None:
    """Drop nulls and consecutive duplicates.

    Duplicate consecutive nodes are common in OSM and produce zero-length
    segments, which break arc-length sampling (division by zero) and make a
    segment's tangent undefined.
    """
    pts: list[tuple[float, float]] = []
    for g in geom:
        if g.get("lat") is None or g.get("lon") is None:
            continue
        pt = (float(g["lat"]), float(g["lon"]))
        if pts and pts[-1] == pt:
            continue
        pts.append(pt)
    return pts if len(pts) >= 2 else None


def parse_roads(payload: dict[str, Any], frame: LocalFrame) -> list[Road]:
    roads: list[Road] = []
    for el in payload.get("elements", []):
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        highway = (tags.get("highway") or "").strip().lower()
        if highway not in STOPPABLE_HIGHWAY_CLASSES:
            continue

        coords = _clean_coords(el.get("geometry") or [])
        if coords is None:
            continue
        nodes = el.get("nodes") or []
        # geometry and nodes are index-aligned; if they are not, we cannot trust
        # node ids for intersection detection, so drop the way.
        if len(nodes) != len(coords):
            nodes = []

        try:
            poly = Polyline.from_latlngs(frame, coords)
        except (ValueError, ArithmeticError):
            continue
        if poly.length < 1.0:
            continue

        oneway, reversed_ = parse_oneway(tags)
        left, right, known = estimate_offsets(tags)
        stoppable, why = road_stoppable(tags)

        roads.append(
            Road(
                way_id=f"way/{el['id']}",
                polyline=poly,
                highway=highway,
                name=tags.get("name"),
                ref=tags.get("ref"),
                oneway=oneway,
                oneway_reversed=reversed_,
                left_offset_m=left,
                right_offset_m=right,
                width_known=known,
                lanes=parse_lanes(tags.get("lanes")),
                nodes=tuple(str(n) for n in nodes),
                tags=tags,
                stoppable=stoppable,
                unstoppable_reason=why,
            )
        )
    return roads


def _project_to_network(
    roads: list[Road], px: float, py: float
) -> tuple[Road, float, float] | None:
    """Find the nearest road to a point and the arc length at which it lands."""
    best: tuple[float, Road, float] | None = None
    for road in roads:
        arc, dist = road.polyline.nearest_arc(px, py)
        if best is None or dist < best[0]:
            best = (dist, road, arc)
    if best is None:
        return None
    _, road, arc = best
    return road, arc, best[0]


def parse_restrictions(
    payload: dict[str, Any], frame: LocalFrame, roads: list[Road]
) -> list[Restriction]:
    """Build typed restrictions from point features plus detected intersections.

    Geometry is chosen per kind, which is the whole point:

    * fire hydrant / stop sign / signal -> disc. They are point obstacles and a
      disc is the correct exclusion.
    * crossing -> linear extent along the roadway, BOTH sides. A crosswalk spans
      the road, so blocking the car from either curb is right, but the exclusion
      must be measured along the road: a disc would also delete curb on the
      cross street and around the corner.
    * bus stop -> linear extent along the roadway, ONE side, chosen by which side
      of the centerline the node sits on.
    * intersection -> linear extent along every way that shares the junction node.
    """
    out: list[Restriction] = []

    for el in payload.get("elements", []):
        if el.get("type") != "node":
            continue
        tags = el.get("tags") or {}
        if el.get("lat") is None or el.get("lon") is None:
            continue

        highway = (tags.get("highway") or "").strip().lower()
        emergency = (tags.get("emergency") or "").strip().lower()

        if emergency == "fire_hydrant":
            px, py = frame.to_m(float(el["lat"]), float(el["lon"]))
            out.append(
                Restriction(
                    kind=RestrictionKind.FIRE_HYDRANT,
                    source_id=f"node/{el['id']}",
                    buffer_m=BUFFER_FIRE_HYDRANT_M,
                    label=tags.get("name") or "fire hydrant",
                ).with_xy((px, py))
            )
            continue

        if highway == "stop":
            px, py = frame.to_m(float(el["lat"]), float(el["lon"]))
            out.append(
                Restriction(
                    kind=RestrictionKind.STOP_SIGN,
                    source_id=f"node/{el['id']}",
                    buffer_m=BUFFER_STOP_SIGN_M,
                    label=tags.get("name") or "stop sign",
                ).with_xy((px, py))
            )
            continue

        if highway == "traffic_signals":
            px, py = frame.to_m(float(el["lat"]), float(el["lon"]))
            out.append(
                Restriction(
                    kind=RestrictionKind.TRAFFIC_SIGNAL,
                    source_id=f"node/{el['id']}",
                    buffer_m=BUFFER_TRAFFIC_SIGNAL_M,
                    label="traffic signal",
                ).with_xy((px, py))
            )
            continue

        if highway in {"crossing", "bus_stop"}:
            if not roads:
                continue
            px, py = frame.to_m(float(el["lat"]), float(el["lon"]))
            hit = _project_to_network(roads, px, py)
            if hit is None:
                continue
            road, arc, dist = hit
            # A node projected far onto a road is probably attached to a
            # different way (or a service road); a 30 m leash keeps the linear
            # exclusion from smearing across an entire block.
            if dist > 30.0:
                continue

            if highway == "crossing":
                out.append(
                    Restriction(
                        kind=RestrictionKind.CROSSING,
                        source_id=f"node/{el['id']}",
                        buffer_m=BUFFER_CROSSWALK_M,
                        road_key=road.way_id,
                        arc_m=BUFFER_CROSSWALK_M,
                        anchor_m=arc,
                        side=None,  # a crosswalk blocks both curbs
                        label=tags.get("name") or "crossing",
                    )
                )
            else:
                side = "left" if road.polyline.signed_offset(arc, px, py) > 0 else "right"
                out.append(
                    Restriction(
                        kind=RestrictionKind.BUS_STOP,
                        source_id=f"node/{el['id']}",
                        buffer_m=BUFFER_BUS_STOP_M,
                        road_key=road.way_id,
                        arc_m=BUFFER_BUS_STOP_M,
                        anchor_m=arc,
                        side=side,  # a bus stop only blocks its own curb
                        label=tags.get("name") or "bus stop",
                    )
                )

    out.extend(_detect_intersections(roads))
    return out


def _detect_intersections(roads: list[Road]) -> list[Restriction]:
    """Find junctions as node ids shared by two or more ways.

    ENDPOINT.md section 6 S1 lists "Intersection | node shared by 2+ road ways |
    6 m" as an exclusion, but its own Overpass query never fetches ways' node ids,
    so the rule is not implementable from the query as written. Because
    ``out geom`` returns both ``geometry`` and ``nodes``, the shared-node test
    costs nothing extra and needs no second round trip. Verified on the Miami
    demo area: 390 shared nodes across 422 ways, 130 of them true 3+ way
    junctions.
    """
    node_ways: dict[str, list[Road]] = {}
    for road in roads:
        if not road.nodes:
            continue
        for nid in road.nodes:
            node_ways.setdefault(nid, []).append(road)

    out: list[Restriction] = []
    for nid, ways in node_ways.items():
        if len(ways) < 2:
            continue
        for road in ways:
            try:
                idx = road.nodes.index(nid)
            except ValueError:
                continue
            if idx >= len(road.polyline.cum):
                continue
            arc = road.polyline.cum[idx]
            out.append(
                Restriction(
                    kind=RestrictionKind.INTERSECTION,
                    source_id=f"node/{nid}",
                    buffer_m=BUFFER_INTERSECTION_M,
                    road_key=road.way_id,
                    arc_m=BUFFER_INTERSECTION_M,
                    anchor_m=arc,
                    side=None,
                    label="intersection",
                )
            )
    return out


def parse_kerbs(payload: dict[str, Any], frame: LocalFrame) -> list[Kerb]:
    """Kerb nodes with a known height, in meters.

    A kerb value on a ``highway=crossing`` node is the older tagging for both
    ends of the crossing, and the node sits on the road centreline, so it is
    flagged rather than assigned to a side.
    """
    kerbs: list[Kerb] = []
    for el in payload.get("elements", []):
        if el.get("type") != "node" or el.get("lat") is None or el.get("lon") is None:
            continue
        tags = el.get("tags") or {}
        kind = KERB_VALUE_KIND.get((tags.get("kerb") or "").strip().lower())
        if kind is None:
            continue
        x, y = frame.to_m(float(el["lat"]), float(el["lon"]))
        kerbs.append(Kerb(
            node_id=f"node/{el['id']}",
            kind=kind,
            x=x,
            y=y,
            on_centerline=(tags.get("highway") or "").strip().lower() == "crossing",
        ))
    return kerbs


def parse_accessible_spaces(payload: dict[str, Any], frame: LocalFrame) -> list[tuple[str, Any]]:
    """``parking_space=disabled`` polygons: ``[(way id, Polygon in meters)]``."""
    from shapely.geometry import Polygon

    out = []
    for el in payload.get("elements", []):
        tags = el.get("tags") or {}
        if el.get("type") != "way" or (tags.get("parking_space") or "").strip().lower() != "disabled":
            continue
        coords = _clean_coords(el.get("geometry") or [])
        if coords is None or len(coords) < 3:
            continue
        poly = Polygon([frame.to_m(la, lo) for la, lo in coords]).buffer(0)
        if not poly.is_empty:
            out.append((f"way/{el['id']}", poly))
    return out


#: ``access``-style keys that gate who may use a parking lot.
_LOT_ACCESS_KEYS = ("access", "motor_vehicle", "motorcar", "vehicle")


def parse_lots(payload: dict[str, Any], frame: LocalFrame) -> list[ParkingLot]:
    """Parse ``amenity=parking`` polygons and pre-resolve who may use them.

    ENDPOINT.md section 6 S1 step 4 adds every lot unconditionally. The real data
    has lots tagged ``access=customers``; picking up a non-customer there is a
    legality bug, not a nitpick, so access is resolved here at ingestion and
    ``legality.py`` stays a pure function of the network.
    """
    lots: list[ParkingLot] = []
    for el in payload.get("elements", []):
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        if (tags.get("amenity") or "").strip().lower() != "parking":
            continue
        coords = _clean_coords(el.get("geometry") or [])
        if coords is None:
            continue
        ring = [frame.to_m(la, lo) for la, lo in coords]
        if len(ring) < 3:
            continue
        if ring[0] != ring[-1]:
            ring.append(ring[0])

        permitted, reason = _lot_permitted(tags)
        lots.append(
            ParkingLot(
                lot_id=f"way/{el['id']}",
                shape=LineString(ring),
                name=tags.get("name"),
                permitted=permitted,
                reason=reason,
            )
        )
    return lots


def _lot_permitted(tags: dict[str, str]) -> tuple[bool, str]:
    for key in _LOT_ACCESS_KEYS:
        raw = (tags.get(key) or "").strip().lower()
        if not raw:
            continue
        if raw in RESTRICTIVE_PARKING_VALUES or raw in {"customers_only", "permit_only"}:
            return False, f"{key}={raw}"
        if raw in PERMISSIVE_PARKING_VALUES or raw in {"yes", "public", "destination"}:
            return True, f"{key}={raw}"
    raw = (tags.get("parking") or "").strip().lower()
    if raw == "customers":
        return False, "parking=customers"
    return True, ""


# --------------------------------------------------------------------------- #
# The source
# --------------------------------------------------------------------------- #

class OsmRegulationSource:
    """``RegulationSource`` over OpenStreetMap. The only implementation today.

    A future CDS Curbs API, CurbLR feed, or city GIS curb layer becomes a second
    implementation of the same protocol (see ``network.RegulationSource``),
    producing the same ``Restriction``/``ParkingLot`` types.

    Note the two different boxes in play: the *query* is built over the tile
    expanded by ``radius_m`` so the document always covers the rider's whole
    search circle, while the *cache id* is the unexpanded tile so that every
    rider in the tile shares one cached document and prefetch can predict its
    key exactly.
    """

    name = "osm"

    async def fetch(
        self, tile: tuple[float, float, float, float], radius_m: float
    ) -> StreetNetwork:
        query_bbox = expanded_query_bbox(tile, radius_m)
        frame = LocalFrame(
            (query_bbox[0] + query_bbox[2]) / 2.0, (query_bbox[1] + query_bbox[3]) / 2.0
        )

        road_query = build_road_query(query_bbox)
        point_query = build_point_query(query_bbox)

        roads_id = tile_cache_id(*_tile_center(tile), radius_m, "roads")
        points_id = tile_cache_id(*_tile_center(tile), radius_m, "points")

        road_payload = await afetch_overpass(road_query, roads_id)
        point_payload = await afetch_overpass(point_query, points_id)

        roads = parse_roads(road_payload, frame)
        restrictions = parse_restrictions(point_payload, frame, roads)
        lots = parse_lots(point_payload, frame)

        info = cache_info(road_query, roads_id)
        return StreetNetwork(
            frame=frame,
            bbox=tile,
            roads=roads,
            restrictions=restrictions,
            lots=lots,
            kerbs=parse_kerbs(point_payload, frame),
            accessible_spaces=parse_accessible_spaces(point_payload, frame),
            source=self.name,
            generated_at=time.time(),
            endpoint=info.endpoint if info else SETTINGS.overpass_mirrors[0],
            cache_id=roads_id,
        )


def _tile_center(tile: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
