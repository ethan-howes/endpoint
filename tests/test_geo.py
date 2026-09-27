"""Geometry helpers. Guards the lat/lng order trap ENDPOINT.md calls the #1 bug."""

from __future__ import annotations

import math

import pytest

from shared.geo import (
    Polyline,
    expanded_query_bbox,
    frame_for,
    haversine_m,
    level_for,
    offset_point,
    tile_bbox,
    tile_cache_id,
    tiles_for_bbox,
)

#: A known point: the Ernest R. Graham Center, FIU Miami.
GRAHAM = (25.756918, -80.372182)


def test_frame_roundtrips_lat_lng():
    """to_m then to_ll must return the original point."""
    frame = frame_for(*GRAHAM)
    x, y = frame.to_m(*GRAHAM)
    lat, lng = frame.to_ll(x, y)
    assert lat == pytest.approx(GRAHAM[0], abs=1e-9)
    assert lng == pytest.approx(GRAHAM[1], abs=1e-9)


def test_frame_uses_easting_northing_order():
    """A point due north has a larger projected Y than X-offset, and moving
    north increases Y. Catches an accidental (lat, lng) swap in the transformer."""
    frame = frame_for(*GRAHAM)
    _, y0 = frame.to_m(*GRAHAM)
    _, y1 = frame.to_m(GRAHAM[0] + 0.001, GRAHAM[1])
    assert y1 > y0
    assert y1 - y0 == pytest.approx(111.0, abs=5.0)


def test_frame_uses_utm_not_antimeridian():
    """A point just west of the antimeridian must not wrap to a huge distance."""
    frame = frame_for(0.0, 179.999)
    x0, y0 = frame.to_m(0.0, 179.999)
    x1, y1 = frame.to_m(0.0, -179.999)
    assert math.hypot(x1 - x0, y1 - y0) < 1000.0


def test_haversine_known_distance():
    # ~111 m per 0.001 deg of latitude.
    assert haversine_m(25.0, -80.0, 25.001, -80.0) == pytest.approx(111.0, abs=2.0)


class TestPolyline:
    def test_arc_length_sampling_is_uniform(self):
        """`point_at` walks true arc length, not vertex index -- this is what
        makes SAMPLE_STEP_M mean 'every 10 m of curb'."""
        pl = Polyline(xs=[0, 100, 200], ys=[0, 0, 0])
        assert pl.length == pytest.approx(200.0)
        for s in (0, 25, 50, 137.5, 200):
            x, y, _, _ = pl.point_at(s)
            assert x == pytest.approx(s)
            assert y == pytest.approx(0.0)

    def test_extrapolates_past_ends(self):
        pl = Polyline(xs=[0, 10], ys=[0, 0])
        x, _, _, _ = pl.point_at(-50)
        assert x == pytest.approx(-50)
        x, _, _, _ = pl.point_at(500)
        assert x == pytest.approx(500)

    def test_tangent_is_unit_length(self):
        pl = Polyline(xs=[0, 30, 30], ys=[0, 40, 90])
        for s in (5, 35, 80):
            _, _, tx, ty = pl.point_at(s)
            assert math.hypot(tx, ty) == pytest.approx(1.0)

    def test_rejects_too_few_points(self):
        with pytest.raises(ValueError):
            Polyline(xs=[0], ys=[0])

    def test_nearest_arc(self):
        pl = Polyline(xs=[0, 100], ys=[0, 0])
        arc, dist = pl.nearest_arc(40, 7)
        assert arc == pytest.approx(40.0)
        assert dist == pytest.approx(7.0)

    def test_signed_offset_sign_convention(self):
        """Positive means LEFT of the way's digitization direction. A bus stop on
        the north side of an east-west road runs west->east, so north is left."""
        pl = Polyline(xs=[0, 100], ys=[0, 0])  # heading +x (east)
        assert pl.signed_offset(50, 50, 10) > 0  # +y is north == left
        assert pl.signed_offset(50, 50, -10) < 0

    def test_distance_to_is_zero_on_the_line(self):
        pl = Polyline(xs=[0, 100], ys=[0, 0])
        assert pl.distance_to(50, 0) == pytest.approx(0.0)


def test_offset_point_right_normal():
    """Right-hand normal of a unit tangent (tx, ty) is (ty, -tx)."""
    # heading east: right is south
    x, y = offset_point(0, 0, 1.0, 0.0, 10.0)
    assert (x, y) == pytest.approx((0.0, -10.0))
    # heading north: right is east
    x, y = offset_point(0, 0, 0.0, 1.0, 10.0)
    assert (x, y) == pytest.approx((10.0, 0.0))


class TestTiling:
    def test_riders_in_one_tile_share_a_key(self):
        """The whole point of containment tiling: two riders a few metres apart
        must land on the same cache key, so prefetch and the request path agree
        without prefetch having to know where the rider will stand."""
        a = tile_cache_id(*GRAHAM, 150.0, "roads")
        b = tile_cache_id(GRAHAM[0] + 0.0001, GRAHAM[1] - 0.0001, 150.0, "roads")
        assert a == b

    def test_riders_in_different_tiles_differ(self):
        a = tile_cache_id(25.7560, -80.3720, 150.0, "roads")
        b = tile_cache_id(25.7620, -80.3720, 150.0, "roads")
        assert a != b

    def test_tile_contains_the_rider(self):
        s, w, n, e = tile_bbox(*GRAHAM, 150.0)
        assert s <= GRAHAM[0] <= n
        assert w <= GRAHAM[1] <= e

    def test_expanded_query_always_covers_the_search_radius(self):
        """A rider near a tile edge must still have their whole search circle
        inside the queried area, or the shortfall shows up silently as too few
        spots."""
        radius = 150.0
        for dlat, dlng in [
            (0.0, 0.0),
            (0.0019, 0.0019),   # essentially at a tile corner
            (-0.0019, 0.0019),
            (0.0019, -0.0019),
            (-0.0019, -0.0019),
        ]:
            lat, lng = GRAHAM[0] + dlat, GRAHAM[1] + dlng
            tile = tile_bbox(lat, lng, radius)
            s, w, n, e = expanded_query_bbox(tile, radius)
            assert s < lat - radius / 111_320.0, (lat, lng, "south")
            assert n > lat + radius / 111_320.0, (lat, lng, "north")
            assert w < lng, (lat, lng, "west")
            assert e > lng, (lat, lng, "east")

    def test_same_input_yields_identical_tile(self):
        assert tile_bbox(*GRAHAM, 150.0) == tile_bbox(*GRAHAM, 150.0)

    def test_larger_radius_selects_a_coarser_level(self):
        """Radius must not silently exceed what the tile can cover."""
        assert level_for(150.0).size_deg < level_for(500.0).size_deg
        assert level_for(500.0).size_deg * 111_320.0 / 2.0 >= 500.0

    def test_tiles_for_bbox_is_compact(self):
        """An 0.8 km demo area must expand to a handful of documents, not dozens
        of near-duplicates."""
        bbox = (25.7533, -80.3762, 25.7605, -80.3682)
        tiles = tiles_for_bbox(bbox, 150.0)
        assert len(tiles) <= 12, f"too many tiles: {len(tiles)}"
        assert len(tiles) == len(set(tiles)), "tiles must be de-duplicated"

    def test_tiles_for_bbox_covers_every_corner(self):
        bbox = (25.7533, -80.3762, 25.7605, -80.3682)
        s, w, n, e = bbox
        tiles = tiles_for_bbox(bbox, 150.0)
        for corner in ((s, w), (s, e), (n, w), (n, e)):
            assert tile_bbox(corner[0], corner[1], 150.0) in tiles
