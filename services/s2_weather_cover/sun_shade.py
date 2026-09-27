"""Module 3 (ENDPOINT.md section 6): rank legal spots by shade at pickup time.

The interesting claim this module has to make good on is the doc's own
definition-of-done: *"moving force_time from 10:00 to 16:00 local visibly changes
which side of the street wins, because shadows flip sides."* That is a physical
property of the sun, not a ranking heuristic, and it holds here because shade is
computed as real geometry from a real sun position rather than inferred.

Three things worth knowing about the implementation, all of which cost accuracy:

**Google Solar API is not used.** The doc makes it primary. It needs a Maps key
with Solar API enabled, and without one the honest options are an untested
raster-decoding path or nothing. ``shade_source`` reports ``osm_geometry`` so the
response says which method produced the answer.

**Building heights are mostly guesses.** In the demo area 3 of 52 buildings carry
``building:levels``; the rest fall back to 6 m. A shadow cast from a wrong height
is not a slightly-wrong shadow, it is a confidently-wrong one that can put a spot
in shade that is in full sun. ``ShadeBlock.height_estimated`` carries this all
the way through rather than hiding it, and ``source_weight`` for
``osm_geometry`` (0.6, against 0.9 for Solar) is where it is paid for in the
score.

**Trees are 8 m.** ENDPOINT.md notes Meta/WRI canopy height rasters as the fix
and then uses the default anyway. A single 8 m tree is a 6 m disc of shade at
high sun, which is real but small next to a building.

The module is pure: given a ``ShadeMap``, a ``CoverMap`` and a sun position, it
produces a ranking. No I/O, no clock.
"""

from __future__ import annotations

import math

from shapely.affinity import translate
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points, unary_union

from shared.config import SETTINGS
from shared.geo import LocalFrame
from shared.models import (
    LatLng,
    RankedSpot,
    ShadeSource,
    Spot,
    SunPosition,
)

from . import scoring
from .cover import Cover, CoverMap, ShadeMap


# --------------------------------------------------------------------------- #
# Shadow geometry
# --------------------------------------------------------------------------- #

def shadow_offset(
    height_m: float,
    elev_deg: float,
    az_deg: float,
    cap_m: float = 100.0,
) -> tuple[float, float]:
    """How far a shadow reaches, and in which direction. (dx east, dy north).

    ``ENDPOINT.md`` section 6's formula, kept as written. Two details that are
    easy to get wrong and that the demo depends on:

    - ``az_deg + 180``: shadows point *away* from the sun. Get this backwards and
      the ranking prefers the sunny side at 10am and the shaded side at 4pm, and
      the "shadows flip sides" demo inverts into a demo of shadows that do not
      move.
    - The cap. ``height / tan(elev)`` diverges as the sun approaches the horizon;
      a 20 m building at 5 degrees is a 229 m shadow, which would blanket the
      whole demo area and rank every spot identically. The cap bounds the
      damage, and the elevation check below keeps the low-sun case out of
      scoring entirely.
    """
    elev = max(elev_deg, 0.5)  # avoid tan(0); the low-sun case is handled upstream
    d = min(height_m / math.tan(math.radians(elev)), cap_m)
    ang = math.radians(az_deg + 180.0)
    return d * math.sin(ang), d * math.cos(ang)


def building_shadow(
    footprint: Polygon, height_m: float, elev_deg: float, az_deg: float
) -> Polygon:
    """A building's footprint swept toward its own shadow.

    The union of the two, hulled, rather than a swept quadrilateral: correct for
    L-shaped and U-shaped footprints, and the hull is conservative in the right
    direction -- it can over-cover a courtyard, never under-cover a wall.
    """
    dx, dy = shadow_offset(height_m, elev_deg, az_deg)
    if dx == 0.0 and dy == 0.0:
        return footprint
    return unary_union([footprint, translate(footprint, dx, dy)]).convex_hull


def tree_shadow(
    trunk: Point,
    height_m: float,
    crown_r: float,
    elev_deg: float,
    az_deg: float,
) -> Point:
    """A tree's crown, elongated away from the sun.

    The trunk top is ``height - crown_r`` above ground, so a short tree casts
    almost nothing -- ``min(height - crown_r, ...)`` is what stops a 5 m tree
    with a 3 m crown from projecting a shadow as long as a tower's.
    """
    crown_r = crown_r or SETTINGS.default_tree_crown_m
    dx, dy = shadow_offset(max(height_m - crown_r, 0.0), elev_deg, az_deg, cap_m=60.0)
    return Point(trunk.x + dx, trunk.y + dy).buffer(crown_r)


def shade_geometry(
    shade_map: ShadeMap, cover_map: CoverMap, sun: SunPosition
) -> object | None:
    """Everything that is in shadow at ``sun``, as one geometry in meters.

    Also folds in the always-shaded cover features. ENDPOINT.md lists them as a
    sun source in their own right ("always-shaded spots, regardless of sun
    position") and they are strictly better evidence than a shadow cast from a
    guessed building height, so a spot under an arcade scores as shaded at
    midnight.
    """
    parts: list[object] = []
    for b in shade_map.blocks:
        try:
            if b.kind == "building":
                parts.append(building_shadow(b.shape, b.height_m, sun.elevation_deg, sun.azimuth_deg))
            else:
                parts.append(tree_shadow(b.shape, b.height_m, b.crown_r or 0.0, sun.elevation_deg, sun.azimuth_deg))
        except Exception:  # noqa: BLE001
            # One malformed footprint must not cost every other shadow in the
            # area. A building with a self-intersecting ring that survives
            # buffer(0) as a GeometryCollection would raise here.
            continue

    for c in cover_map.covers:
        if c.provides_sun:
            parts.append(c.shape)

    if not parts:
        return None
    return unary_union([p for p in parts if p is not None and not p.is_empty])


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #

def shade_fraction(geom: object, point: Point, radius_m: float) -> float:
    """Share of a disc around ``point`` that is shaded, 0.0 to 1.0.

    A fraction rather than a boolean because the difference between "an awning
    covers half of where you're standing" and "an awning covers all of it" is
    real, and a spot on the edge of an arcade deserves to rank below one directly
    under it.
    """
    disc = point.buffer(radius_m)
    try:
        frac = float(geom.intersection(disc).area / disc.area)
    except Exception:  # noqa: BLE001
        return 0.0
    # Clamped, not just bounded by the arithmetic. `intersection.area` of a
    # polygon against a disc it contains comes back as disc.area plus ~4e-16 of
    # float noise, so an unshaded-by-a-rounding-error spot reports "100.00004%
    # shade" -- which a UI renders verbatim and a rider reads as a measurement
    # finer than the building heights behind it.
    return min(max(frac, 0.0), 1.0)


def _nearest_shaded(geom: object, spot_pt: Point, max_gap_m: float) -> tuple[float, Point] | None:
    """The closest shaded point within ``max_gap_m``, and its distance.

    Walks outward from the stop point rather than testing a grid: the answer is
    the *exact* nearest shaded point, and a grid search would put a resolution
    artefact into every gap measurement. Cheap because the distance is almost
    always small -- a spot either has nearby shade or has none within 15 m.
    """
    if geom is None or geom.is_empty:
        return None
    if geom.contains(spot_pt):
        return 0.0, spot_pt

    try:
        # `nearest_points(a, b)[0]` is the point on `a`; index 1 is the point on
        # `b`. We want the shaded one. Index 0 would return the spot itself,
        # which sits at distance 0 from every shade geometry and would make every
        # spot look like it was already in the shade.
        nearest = nearest_points(spot_pt, geom)[1]
    except Exception:  # noqa: BLE001
        return None
    d = nearest.distance(spot_pt)
    if d > max_gap_m:
        return None
    return d, nearest


def _persistence(geom_t0: object, geom_t1: object, wait_pt: Point) -> float:
    """Half the shade now, half the shade when the car arrives.

    Shade that vanishes during the wait is worth less than shade that holds, and
    a rider who has just walked 150 m to sit in it deserves to know. Half and
    half rather than weighting arrival higher: a spot that is shaded on arrival
    and exposed five minutes later is nearly as bad as the reverse.
    """
    r = SETTINGS.shade_probe_radius_m
    return 0.5 * shade_fraction(geom_t0, wait_pt, r) + 0.5 * shade_fraction(geom_t1, wait_pt, r)


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

def rank_spots(
    spots: list[Spot],
    shade_map: ShadeMap,
    cover_map: CoverMap,
    sun: SunPosition,
    sun_later: SunPosition,
    *,
    max_gap_m: float | None = None,
) -> tuple[list[RankedSpot], object | None]:
    """Every spot, scored for sun, plus the shade geometry for the map overlay.

    ``sun_later`` is the sun at ``pickup_time + wait_minutes`` and is passed in
    rather than modelled. An earlier version estimated it with a "roughly 0.25
    degrees per minute" rule and a hand-rolled azimuth rate that had to encode
    the direction the sun turns -- and got the sign wrong, which made shade drift
    the same way all afternoon instead of reversing. pvlib is already a
    dependency and already computes a correct sun position, so modelling a worse
    one in-house was pure downside.

    Returns the geometry rather than burying it so the caller can put it in
    ``overlays.shade_geojson`` even when every spot ends up with no shade. A demo
    that shows the shadows is legible even when the ranking is not obvious, and
    a rider shown "here is where the shade is" can judge it themselves.
    """
    max_gap_m = SETTINGS.sun_max_gap_m if max_gap_m is None else max_gap_m

    if sun.elevation_deg < SETTINGS.sun_min_elevation_deg:
        # ENDPOINT.md: below 5 degrees, rank by walk distance. Shade at that
        # angle is a sliver along a wall, not somewhere to send someone.
        return _by_walk(spots, "Sun is low; shade not needed"), None

    shade_t0 = shade_geometry(shade_map, cover_map, sun)
    if shade_t0 is None:
        return _by_walk(spots, "No shade data for this area"), None

    shade_t1 = shade_geometry(shade_map, cover_map, sun_later) or shade_t0

    frame: LocalFrame = shade_map.frame
    weight = scoring.source_weight(ShadeSource.OSM_GEOMETRY.value)

    ranked: list[RankedSpot] = []
    for spot in spots:
        px, py = frame.to_m(spot.stop_point.lat, spot.stop_point.lng)
        spot_pt = Point(px, py)

        found = _nearest_shaded(shade_t0, spot_pt, max_gap_m)
        if found is None:
            ranked.append(
                RankedSpot(
                    spot=spot,
                    wait_point=spot.stop_point,
                    cover_feature=None,
                    gap_m=None,
                    score=round(scoring.no_cover_score(spot.walk_distance_m), 4),
                    confidence=spot.confidence,
                    reason="No shade within reach",
                )
            )
            continue

        gap, wait_pt = found
        persistence = _persistence(shade_t0, shade_t1, wait_pt)
        score = max(
            weight * persistence * scoring.gap_factor(gap, max_gap_m) * scoring.walk_factor(spot.walk_distance_m),
            scoring.no_cover_score(spot.walk_distance_m),
        )

        # A spot whose wait point is under a mapped cover feature is evidenced by
        # that feature, not by a shadow model, so report the feature. It is the
        # difference between "we think this is shaded" and "there is an arcade
        # here".
        cover = _covering_feature(cover_map, wait_pt, max_gap_m)

        ll = frame.to_ll(wait_pt.x, wait_pt.y)
        ranked.append(
            RankedSpot(
                spot=spot,
                wait_point=LatLng(lat=ll[0], lng=ll[1]),
                cover_feature=cover.as_model() if cover else None,
                gap_m=round(gap, 2),
                score=round(score, 4),
                confidence=spot.confidence,
                reason=(
                    f"{persistence * 100:.0f}% shade {gap:.0f} m from the pickup point "
                    f"(sun {sun.elevation_deg:.0f} deg up), {spot.walk_distance_m:.0f} m walk"
                ),
            )
        )

    ranked.sort(key=scoring.rank_key)
    return ranked, shade_t0


def _covering_feature(
    cover_map: CoverMap, pt: Point, max_gap_m: float
) -> Cover | None:
    for c in cover_map.covers:
        if c.provides_sun and c.shape.distance(pt) <= max_gap_m:
            return c
    return None


def _by_walk(spots: list[Spot], reason: str) -> list[RankedSpot]:
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


__all__ = [
    "building_shadow", "shadow_offset", "shade_fraction", "shade_geometry",
    "tree_shadow", "rank_spots",
]
