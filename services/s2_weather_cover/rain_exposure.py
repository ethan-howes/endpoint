"""Rain ranking by exposure: metres walked in the rain, along the route the rider actually walks.

The gap model (``rain_cover.py``) asks one question per spot -- how far is the kerb
from the nearest cover? -- and that question has a blind spot the demo walked
straight into. A rider inside a building whose covered passage runs out to the
road reaches the car almost dry, yet the passage is 11 m from the kerb, so the gap
model scored the spot as uncovered and picked a spot across an open car park
instead. Measured on the committed FIU fixtures for a rider inside the Student
Academic Success Center: the gap model's pick meant 102 m in the rain, the least
exposed legal spot 59 m. For the default demo rider, whose best cover really is at
the kerb, the two models agree to within a metre.

So this module follows the walk:

1. **Dry geometry** is the union of building footprints (indoors is dry) and cover
   features, with covered walkways and passages buffered to their width.
2. **The walking network** (``paths.py``) is split into edges, and each edge knows
   how many of its metres fall inside the dry geometry.
3. **One shortest-path search from the rider** reaches every candidate kerb at
   once, with edge cost ``wet + dry_cost * dry``. Routes therefore *seek* cover --
   through a passage rather than beside it -- which is how a real rider would walk.
4. Each spot is scored on that cost, and carries its wet/dry split, its route,
   and a wait point: the last dry point before the kerb.

Pure: no I/O and no module state. Everything tunable is in ``shared/config.py``.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np
import polyline
import shapely
from shapely.geometry import LineString, Point

from shared.config import SETTINGS
from shared.geo import LocalFrame
from shared.models import LatLng, RankedSpot, Spot

from .cover import Cover, CoverMap, ShadeMap
from .paths import PathMap

XY = tuple[float, float]
_RIDER = -1  # virtual node id for the rider


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #

@dataclass
class Network:
    """The walking graph with every edge already split into wet and dry metres."""

    ids: np.ndarray            # node ids, aligned with xy
    xy: np.ndarray             # (n, 2) projected coordinates
    index: dict[int, int]      # node id -> row in ids/xy
    adj: dict[int, list[tuple[int, float, float]]]  # node -> [(neighbour, length, dry length)]
    dry: object                # shapely geometry (prepared), or None
    entrances: set[int]
    covers: list[Cover]


def dry_geometry(cover_map: CoverMap | None, shade_map: ShadeMap | None):
    """Union of everything a rider can walk under or inside."""
    shapes = []
    half = SETTINGS.cover_path_half_width_m
    for c in (cover_map.covers if cover_map else []):
        g = c.shape
        shapes.append(g.buffer(half) if g.geom_type in ("LineString", "MultiLineString") else g)
    for b in (shade_map.buildings if shade_map else []):
        if b.shape is not None and not b.shape.is_empty:
            shapes.append(b.shape)
    shapes = [s for s in shapes if s is not None and not s.is_empty]
    if not shapes:
        return None
    dry = shapely.union_all(shapes)
    shapely.prepare(dry)
    return dry


def _dry_lengths(segments: list[tuple[XY, XY]], dry) -> np.ndarray:
    """Dry metres of each straight segment, vectorised."""
    if not segments:
        return np.zeros(0)
    if dry is None:
        return np.zeros(len(segments))
    lines = shapely.linestrings(np.array(segments, dtype=float))
    hits = shapely.intersects(dry, lines)
    out = np.zeros(len(segments))
    if hits.any():
        out[hits] = shapely.length(shapely.intersection(lines[hits], dry))
    return out


def build_network(path_map: PathMap, cover_map: CoverMap | None, shade_map: ShadeMap | None) -> Network:
    dry = dry_geometry(cover_map, shade_map)
    points: dict[int, XY] = {}
    pairs: list[tuple[int, int]] = []
    segments: list[tuple[XY, XY]] = []
    covered: list[bool] = []
    seen_edges: set[tuple[int, int]] = set()
    for way in path_map.ways:
        for (a, pa), (b, pb) in zip(
            zip(way.node_ids, way.points), zip(way.node_ids[1:], way.points[1:])
        ):
            if a == b:
                continue
            key = (a, b) if a < b else (b, a)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            points.setdefault(a, pa)
            points.setdefault(b, pb)
            pairs.append((a, b))
            segments.append((pa, pb))
            covered.append(way.covered)

    dry_len = _dry_lengths(segments, dry)
    adj: dict[int, list[tuple[int, float, float]]] = {}
    for (a, b), (pa, pb), d, cov in zip(pairs, segments, dry_len, covered):
        length = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        dry_m = length if cov else min(float(d), length)
        adj.setdefault(a, []).append((b, length, dry_m))
        adj.setdefault(b, []).append((a, length, dry_m))

    ids = np.array(list(points.keys()), dtype=np.int64)
    xy = np.array([points[i] for i in ids], dtype=float).reshape(-1, 2)
    return Network(
        ids=ids, xy=xy, index={int(i): k for k, i in enumerate(ids)}, adj=adj, dry=dry,
        entrances={i for i in path_map.entrances if i in points},
        covers=list(cover_map.covers) if cover_map else [],
    )


def _nearest_nodes(net: Network, at: XY, k: int, radius: float) -> list[int]:
    if len(net.ids) == 0:
        return []
    d = np.hypot(net.xy[:, 0] - at[0], net.xy[:, 1] - at[1])
    order = np.argsort(d)[:k]
    return [int(net.ids[i]) for i in order if d[i] <= radius]


def _connectors(net: Network, at: XY, *, rider: bool) -> list[tuple[int, float, float]]:
    """Straight links from a point to nearby network nodes: (node, length, dry length).

    For the rider, building entrances within reach are always offered as well as
    the nearest nodes: a rider standing inside a building leaves it by a door, and
    the door may not be among the geometrically closest nodes.
    """
    k, radius = SETTINGS.path_connect_k, SETTINGS.path_connect_radius_m
    nodes = _nearest_nodes(net, at, k, radius)
    if rider:
        for e in net.entrances:
            p = net.xy[net.index[e]]
            if math.hypot(p[0] - at[0], p[1] - at[1]) <= radius and e not in nodes:
                nodes.append(e)
    if not nodes:
        return []
    segs = [(at, tuple(net.xy[net.index[n]])) for n in nodes]
    dry = _dry_lengths(segs, net.dry)
    out = []
    for n, (a, b), d in zip(nodes, segs, dry):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        out.append((n, length, min(float(d), length)))
    return out


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

@dataclass
class _Reach:
    cost: float
    wet: float
    dry: float
    prev: int | None


def _search(net: Network, rider_xy: XY) -> dict[int, _Reach]:
    """Cover-seeking shortest paths from the rider to every reachable node."""
    dc = SETTINGS.exposure_dry_cost
    best: dict[int, _Reach] = {}
    heap: list[tuple[float, int]] = []
    for n, length, dry in _connectors(net, rider_xy, rider=True):
        wet = length - dry
        cost = wet + dc * dry
        if n not in best or cost < best[n].cost:
            best[n] = _Reach(cost, wet, dry, _RIDER)
            heapq.heappush(heap, (cost, n))
    done: set[int] = set()
    while heap:
        cost, n = heapq.heappop(heap)
        if n in done:
            continue
        done.add(n)
        here = best[n]
        for m, length, dry in net.adj.get(n, ()):
            wet = length - dry
            c = cost + wet + dc * dry
            if m not in best or c < best[m].cost:
                best[m] = _Reach(c, here.wet + wet, here.dry + dry, n)
                heapq.heappush(heap, (c, m))
    return best


def _route(net: Network, reach: dict[int, _Reach], end: int, rider_xy: XY, stop_xy: XY) -> list[XY]:
    nodes = []
    n: int | None = end
    while n is not None and n != _RIDER:
        nodes.append(n)
        n = reach[n].prev
    nodes.reverse()
    return [rider_xy] + [tuple(net.xy[net.index[i]]) for i in nodes] + [stop_xy]


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

def score(wet_m: float, dry_m: float) -> float:
    cost = wet_m + SETTINGS.exposure_dry_cost * dry_m
    return 1.0 / (1.0 + cost / SETTINGS.exposure_score_scale_m)


def _wait_point(route: LineString, dry) -> tuple[Point, float | None]:
    """The last dry point before the kerb, and the wet metres from there to the kerb."""
    if dry is None:
        return Point(route.coords[-1]), None
    dry_part = route.intersection(dry)
    if dry_part.is_empty:
        return Point(route.coords[-1]), None
    furthest = max(route.project(Point(c)) for c in shapely.get_coordinates(dry_part))
    return route.interpolate(furthest), max(0.0, route.length - furthest)


def _cover_at(net: Network, pt: Point) -> Cover | None:
    reach = SETTINGS.cover_path_half_width_m + 0.5
    best: tuple[float, Cover] | None = None
    for c in net.covers:
        d = c.shape.distance(pt)
        if d <= reach and (best is None or d < best[0]):
            best = (d, c)
    return best[1] if best else None


def _reason(wet: float, walk: float, gap: float | None, cover: Cover | None) -> str:
    if wet < 1.0:
        return f"Covered all the way to the car ({walk:.0f} m walk)"
    text = f"{wet:.0f} m in the rain on a {walk:.0f} m walk"
    if gap is not None:
        # The wait point is the last dry place before the kerb: the rider waits
        # there, and only walks the remaining gap once the car has arrived.
        where = f"under the {cover.kind.replace('_', ' ')}" if cover else "indoors"
        text += f"; wait {where} until the car arrives, then {gap:.0f} m to the car"
    return text


def rank_by_exposure(
    spots: list[Spot],
    rider: LatLng,
    net: Network,
    frame: LocalFrame,
) -> list[RankedSpot]:
    """Every spot, best (least rain) first."""
    rider_xy = frame.to_m(rider.lat, rider.lng)
    reach = _search(net, rider_xy)
    dc = SETTINGS.exposure_dry_cost

    ranked: list[RankedSpot] = []
    for spot in spots:
        stop_xy = frame.to_m(spot.stop_point.lat, spot.stop_point.lng)
        best: tuple[float, int, float, float] | None = None  # cost, node, wet, dry
        for n, length, dry in _connectors(net, stop_xy, rider=False):
            if n not in reach:
                continue
            r = reach[n]
            wet_c = length - dry
            cost = r.cost + wet_c + dc * dry
            if best is None or cost < best[0]:
                best = (cost, n, r.wet + wet_c, r.dry + dry)

        if best is None:
            # Off the network: assume the whole walk is in the rain, which is the
            # conservative answer and ranks the spot below any reachable one.
            walk = spot.walk_distance_m
            ranked.append(RankedSpot(
                spot=spot, wait_point=spot.stop_point, cover_feature=None, gap_m=None,
                score=round(score(walk, 0.0), 4), confidence=spot.confidence,
                reason="No walking route found; assuming the whole walk is in the rain",
                wet_m=round(walk, 1), dry_m=0.0,
            ))
            continue

        _, node, wet, dry = best
        pts = _route(net, reach, node, rider_xy, stop_xy)
        line = LineString(pts)
        walk = line.length
        wait_pt, gap = _wait_point(line, net.dry)
        cover = _cover_at(net, wait_pt) if gap is not None else None
        wait_ll = frame.to_ll(wait_pt.x, wait_pt.y)
        route_ll = [frame.to_ll(x, y) for x, y in pts]

        ranked.append(RankedSpot(
            spot=spot.model_copy(update={"walk_distance_m": round(walk, 1)}),
            wait_point=LatLng(lat=wait_ll[0], lng=wait_ll[1]),
            cover_feature=cover.as_model() if cover else None,
            gap_m=round(gap, 2) if gap is not None else None,
            score=round(score(wet, dry), 4),
            confidence=spot.confidence,
            reason=_reason(wet, walk, gap, cover),
            wet_m=round(wet, 1),
            dry_m=round(dry, 1),
            walk_polyline=polyline.encode(route_ll, precision=5),
        ))

    ranked.sort(key=lambda r: (-r.score, r.wet_m or 0.0, r.spot.walk_distance_m, r.spot.spot_id))
    return ranked


__all__ = ["Network", "build_network", "dry_geometry", "rank_by_exposure", "score"]
