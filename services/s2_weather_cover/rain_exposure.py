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

import polyline
import shapely
from shapely.geometry import LineString, Point

from shared.config import SETTINGS
from shared.geo import LocalFrame
from shared.models import LatLng, RankedSpot, Spot

from . import walk_network
from .cover import Cover

XY = tuple[float, float]


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
# The graph, its connectors and the search live in ``walk_network.py``, which
# every weather mode now routes over. Re-exported here so this module's API --
# ``build_network`` and ``dry_geometry`` -- is unchanged for its callers.

Network = walk_network.Network
build_network = walk_network.build_network
dry_geometry = walk_network.dry_geometry


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

def score(wet_m: float, dry_m: float, penalty_m: float = 0.0) -> float:
    """``1 / (1 + cost / scale)``. ``penalty_m`` is the route's accessibility
    cost (steps, unramped crossings), in metres, as ``walk_network`` charges it."""
    cost = wet_m + SETTINGS.exposure_dry_cost * dry_m + penalty_m
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
    *,
    closed: frozenset[str] = frozenset(),
) -> list[RankedSpot]:
    """Every spot, best (least rain) first. ``closed`` buildings are not walked
    through (see ``walk_network.closed_buildings``)."""
    s = walk_network.search(net, frame.to_m(rider.lat, rider.lng), mode="rain", closed=closed)

    ranked: list[RankedSpot] = []
    for spot in spots:
        stop_xy = frame.to_m(spot.stop_point.lat, spot.stop_point.lng)
        route = walk_network.route_to(net, s, stop_xy)

        if route is None:
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

        line = LineString(route.points)
        walk = line.length
        wait_pt, gap = _wait_point(line, net.dry)
        cover = _cover_at(net, wait_pt) if gap is not None else None
        wait_ll = frame.to_ll(wait_pt.x, wait_pt.y)
        route_ll = [frame.to_ll(x, y) for x, y in route.points]

        ranked.append(RankedSpot(
            spot=spot.model_copy(update={"walk_distance_m": round(walk, 1)}),
            wait_point=LatLng(lat=wait_ll[0], lng=wait_ll[1]),
            cover_feature=cover.as_model() if cover else None,
            gap_m=round(gap, 2) if gap is not None else None,
            score=round(score(route.wet, route.dry, route.penalty), 4),
            confidence=spot.confidence,
            reason=_reason(route.wet, walk, gap, cover),
            wet_m=round(route.wet, 1),
            dry_m=round(route.dry, 1),
            walk_polyline=polyline.encode(route_ll, precision=5),
            indoor_m=round(route.indoor_m, 1),
            route_notes=list(route.notes),
        ))

    ranked.sort(key=lambda r: (-r.score, r.wet_m or 0.0, r.spot.walk_distance_m, r.spot.spot_id))
    return ranked


__all__ = ["Network", "build_network", "dry_geometry", "rank_by_exposure", "score"]
