"""The walking network every S2 ranking routes over: accessible, and door-aware.

Grown out of ``rain_exposure.py``'s graph, which already split each edge into wet
and dry metres. This module adds what a rider with a walker or a cane needs from
a route, in every weather mode, not just rain:

**Buildings are walked through by their doors, and only by their doors.**
Doors are mapped ``entrance=*`` nodes, or the end of a footway, path or flight of
steps that stops at a wall (an inferred door -- 41 of 67 named FIU buildings have
no mapped entrance). Doors of the same building are joined by indoor edges,
straight line x ``indoor_detour_factor``. Everything else about a building is a
wall.

**Buildings keep hours.** An indoor edge exists only while its building is open:
its ``opening_hours`` tag when present and parseable, else
``building_default_hours`` (7:00-22:00 local). Closed buildings are passed to the
search per request, so the graph itself stays time-independent and cacheable.

**Homes and residence halls are never walked through** (``NO_WALKTHROUGH_BUILDINGS``),
but a rider standing inside any building, open or not, may always leave by its
doors.

**Accessible route costs.** Steps, crossings with a raised or unmapped kerb at an
end, and unpaved surfaces add a penalty in metres to the route cost, so routes
avoid them and the spots whose best route still has one rank lower.

**Points snap onto the nearest path, not onto distant nodes.** The rider and each
stop join the graph at their projection onto the nearest walkable segments, split
there. The earlier graph joined them by straight links to any node within 60 m,
and those links jumped flights of steps and cut across roads without a crossing,
so none of the costs above could apply. A snap may not cross a building wall.

Pure: no I/O. Everything tunable is in ``shared/config.py``.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, NamedTuple

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from shared.config import (
    NO_WALKTHROUGH_BUILDINGS,
    OPEN_SIDED_BUILDINGS,
    SETTINGS,
    UNPAVED_SURFACES,
)

from .cover import Cover, CoverMap, ShadeBlock, ShadeMap
from .opening_hours import in_default_window, is_open
from .paths import PathMap

XY = tuple[float, float]
Mode = Literal["rain", "length"]
RIDER = -1  # virtual node id for the rider

#: Way classes whose end at a wall is taken as a door.
_DOOR_WAYS = frozenset({"footway", "path", "steps", "pedestrian", "corridor"})
#: More than this much of a straight link inside a building is "through a wall".
_WALL_TOLERANCE_M = 0.5
#: Cap on doors joined per building, so one heavily mapped hall cannot add
#: thousands of indoor edges. 12 doors is 66 pairs.
_MAX_DOORS = 12
#: How far an isolated door (an entrance on no walkable way) reaches outward.
_DOOR_STUB_RADIUS_M = 30.0
#: A point snaps to the nearest segment and to any others at most this much
#: farther away. Wider, and a rider on one sidewalk snaps to the one across the
#: road, which is a road crossing with no crossing in it.
_SNAP_SLACK_M = 5.0


class Edge(NamedTuple):
    to: int
    length: float
    dry: float
    penalty: float = 0.0
    #: "steps" | "raised_crossing" | "unknown_crossing" | "unpaved" | "indoor" | None
    flag: str | None = None
    way: int | None = None
    #: ``ShadeBlock.block_id`` of the building an indoor edge runs through.
    building: str | None = None


class _Seg(NamedTuple):
    """One undirected walkable segment, for snapping."""

    a: int
    b: int
    pa: XY
    pb: XY
    length: float
    dry: float
    penalty: float
    flag: str | None
    way: int | None


class Link(NamedTuple):
    """How a point joins the graph: to ``node``, via its snap point ``via``."""

    node: int
    length: float
    dry: float
    penalty: float = 0.0
    flag: str | None = None
    way: int | None = None
    via: XY | None = None
    #: The segment snapped to and the fraction along it, for same-segment routes.
    seg: int | None = None
    t: float = 0.0


@dataclass
class Building:
    block_id: str
    shape: object
    name: str | None
    #: Eligible for indoor edges: not residential, not private, enclosed.
    walkthrough: bool
    opening_hours: str
    doors: list[int] = field(default_factory=list)


@dataclass
class Network:
    """The walking graph, with every edge split into wet and dry metres."""

    ids: np.ndarray            # node ids, aligned with xy
    xy: np.ndarray             # (n, 2) projected coordinates
    index: dict[int, int]      # node id -> row in ids/xy
    adj: dict[int, list[Edge]]
    dry: object                # prepared shapely geometry, or None
    entrances: set[int]
    covers: list[Cover]
    #: Prepared union of enclosed building footprints: what a link may not cross.
    walls: object = None
    buildings: dict[str, Building] = field(default_factory=dict)
    segs: list[_Seg] = field(default_factory=list)
    _seg_tree: STRtree | None = None
    _tree: STRtree | None = None
    _tree_ids: list[str] = field(default_factory=list)

    def buildings_containing(self, at: XY) -> list[Building]:
        if self._tree is None:
            return []
        pt = Point(at)
        return [
            self.buildings[self._tree_ids[i]]
            for i in self._tree.query(pt)
            if self.buildings[self._tree_ids[i]].shape.contains(pt)
        ]


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

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


def _overlap_lengths(segments: list[tuple[XY, XY]], geom) -> np.ndarray:
    """Metres of each straight segment inside ``geom``, vectorised."""
    if not segments:
        return np.zeros(0)
    if geom is None:
        return np.zeros(len(segments))
    lines = shapely.linestrings(np.array(segments, dtype=float))
    hits = shapely.intersects(geom, lines)
    out = np.zeros(len(segments))
    if hits.any():
        out[hits] = shapely.length(shapely.intersection(lines[hits], geom))
    return out


def _walls(shade_map: ShadeMap | None):
    shapes = [
        b.shape for b in (shade_map.buildings if shade_map else [])
        if b.building not in OPEN_SIDED_BUILDINGS and b.shape is not None and not b.shape.is_empty
    ]
    if not shapes:
        return None
    walls = shapely.union_all(shapes)
    shapely.prepare(walls)
    return walls


def _walkthrough(b: ShadeBlock) -> bool:
    return b.building not in NO_WALKTHROUGH_BUILDINGS and b.access not in {"private", "no"}


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

def _way_penalty(way, kerbs: dict[int, str]) -> tuple[float, str | None]:
    """Whole-way penalty and its flag. Spread over the way's segments by length,
    so traversing the whole way costs it once."""
    if way.highway == "steps":
        return SETTINGS.steps_penalty_m, "steps"
    if way.footway == "crossing" or way.highway == "crossing":
        kinds = [kerbs[n] for n in way.node_ids if n in kerbs]
        if "raised" in kinds:
            return SETTINGS.raised_crossing_penalty_m, "raised_crossing"
        step_free = sum(1 for k in kinds if k in ("flush", "lowered"))
        if step_free >= 2:
            return 0.0, None
        # One mapped ramp: the other end is unknown.
        share = 0.5 if step_free == 1 else 1.0
        return SETTINGS.unknown_crossing_penalty_m * share, "unknown_crossing"
    return 0.0, None


def _assign_doors(path_map: PathMap, buildings: dict[str, Building], blocks: dict[str, ShadeBlock],
                  points: dict[int, XY]) -> None:
    """Attach mapped and inferred doors to the buildings whose wall they sit on."""
    ids = list(buildings)
    if not ids:
        return
    tree = STRtree([buildings[i].shape for i in ids])
    snap = SETTINGS.door_snap_m

    candidates: dict[int, XY] = dict(path_map.entrances)
    for w in path_map.ways:
        if w.highway in _DOOR_WAYS:
            for k in (0, -1):
                candidates.setdefault(w.node_ids[k], w.points[k])

    outline_owner: dict[int, list[str]] = {}
    for bid, blk in blocks.items():
        for n in blk.node_ids:
            outline_owner.setdefault(n, []).append(bid)

    for nid, xy in candidates.items():
        owners = set(outline_owner.get(nid, []) if nid in path_map.entrances else [])
        pt = Point(xy)
        for i in tree.query(pt.buffer(snap)):
            b = buildings[ids[i]]
            if b.shape.boundary.distance(pt) <= snap:
                owners.add(b.block_id)
        for bid in owners:
            if bid in buildings and nid not in buildings[bid].doors:
                buildings[bid].doors.append(nid)
                points.setdefault(nid, xy)


def _add_edge(adj, segs: list[_Seg], a: int, b: int, pa: XY, pb: XY, dry: float,
              penalty: float = 0.0, flag: str | None = None, way: int | None = None) -> None:
    length = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
    adj.setdefault(a, []).append(Edge(b, length, dry, penalty, flag, way))
    adj.setdefault(b, []).append(Edge(a, length, dry, penalty, flag, way))
    segs.append(_Seg(a, b, pa, pb, length, dry, penalty, flag, way))


def build_network(path_map: PathMap, cover_map: CoverMap | None, shade_map: ShadeMap | None) -> Network:
    dry = dry_geometry(cover_map, shade_map)
    points: dict[int, XY] = {}
    rows: list[tuple[int, int, XY, XY, float, str | None, int]] = []  # a, b, pa, pb, penalty, flag, way
    covered: list[bool] = []
    seen_edges: set[tuple[int, int]] = set()

    for way in path_map.ways:
        segs = []
        for (a, pa), (b, pb) in zip(
            zip(way.node_ids, way.points), zip(way.node_ids[1:], way.points[1:])
        ):
            if a == b:
                continue
            key = (a, b) if a < b else (b, a)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            segs.append((a, b, pa, pb, math.hypot(pb[0] - pa[0], pb[1] - pa[1])))
        if not segs:
            continue
        pen, flag = _way_penalty(way, path_map.kerbs)
        unpaved = way.surface in UNPAVED_SURFACES
        total = sum(s[4] for s in segs) or 1.0
        for a, b, pa, pb, length in segs:
            p = pen * length / total
            f = flag
            if unpaved:
                p += SETTINGS.unpaved_penalty_ratio * length
                f = f or "unpaved"
            points.setdefault(a, pa)
            points.setdefault(b, pb)
            rows.append((a, b, pa, pb, p, f, way.way_id))
            covered.append(way.covered)

    dry_len = _overlap_lengths([(r[2], r[3]) for r in rows], dry)
    adj: dict[int, list[Edge]] = {}
    segments: list[_Seg] = []
    for (a, b, pa, pb, pen, flag, wid), d, cov in zip(rows, dry_len, covered):
        length = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        _add_edge(adj, segments, a, b, pa, pb, length if cov else min(float(d), length), pen, flag, wid)

    # --- buildings, doors, indoor edges ---
    blocks = {b.block_id: b for b in (shade_map.buildings if shade_map else [])
              if b.shape is not None and not b.shape.is_empty and b.shape.geom_type in ("Polygon", "MultiPolygon")}
    buildings = {
        bid: Building(bid, b.shape, b.name, _walkthrough(b), b.opening_hours)
        for bid, b in blocks.items()
    }
    _assign_doors(path_map, buildings, blocks, points)

    factor = SETTINGS.indoor_detour_factor
    levels = path_map.entrance_levels
    for b in buildings.values():
        if not b.walkthrough or len(b.doors) < 2:
            continue
        doors = sorted(b.doors)[:_MAX_DOORS]
        for i, a in enumerate(doors):
            for c in doors[i + 1:]:
                if a in levels and c in levels and levels[a] != levels[c]:
                    continue  # no elevator data: never change floors
                pa, pc = points[a], points[c]
                length = math.hypot(pc[0] - pa[0], pc[1] - pa[1]) * factor
                # Indoor edges are not snapping targets: nobody joins the graph
                # halfway along an imaginary corridor.
                adj.setdefault(a, []).append(Edge(c, length, length, 0.0, "indoor", None, b.block_id))
                adj.setdefault(c, []).append(Edge(a, length, length, 0.0, "indoor", None, b.block_id))

    walls = _walls(shade_map)

    # Isolated doors (an entrance on no walkable way) reach outward to the
    # nearest outdoor nodes, or the building's indoor edges would lead nowhere.
    outdoor = {n for s in segments for n in (s.a, s.b)}
    for b in buildings.values():
        for d in b.doors:
            if d in outdoor:
                continue
            pd = points[d]
            near = sorted(
                (math.hypot(points[n][0] - pd[0], points[n][1] - pd[1]), n) for n in outdoor
            )[:3]
            for dist, n in near:
                if dist > _DOOR_STUB_RADIUS_M:
                    break
                through = _overlap_lengths([(pd, points[n])], walls)[0]
                if through <= _WALL_TOLERANCE_M:
                    _add_edge(adj, segments, d, n, pd, points[n], 0.0)

    ids = np.array(list(points.keys()), dtype=np.int64)
    xy = np.array([points[i] for i in ids], dtype=float).reshape(-1, 2)
    net = Network(
        ids=ids, xy=xy, index={int(i): k for k, i in enumerate(ids)}, adj=adj, dry=dry,
        entrances={i for i in path_map.entrances if i in points},
        covers=list(cover_map.covers) if cover_map else [],
        walls=walls, buildings=buildings, segs=segments,
    )
    if segments:
        net._seg_tree = STRtree([LineString([s.pa, s.pb]) for s in segments])
    if buildings:
        tree_ids = list(buildings)
        net._tree = STRtree([buildings[i].shape for i in tree_ids])
        net._tree_ids = tree_ids
    return net


# --------------------------------------------------------------------------- #
# Joining a point to the graph
# --------------------------------------------------------------------------- #

def _snap_links(net: Network, at: XY, *, allow_walls: bool) -> list[Link]:
    """Links from ``at`` to the ends of the nearest walkable segments.

    Each snapped segment yields two links, one per end, each carrying the
    straight leg to the snap point plus that share of the segment (and of its
    dry metres and penalty).
    """
    if net._seg_tree is None:
        return []
    radius = SETTINGS.path_connect_radius_m
    pt = Point(at)
    cands = []
    for i in net._seg_tree.query(pt.buffer(radius)):
        s = net.segs[int(i)]
        line = LineString([s.pa, s.pb])
        d = line.distance(pt)
        if d <= radius:
            cands.append((d, int(i), line))
    if not cands:
        return []
    cands.sort(key=lambda c: (c[0], c[1]))
    limit = cands[0][0] + _SNAP_SLACK_M
    chosen = [c for c in cands if c[0] <= limit][: SETTINGS.path_connect_k]

    legs = [(at, (lambda p: (p.x, p.y))(line.interpolate(line.project(pt)))) for _, _, line in chosen]
    leg_dry = _overlap_lengths(legs, net.dry)
    leg_wall = _overlap_lengths(legs, net.walls) if not allow_walls else np.zeros(len(legs))

    links: list[Link] = []
    for (d, i, line), (_, via), ldry, lwall in zip(chosen, legs, leg_dry, leg_wall):
        if lwall > _WALL_TOLERANCE_M:
            continue
        s = net.segs[i]
        t = line.project(Point(via)) / s.length if s.length > 0 else 0.0
        ldry = min(float(ldry), d)
        for node, share in ((s.a, t), (s.b, 1.0 - t)):
            links.append(Link(
                node=node, length=d + share * s.length, dry=ldry + share * s.dry,
                penalty=share * s.penalty, flag=s.flag if share > 0 else None,
                way=s.way, via=via, seg=i, t=t,
            ))
    return links


def _links(net: Network, at: XY, *, rider: bool) -> tuple[list[Link], str | None]:
    """How a point joins the graph, and a note when it had to bend the rules.

    A rider inside a building joins at that building's doors, whatever the time
    and whatever the building: they are leaving it, not passing through. Every
    other point snaps onto the nearest walkable segments without crossing a wall.
    If that leaves nothing -- a point hemmed in by walls, or inside a building
    with no doors found -- the wall rule is waived and the note says so.
    """
    inside = net.buildings_containing(at) if rider else []
    for b in inside:
        if b.doors:
            out = []
            for dnode in b.doors:
                p = net.xy[net.index[dnode]]
                length = math.hypot(p[0] - at[0], p[1] - at[1])
                out.append(Link(dnode, length, length))  # indoors: dry
            return out, None

    links = _snap_links(net, at, allow_walls=False)
    if links:
        return links, None
    links = _snap_links(net, at, allow_walls=True)
    if not links:
        return [], None
    note = ("starts inside a building with no mapped doors; the route may cut through its walls"
            if inside else "the route may cut through a building")
    return links, note


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def _cost(mode: Mode, length: float, dry: float, penalty: float) -> float:
    if mode == "rain":
        return (length - dry) + SETTINGS.exposure_dry_cost * dry + penalty
    return length + penalty


class _Reach(NamedTuple):
    cost: float
    prev: int | None
    edge: Edge
    via: XY | None = None


@dataclass
class Search:
    reach: dict[int, _Reach]
    rider_xy: XY
    mode: Mode
    closed: frozenset[str]
    note: str | None = None
    #: The rider's own snap links, for a stop on the same segment.
    rider_links: list[Link] = field(default_factory=list)


def search(net: Network, rider_xy: XY, *, mode: Mode = "rain",
           closed: frozenset[str] = frozenset()) -> Search:
    """Cheapest routes from the rider to every reachable node. Indoor edges of
    ``closed`` buildings are skipped."""
    best: dict[int, _Reach] = {}
    heap: list[tuple[float, int]] = []
    links, note = _links(net, rider_xy, rider=True)
    for lk in links:
        c = _cost(mode, lk.length, lk.dry, lk.penalty)
        if lk.node not in best or c < best[lk.node].cost:
            best[lk.node] = _Reach(c, RIDER, Edge(lk.node, lk.length, lk.dry, lk.penalty, lk.flag, lk.way), lk.via)
            heapq.heappush(heap, (c, lk.node))
    done: set[int] = set()
    while heap:
        cost, n = heapq.heappop(heap)
        if n in done:
            continue
        done.add(n)
        for e in net.adj.get(n, ()):
            if e.building is not None and e.building in closed:
                continue
            c = cost + _cost(mode, e.length, e.dry, e.penalty)
            if e.to not in best or c < best[e.to].cost:
                best[e.to] = _Reach(c, n, e)
                heapq.heappush(heap, (c, e.to))
    return Search(best, rider_xy, mode, closed, note, links)


@dataclass
class Route:
    """One rider-to-stop walk and everything a ranking or a message needs from it."""

    points: list[XY]
    length: float
    wet: float
    dry: float
    indoor_m: float
    penalty: float
    cost: float
    notes: list[str] = field(default_factory=list)
    #: Names (or None) of the buildings walked through, in order, and their ids.
    through: list[str | None] = field(default_factory=list)
    through_ids: list[str] = field(default_factory=list)


def _same_segment(net: Network, s: Search, stop_links: list[Link]) -> tuple[float, list[Edge], list[XY]] | None:
    """Rider and stop snapped to the same segment: walk along it directly."""
    best = None
    rider_by_seg = {lk.seg: lk for lk in s.rider_links if lk.seg is not None}
    for sl in stop_links:
        rl = rider_by_seg.get(sl.seg)
        if rl is None or rl.via is None or sl.via is None:
            continue
        seg = net.segs[sl.seg]
        share = abs(rl.t - sl.t)
        r_leg = math.hypot(rl.via[0] - s.rider_xy[0], rl.via[1] - s.rider_xy[1])
        edge = Edge(-3, r_leg + share * seg.length, share * seg.dry, share * seg.penalty,
                    seg.flag if share > 0 else None, seg.way)
        cost = _cost(s.mode, edge.length, edge.dry, edge.penalty)
        if best is None or cost < best[0]:
            best = (cost, [edge], [s.rider_xy, rl.via, sl.via])
    return best


def route_to(net: Network, s: Search, stop_xy: XY) -> Route | None:
    """The cheapest route to ``stop_xy``, or None when the stop is off the network."""
    links, link_note = _links(net, stop_xy, rider=False)

    best: tuple[float, Link] | None = None
    for lk in links:
        if lk.node not in s.reach:
            continue
        c = s.reach[lk.node].cost + _cost(s.mode, lk.length, lk.dry, lk.penalty)
        if best is None or c < best[0]:
            best = (c, lk)

    direct = _same_segment(net, s, links)
    if best is None and direct is None:
        return None

    if direct is not None and (best is None or direct[0] <= best[0]):
        cost, edges, pts = direct
        last_leg = math.hypot(stop_xy[0] - pts[-1][0], stop_xy[1] - pts[-1][1])
        edges = edges + [Edge(-2, last_leg, 0.0)]
        cost += _cost(s.mode, last_leg, 0.0, 0.0)
        pts = pts + [stop_xy]
    else:
        cost, lk = best
        edges, nodes, first_via = [], [], None
        n: int | None = lk.node
        while n is not None and n != RIDER:
            r = s.reach[n]
            nodes.append(n)
            edges.append(r.edge)
            if r.prev == RIDER:
                first_via = r.via
            n = r.prev
        nodes.reverse()
        edges.reverse()
        edges.append(Edge(-2, lk.length, lk.dry, lk.penalty, lk.flag, lk.way))
        pts = [s.rider_xy] + ([first_via] if first_via else [])
        pts += [tuple(net.xy[net.index[i]]) for i in nodes]
        pts += ([lk.via] if lk.via else []) + [stop_xy]

    length = sum(e.length for e in edges)
    dry = min(sum(e.dry for e in edges), length)
    indoor = sum(e.length for e in edges if e.flag == "indoor")
    penalty = sum(e.penalty for e in edges)

    through: list[str | None] = []
    through_ids: list[str] = []
    for e in edges:
        if e.flag == "indoor" and e.building is not None and e.building not in through_ids:
            through_ids.append(e.building)
            through.append(net.buildings[e.building].name)
    notes = [f"through {name}" if name else "through a campus building" for name in through]
    if any(e.flag == "steps" for e in edges):
        notes.append("route includes steps")
    if any(e.flag == "raised_crossing" for e in edges):
        notes.append("crosses a road at a raised curb")
    unknown = {e.way for e in edges if e.flag == "unknown_crossing"}
    if unknown:
        notes.append(
            f"{len(unknown)} crossing{'s' if len(unknown) > 1 else ''} with no mapped curb ramp"
        )
    if any(e.flag == "unpaved" for e in edges):
        notes.append("part of the route is unpaved")
    for extra in (s.note, link_note):
        if extra:
            notes.append(extra)

    return Route(
        points=pts, length=length, wet=length - dry, dry=dry, indoor_m=indoor,
        penalty=penalty, cost=cost, notes=notes, through=through, through_ids=through_ids,
    )


# --------------------------------------------------------------------------- #
# Hours
# --------------------------------------------------------------------------- #

def closed_buildings(net: Network, local: datetime) -> frozenset[str]:
    """Buildings whose indoor edges are unusable at ``local`` (local wall time)."""
    out = set()
    for b in net.buildings.values():
        if not b.walkthrough or len(b.doors) < 2:
            continue
        open_now = is_open(b.opening_hours, local)
        if open_now is None:
            open_now = in_default_window(local, SETTINGS.building_default_hours)
        if not open_now:
            out.add(b.block_id)
    return frozenset(out)


__all__ = [
    "Building", "Edge", "Link", "Network", "Route", "Search",
    "build_network", "closed_buildings", "dry_geometry", "route_to", "search",
]
