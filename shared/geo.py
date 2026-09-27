"""Geometry helpers. All internal math happens in METERS, never in raw degrees
(ENDPOINT.md section 6).

The lat/lng order trap called out in ENDPOINT.md ("the #1 bug tonight") is
handled structurally rather than by discipline:

* Our JSON uses named ``{lat, lng}`` fields (see ``models.LatLng``).
* GeoJSON, shapely-with-pyproj, and OSRM all use ``(lng, lat)``.
* ``Transformer`` is always built with ``always_xy=True``, so it wants
  ``(lng, lat)`` in and out.

The only place those two conventions meet is this module. Everything above it
works in a local metric frame, so a mistake has to be made deliberately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from pyproj import Transformer
from shapely.geometry import LineString, Point

__all__ = [
    "LocalFrame",
    "Polyline",
    "frame_for",
    "haversine_m",
    "normalize_bearing_deg",
    "bearing_delta_deg",
    "bearing_between_deg",
    "offset_point",
    "TileLevel",
    "TILE_LEVELS",
    "level_for",
    "tile_bbox",
    "expanded_query_bbox",
    "tile_cache_id",
    "tiles_for_bbox",
]

#: Meters per degree of latitude, good enough for the haversine fallback and for
#: deciding whether two coordinates are "the same node" after a round trip.
_M_PER_DEG_LAT = 111_320.0


@lru_cache(maxsize=64)
def _transformers(epsg: int) -> tuple[Transformer, Transformer]:
    to_m = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    return to_m, to_ll


def _utm_epsg(lat: float, lng: float) -> int:
    """ENDPOINT.md section 6 ``local_projector``, unchanged in spirit."""
    zone = int((lng + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


@dataclass(frozen=True)
class LocalFrame:
    """A metric working frame anchored at a point, using that point's UTM zone.

    Prefer ``frame_for(...)`` over constructing this directly: frames are cached
    by origin so repeated calls reuse the underlying ``Transformer``.
    """

    lat: float
    lng: float

    def to_m(self, lat: float, lng: float) -> tuple[float, float]:
        to_m, _ = _transformers(_utm_epsg(self.lat, self.lng))
        x, y = to_m.transform(lng, lat)  # always_xy -> (lng, lat) in
        return float(x), float(y)

    def to_ll(self, x: float, y: float) -> tuple[float, float]:
        _, to_ll = _transformers(_utm_epsg(self.lat, self.lng))
        lng, lat = to_ll.transform(x, y)
        return float(lat), float(lng)

    def to_shape(self, latlngs: list[tuple[float, float]]) -> LineString:
        """Build a shapely LineString from ``[(lat, lng), ...]``, in meters."""
        return LineString([self.to_m(la, lo) for la, lo in latlngs])

    def distance_m(self, a: tuple[float, float], b: tuple[float, float]) -> float:
        ax, ay = self.to_m(*a)
        bx, by = self.to_m(*b)
        return math.hypot(bx - ax, by - ay)


@lru_cache(maxsize=64)
def frame_for(lat: float, lng: float) -> LocalFrame:
    """Cached accessor for a metric frame. Origin is rounded to ~1 m so that
    nearby requests share a ``Transformer`` instead of building thousands."""
    return LocalFrame(round(lat, 5), round(lng, 5))


def haversine_m(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance in meters. Used for cheap radius pre-filters where
    building a full projected frame would be wasted work."""
    r = 6_371_000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def normalize_bearing_deg(bearing: float) -> float:
    """Wrap to [0, 360)."""
    return bearing % 360.0


def bearing_delta_deg(a: float, b: float) -> float:
    """Smallest signed difference ``a - b`` between two compass bearings, in (-180, 180].

    Comparing bearings with ``abs(a - b) < tol`` is a bug waiting to happen:
    359.7 deg and 0.3 deg are 0.6 deg apart, not 359.4. S3 compares
    ``curb_bearing_deg`` against camera headings, so this belongs next to the
    geometry helpers rather than in a test.
    """
    return (a - b + 180.0) % 360.0 - 180.0


def bearing_between_deg(
    frame: LocalFrame, a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Compass bearing from ``a`` to ``b``, degrees clockwise from north."""
    ax, ay = frame.to_m(*a)
    bx, by = frame.to_m(*b)
    return normalize_bearing_deg(math.degrees(math.atan2(bx - ax, by - ay)))


@dataclass
class Polyline:
    """An arc-length-parameterized polyline in METERS.

    Sampling by arc length (rather than by vertex index) is what makes
    ``SAMPLE_STEP_M`` mean "every 10 m of curb" instead of "every 10 vertices",
    which on a dense campus would bunch candidates at corners.
    """

    xs: list[float]
    ys: list[float]

    def __post_init__(self) -> None:
        if len(self.xs) != len(self.ys):
            raise ValueError("xs and ys must be the same length")
        if len(self.xs) < 2:
            raise ValueError("a polyline needs at least two points")

        # Cumulative arc length at each vertex.
        cum = [0.0]
        for i in range(1, len(self.xs)):
            cum.append(cum[-1] + math.hypot(self.xs[i] - self.xs[i - 1], self.ys[i] - self.ys[i - 1]))
        self.cum = cum
        self.length = cum[-1]

    @classmethod
    def from_latlngs(cls, frame: LocalFrame, latlngs: list[tuple[float, float]]) -> "Polyline":
        pts = [frame.to_m(la, lo) for la, lo in latlngs]
        return cls([p[0] for p in pts], [p[1] for p in pts])

    def point_at(self, s: float) -> tuple[float, float, float, float]:
        """Return ``(x, y, tx, ty)`` at arc length ``s`` from the start.

        ``(tx, ty)`` is the unit tangent of the segment containing ``s``, so the
        caller gets a well-defined normal even mid-segment. Extrapolating past
        either end reuses the first/last segment's direction.
        """
        if s <= 0:
            i = 0
        elif s >= self.length:
            i = len(self.xs) - 2
        else:
            # Binary search for the segment containing s.
            lo, hi = 0, len(self.cum) - 1
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if self.cum[mid] <= s:
                    lo = mid
                else:
                    hi = mid
            i = lo

        seg_len = self.cum[i + 1] - self.cum[i]
        if seg_len <= 0:
            # Degenerate (duplicate) segment: fall back to a neighbouring one.
            frac = 0.0
        else:
            # Deliberately NOT clamped. The binary search below already
            # guarantees 0 <= frac <= 1 for any s inside the polyline, so the
            # clamp would only ever affect the extrapolating branches, turning
            # documented extrapolation into snapping to the endpoint.
            frac = (s - self.cum[i]) / seg_len

        x = self.xs[i] + frac * (self.xs[i + 1] - self.xs[i])
        y = self.ys[i] + frac * (self.ys[i + 1] - self.ys[i])

        dx = self.xs[i + 1] - self.xs[i]
        dy = self.ys[i + 1] - self.ys[i]
        norm = math.hypot(dx, dy)
        if norm == 0:
            tx, ty = 1.0, 0.0
        else:
            tx, ty = dx / norm, dy / norm
        return x, y, tx, ty

    def distance_to(self, px: float, py: float) -> float:
        """Shortest distance in meters from a point to this polyline."""
        best = float("inf")
        for i in range(len(self.xs) - 1):
            ax, ay = self.xs[i], self.ys[i]
            bx, by = self.xs[i + 1], self.ys[i + 1]
            dx, dy = bx - ax, by - ay
            seg_sq = dx * dx + dy * dy
            if seg_sq == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / seg_sq
                t = min(max(t, 0.0), 1.0)
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d < best:
                best = d
        return best

    def nearest_arc(self, px: float, py: float) -> tuple[float, float]:
        """Return ``(arc_m, distance_m)`` of the closest point on this polyline.

        Used to project a crossing or bus-stop node onto a roadway so its
        exclusion can be measured ALONG the road instead of as a disc.
        """
        best_d, best_arc = float("inf"), 0.0
        for i in range(len(self.xs) - 1):
            ax, ay = self.xs[i], self.ys[i]
            bx, by = self.xs[i + 1], self.ys[i + 1]
            dx, dy = bx - ax, by - ay
            seg_sq = dx * dx + dy * dy
            seg_len = math.sqrt(seg_sq)
            if seg_len == 0:
                t, d = 0.0, math.hypot(px - ax, py - ay)
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / seg_sq
                t = min(max(t, 0.0), 1.0)
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d < best_d:
                best_d = d
                best_arc = self.cum[i] + t * seg_len
        return best_arc, best_d

    def signed_offset(self, arc_m: float, px: float, py: float) -> float:
        """Signed perpendicular offset of a point from the centerline.

        Positive means the point lies to the LEFT of the way's digitization
        direction. This is how we tell which curb a bus stop belongs to, so its
        exclusion can be applied to one side rather than both.
        """
        cx, cy, tx, ty = self.point_at(arc_m)
        cross = tx * (py - cy) - ty * (px - cx)
        return cross

    def as_shapely(self) -> LineString:
        return LineString(list(zip(self.xs, self.ys)))

    def nearest_point(self, px: float, py: float) -> tuple[float, float, float]:
        """Return ``(x, y, distance)`` of the closest point on this polyline."""
        best_d, best_x, best_y = float("inf"), self.xs[0], self.ys[0]
        for i in range(len(self.xs) - 1):
            ax, ay = self.xs[i], self.ys[i]
            bx, by = self.xs[i + 1], self.ys[i + 1]
            dx, dy = bx - ax, by - ay
            seg_sq = dx * dx + dy * dy
            if seg_sq == 0:
                t = 0.0
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / seg_sq
                t = min(max(t, 0.0), 1.0)
            cx, cy = ax + t * dx, ay + t * dy
            d = math.hypot(px - cx, py - cy)
            if d < best_d:
                best_d, best_x, best_y = d, cx, cy
        return best_x, best_y, best_d

    def to_latlngs(self, frame: LocalFrame) -> list[tuple[float, float]]:
        return [frame.to_ll(x, y) for x, y in zip(self.xs, self.ys)]


def side_normal(tx: float, ty: float, side: str) -> tuple[float, float]:
    """Unit outward normal on ``side`` of a roadway with unit tangent ``(tx, ty)``.

    The right-hand normal of ``(tx, ty)`` in a metric frame is ``(ty, -tx)``; the
    left-hand normal is its negation. ``side`` is relative to the way's
    digitization direction, NOT to the compass, and NOT to which way traffic
    runs -- see ``legal_sides`` for that distinction.

    This exists as one function because both *where the point goes* and *which
    way the camera is aimed* have to be derived from the same normal. They were
    originally two independent implementations of it, and they disagreed: every
    "left" candidate was placed on the right kerb while being labelled left, so
    both sides of every street collapsed onto a single point.
    """
    if side == "right":
        return ty, -tx
    if side == "left":
        return -ty, tx
    raise ValueError(f"side must be 'left' or 'right', got {side!r}")


def offset_point(
    x: float, y: float, tx: float, ty: float, distance: float, side: str = "right"
) -> tuple[float, float]:
    """Offset a point perpendicular to a tangent, out to ``side``.

    ``distance`` is a magnitude; pass ``side`` to choose which way "out" is. The
    default is ``right`` because that is the right-hand normal in a metric frame
    and therefore the side a vehicle travels on in right-hand traffic -- but
    callers producing per-side curbs must pass it explicitly.
    """
    nx, ny = side_normal(tx, ty, side)
    return x + nx * distance, y + ny * distance


# --------------------------------------------------------------------------- #
# Cache tiling
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TileLevel:
    """One granularity of the Overpass document cache.

    The cache unit is a *containment* tile: every rider inside it maps to the
    same key, so ``prefetch_demo_area.py`` and the request path always agree.
    Snapping the rider to a grid node instead was tried first and rejected --
    because the tile must also cover the rider's whole search circle, adjacent
    snapped nodes produce heavily overlapping tiles, and a 0.8 km demo area
    expanded to 42 near-duplicate cache entries instead of 4.

    Coverage is guaranteed by expanding the *query* by the search radius while
    keeping the *cache key* at tile granularity. Query areas overlap slightly;
    cache entries do not proliferate.
    """

    size_deg: float
    max_radius_m: float


#: Level 0 tiles are ~445 m and cover any radius up to 200 m (the demo default is
#: 150 m). Level 1 tiles are ~1110 m and cover up to the 500 m hard cap.
TILE_LEVELS: tuple[TileLevel, ...] = (
    TileLevel(size_deg=0.004, max_radius_m=200.0),
    TileLevel(size_deg=0.010, max_radius_m=500.0),
)

#: Slack so the expanded query always fully contains the search circle.
QUERY_MARGIN_M = 25.0


def level_for(radius_m: float) -> TileLevel:
    for level in TILE_LEVELS:
        if radius_m <= level.max_radius_m:
            return level
    return TILE_LEVELS[-1]


def _tile_index(lat: float, lng: float, size: float) -> tuple[int, int]:
    return math.floor(lat / size), math.floor(lng / size)


def tile_bbox(
    lat: float, lng: float, radius_m: float
) -> tuple[float, float, float, float]:
    """The containment tile containing ``(lat, lng)``. This is the cache unit."""
    level = level_for(radius_m)
    i, j = _tile_index(lat, lng, level.size_deg)
    s = level.size_deg
    return (round(i * s, 6), round(j * s, 6), round((i + 1) * s, 6), round((j + 1) * s, 6))


def expanded_query_bbox(
    tile: tuple[float, float, float, float], radius_m: float
) -> tuple[float, float, float, float]:
    """Grow a tile by the search radius so it always covers the full circle.

    A rider standing 10 m from a tile edge would otherwise have part of their
    150 m search area fall outside the cached document, and the shortfall would
    be invisible -- just fewer spots than expected.
    """
    s, w, n, e = tile
    dlat = (radius_m + QUERY_MARGIN_M) / 111_320.0
    dlng = (radius_m + QUERY_MARGIN_M) / (
        111_320.0 * max(math.cos(math.radians((s + n) / 2.0)), 1e-6)
    )
    return (round(s - dlat, 6), round(w - dlng, 6), round(n + dlat, 6), round(e + dlng, 6))


def tile_cache_id(
    lat: float, lng: float, radius_m: float, kind: str
) -> str:
    """Stable cache identifier for the tile containing a point.

    Derived from the tile, never from the exact position, so every rider in the
    same tile shares one entry and a prefetch can predict it.
    """
    tb = tile_bbox(lat, lng, radius_m)
    return f"{kind}:{','.join(f'{v:.6f}' for v in tb)}"


def tiles_for_bbox(
    bbox: tuple[float, float, float, float], radius_m: float
) -> list[tuple[float, float, float, float]]:
    """Every tile needed to cover ``bbox``, in deterministic order.

    Non-overlapping and de-duplicated, so prefetching an 0.8 km demo area
    fetches 4 documents rather than 42.
    """
    level = level_for(radius_m)
    s, w, n, e = bbox
    i0, _ = _tile_index(s, w, level.size_deg)
    i1, j1 = _tile_index(n, e, level.size_deg)
    _, j0 = _tile_index(s, w, level.size_deg)
    size = level.size_deg
    out: list[tuple[float, float, float, float]] = []
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            out.append(
                (round(i * size, 6), round(j * size, 6), round((i + 1) * size, 6), round((j + 1) * size, 6))
            )
    return out


def point_to_point_frame(px: float, py: float, x: float, y: float) -> Point:
    return Point(x, y)


def polyline_to_shape(pts: list[tuple[float, float]]) -> LineString:
    return LineString(pts)


def _m_per_deg_lat(lat: float) -> float:
    return _M_PER_DEG_LAT * math.cos(math.radians(lat))
