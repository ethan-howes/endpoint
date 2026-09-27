"""Module 2 (ENDPOINT.md section 6): rank legal spots by how well they keep rain off.

Pure over a ``CoverMap`` -- no I/O, no thresholds of its own. Everything about
what a cover feature is worth lives in ``scoring.py``, so the rain and sun paths
cannot drift apart on the walk penalty or the gap ramp.

The one judgement this module makes is the **wait point**: a rider with mobility
needs does not want the car to stop *near* shelter, they want the car to stop
where they can already stand under it. So a spot is scored on the gap between
where the car stops and where the rider can wait, and the response carries both
points. A spot with excellent cover 8 m from the kerb is a different offer from
one with cover directly overhead, and the rider is the only one who can tell the
difference -- which is why ``RankedSpot`` has two coordinates.
"""

from __future__ import annotations

from shapely.geometry import Point
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from shared.config import SETTINGS
from shared.geo import LocalFrame
from shared.models import LatLng, RankedSpot, Spot

from . import scoring
from .cover import Cover, CoverMap


def _nearest_cover(
    tree: STRtree, geom: Point, covers: list[Cover], max_gap_m: float
) -> tuple[Cover, float, Point] | None:
    """The closest cover feature within ``max_gap_m``, or None.

    An STRtree rather than a linear scan: a 150 m radius in a dense downtown
    produces hundreds of features and dozens of spots, and the naive version is
    O(spots x features) distance computations per request. The tree is built
    once per request, not once per spot.
    """
    # `max_gap_m` is a distance, the tree indexes envelopes, so query a disc of
    # that radius rather than a box -- otherwise a spot near a bbox corner gets
    # filtered out by geometry that was never actually in range.
    hits = tree.query(geom.buffer(max_gap_m))
    if len(hits) == 0:
        return None

    best: tuple[float, Cover] | None = None
    for idx in hits:
        c = covers[idx]
        d = c.shape.distance(geom)
        if best is None or d < best[0] or (d == best[0] and c.feature_id < best[1].feature_id):
            best = (d, c)
    if best is None or best[0] > max_gap_m:
        return None

    d, c = best
    # `shapely.ops.nearest_points(a, b)` returns a pair whose *first* element is
    # the point on `a` and second is the point on `b`. We want the point on the
    # cover, so that is index 1. Index 0 hands back the stop point itself, which
    # is at distance 0 from everything: the gap would measure correctly and the
    # wait point would be the kerb, telling the rider to stand in the rain next
    # to the shelter they were just sent to.
    nearest = nearest_points(geom, c.shape)[1]
    if nearest is None:
        return None
    return c, d, nearest


def rank_spots(
    spots: list[Spot],
    cover_map: CoverMap,
    *,
    max_gap_m: float | None = None,
) -> list[RankedSpot]:
    """Every spot, scored for rain, best first.

    Ordering is ``scoring.rank_key``, shared with the sun path: score, then gap,
    then walk, then id. See there for why the gap term matters.
    """
    max_gap_m = SETTINGS.rain_max_gap_m if max_gap_m is None else max_gap_m
    # Every feature in the map provides rain; `provides_sun` only narrows the
    # *sun* path, so there is nothing to filter here.
    covers = list(cover_map.covers)
    if not covers or not spots:
        return _by_walk(spots, "No cover data available nearby")

    frame: LocalFrame = cover_map.frame
    tree = STRtree([c.shape for c in covers])

    ranked: list[RankedSpot] = []
    for spot in spots:
        px, py = frame.to_m(spot.stop_point.lat, spot.stop_point.lng)
        geom = Point(px, py)

        match = _nearest_cover(tree, geom, covers, max_gap_m)
        if match is None:
            score = scoring.no_cover_score(spot.walk_distance_m)
            ranked.append(
                RankedSpot(
                    spot=spot,
                    wait_point=spot.stop_point,
                    cover_feature=None,
                    gap_m=None,
                    score=round(score, 4),
                    confidence=spot.confidence,
                    reason="No cover found nearby",
                )
            )
            continue

        cover, gap, wait_pt = match
        score = scoring.covered_score(
            gap, cover.confidence, spot.walk_distance_m, max_gap_m
        )
        wait_ll = frame.to_ll(wait_pt.x, wait_pt.y)
        ranked.append(
            RankedSpot(
                spot=spot,
                wait_point=LatLng(lat=wait_ll[0], lng=wait_ll[1]),
                cover_feature=cover.as_model(),
                gap_m=round(gap, 2),
                score=round(score, 4),
                confidence=spot.confidence,
                reason=_reason(cover, gap, spot.walk_distance_m),
            )
        )

    ranked.sort(key=scoring.rank_key)
    return ranked


def _reason(cover: Cover, gap_m: float, walk_m: float) -> str:
    """The sentence a rider reads. Names the thing, not the score.

    "Under cover 8 m away" is actionable. "Score 0.42" is not. The gap is
    rounded to whole metres because a rider cannot act on 4.3 m and a precise
    number implies a precision the OSM tags do not support.
    """
    where = f"under {cover.name}" if cover.name else f"under the {cover.kind.replace('_', ' ')}"
    if gap_m <= SETTINGS.cover_gap_free_m:
        return f"Waiting {where}, right at the pickup point"
    return (
        f"{where} {gap_m:.0f} m from the pickup point, "
        f"{walk_m:.0f} m walk"
    )


def _by_walk(spots: list[Spot], reason: str) -> list[RankedSpot]:
    """Nearest first, all scoring the same. The §6 fallback ranking."""
    out = [
        RankedSpot(
            spot=s,
            wait_point=s.stop_point,
            cover_feature=None,
            gap_m=None,
            score=round(scoring.no_cover_score(s.walk_distance_m), 4),
            confidence=s.confidence,
            reason=reason,
        )
        for s in spots
    ]
    out.sort(key=lambda r: (r.spot.walk_distance_m, r.spot.spot_id))
    return out


__all__ = ["rank_spots"]
