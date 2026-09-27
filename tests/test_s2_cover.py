"""Cover parsing: what OSM actually maps, turned into the right geometry.

Every case here is drawn from a measured tag in the demo area, not invented.
That matters because the failure this file exists to prevent is invisible: a
feature that is parsed away looks exactly like an area with no cover, and the
service still answers 200 with a confident-looking ranking.

ENDPOINT.md section 6 assumes cover features are areas. Most are not.
"""

from __future__ import annotations

from math import cos, hypot, pi, radians

import pytest
from shapely.geometry import LineString, Point, Polygon

from services.s2_weather_cover.cover import (
    CoverMap,
    ShadeMap,
    _classify,
    _geometry_in,
    _is_closed_ring,
    parse_covers,
    parse_shade,
)
from shared.config import SETTINGS
from shared.geo import _utm_epsg, frame_for

#: Anchored on the rider, like every real request.
FRAME = frame_for(*SETTINGS.demo_rider)
LAT0, LNG0 = SETTINGS.demo_rider


def _offset_pts(deltas: list[tuple[float, float]]) -> list[dict[str, float]]:
    """``[(east_m, north_m), ...]`` -> Overpass ``geometry`` near the rider.

    Offsets rather than absolute coordinates, because the assertion is about
    shape and closure, and absolute coordinates invite a test that passes for the
    wrong reason on any machine but the author's.
    """
    pts = []
    for east, north in deltas:
        dlat = north / 111_320.0
        dlng = east / (111_320.0 * cos(radians(LAT0)))
        pts.append({"lat": LAT0 + dlat, "lon": LNG0 + dlng})
    return pts


def _way(tags: dict[str, str], deltas: list[tuple[float, float]]) -> dict:
    return {"type": "way", "id": 1, "tags": tags, "geometry": _offset_pts(deltas)}


def _node(tags: dict[str, str], east: float = 0.0, north: float = 0.0) -> dict:
    (p,) = _offset_pts([(east, north)])
    return {"type": "node", "id": 2, "tags": tags, "lat": p["lat"], "lon": p["lon"]}


def _parse(elements: list[dict]) -> CoverMap:
    return parse_covers(
        (LAT0, LNG0, LAT0 + 0.004, LNG0 + 0.004), 150.0, {"elements": elements}, "cid", FRAME
    )


# --------------------------------------------------------------------------- #
# the regression: an open way is a line, not a degenerate polygon
# --------------------------------------------------------------------------- #

class TestOpenWaysSurvive:
    """``is_area`` used to be derived from the element *type*, so every way was
    forced through ``shapely.Polygon`` -- and ``Polygon`` on two points is empty.
    Measured over the captured demo responses, 529 of 547 classified cover
    features are open ways, so 97 % of the cover, all 100 ``covered=yes``
    highways included, was silently discarded. The rain ranking was scoring
    against an almost empty map.

    Ring closure, not element type, is what separates an area from a line: being
    *on* a covered walkway is being covered, so its geometry is a centreline.
    """

    #: The three open-way covers in the demo box, with their real tags and point
    #: counts. 2- and 3-point ways dominate -- short covered segments.
    OPEN_WAYS = [
        ({"highway": "pedestrian", "covered": "yes"}, 2),
        ({"highway": "footway", "covered": "arcade"}, 3),
        ({"highway": "residential", "covered": "yes"}, 11),
    ]

    @pytest.mark.parametrize("tags,npoints", OPEN_WAYS)
    def test_an_open_way_becomes_a_line_and_is_kept(self, tags, npoints):
        deltas = [(10.0 * i, 0.0) for i in range(npoints)]
        m = _parse([_way(tags, deltas)])

        assert len(m.covers) == 1, (
            f"a {npoints}-point {tags!r} was dropped; an open way is a line"
        )
        assert isinstance(m.covers[0].shape, LineString)
        assert m.covers[0].kind == "covered_walkway"

    def test_the_line_carries_its_own_length(self):
        """A LineString, so a 40 m covered walkway reports 40 m -- this is what
        makes the gap-to-kerb number in the response mean anything."""
        m = _parse([_way({"highway": "pedestrian", "covered": "yes"}, [(0.0, 0.0), (40.0, 0.0)])])
        assert m.covers[0].shape.length == pytest.approx(40.0, abs=0.5)

    def test_a_two_point_way_is_not_an_empty_polygon(self):
        """The exact shape that failed. Guards against someone 'fixing' it by
        special-casing point counts instead of checking closure."""
        assert _parse([_way({"highway": "pedestrian", "covered": "yes"},
                            [(0.0, 0.0), (40.0, 0.0)])]).covers[0].shape.area == 0.0

    def test_open_building_passages_survive_too(self):
        """223 of the 427 passages in the demo box are 2-point open ways -- the
        short links between buildings, which is exactly where a rider waits."""
        m = _parse([_way({"tunnel": "building_passage"}, [(0.0, 0.0), (25.0, 0.0)])])
        assert len(m.covers) == 1
        assert m.covers[0].kind == "building_passage"
        assert isinstance(m.covers[0].shape, LineString)


# --------------------------------------------------------------------------- #
# closed ways stay areas
# --------------------------------------------------------------------------- #

class TestClosedWaysStayAreas:
    """A shelter with a mapped outline is an area, and must remain one."""

    RING = [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0), (0.0, 0.0)]

    def test_a_closed_shelter_is_a_polygon(self):
        m = _parse([_way({"amenity": "shelter"}, self.RING)])
        assert len(m.covers) == 1
        assert isinstance(m.covers[0].shape, Polygon)
        assert m.covers[0].shape.area == pytest.approx(200.0, rel=0.02)
        assert m.covers[0].kind == "shelter"

    def test_a_closed_building_passage_is_a_polygon(self):
        m = _parse([_way({"tunnel": "building_passage"}, self.RING)])
        assert isinstance(m.covers[0].shape, Polygon)

    def test_closure_is_compared_with_a_tolerance(self):
        """The first and last coordinate of a closed way come from the same OSM
        node, but arrive as two pairs already put through a projection, so exact
        float equality holds only by luck. Half a millimetre separates the two
        cases at a 1.5 m cover gap and sits far above the float noise."""
        assert _is_closed_ring([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)])
        assert _is_closed_ring([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (1e-4, 4e-4)])
        # An open square, and a ring whose ends are 1 cm apart, are both open --
        # 1 mm would be *inside* the tolerance, which is the whole point of it.
        assert not _is_closed_ring([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
        assert not _is_closed_ring([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.01, 0.01)])

    def test_a_ring_repeated_exactly_twice_is_still_a_ring(self):
        """A degenerate 2-node closed way. Rare, and worth a polygon rather than
        a crash -- the area check rejects it, so the feature is dropped cleanly
        instead of raising inside the parser."""
        assert _is_closed_ring([(0.0, 0.0), (0.0, 0.0)])
        assert parse_covers(
            (LAT0, LNG0, LAT0 + 0.004, LNG0 + 0.004), 150.0,
            {"elements": [_way({"amenity": "shelter"}, [(0.0, 0.0), (0.0, 0.0)])]},
            "cid", FRAME,
        ).covers == []


# --------------------------------------------------------------------------- #
# nodes degrade to a disc
# --------------------------------------------------------------------------- #

class TestNodeShelters:
    """9 of the demo box's shelter features are nodes, and a bare point measures
    to its centre -- over-reporting the gap by half the shelter's length. With
    ``cover_gap_free_m`` at 1.5 m that is the difference between standing under
    the shelter and standing just outside it."""

    def test_a_node_shelter_becomes_a_buffered_disc(self):
        m = _parse([_node({"amenity": "shelter"})])
        assert len(m.covers) == 1
        shape = m.covers[0].shape
        assert not isinstance(shape, LineString)
        assert shape.area == pytest.approx(
            pi * SETTINGS.node_cover_radius_m**2, rel=0.01
        )

    def test_the_disc_is_centred_on_the_node(self):
        m = _parse([_node({"amenity": "shelter"}, east=20.0, north=10.0)])
        x, y = FRAME.to_m(*SETTINGS.demo_rider)
        assert m.covers[0].shape.contains(Point(x + 20.0, y + 10.0))

    def test_a_node_with_no_geometry_is_dropped_not_faked(self):
        """A null in ``geometry`` is normal Overpass output. Inventing a
        zero-sized feature at the origin would put a shelter in the Atlantic."""
        assert parse_covers(
            (LAT0, LNG0, LAT0 + 0.004, LNG0 + 0.004), 150.0,
            {"elements": [{"type": "way", "id": 3, "tags": {"tunnel": "building_passage"}}]},
            "cid", FRAME,
        ).covers == []


# --------------------------------------------------------------------------- #
# classification ladder
# --------------------------------------------------------------------------- #

class TestClassification:
    """The kinds ENDPOINT.md names, and the real tags that produce them in Miami."""

    @pytest.mark.parametrize("tags,kind", [
        ({"tunnel": "building_passage"}, "building_passage"),
        ({"man_made": "awning"}, "awning"),
        ({"man_made": "canopy"}, "canopy"),
        ({"amenity": "shelter"}, "shelter"),
        ({"highway": "bus_stop", "shelter": "yes"}, "shelter"),
        ({"public_transport": "platform", "covered": "yes"}, "covered_walkway"),
        ({"highway": "footway", "covered": "yes"}, "covered_walkway"),
        ({"highway": "pedestrian", "covered": "arcade"}, "covered_walkway"),
    ])
    def test_real_tags_classify(self, tags, kind):
        assert _classify(tags) == (kind, True)

    @pytest.mark.parametrize("tags", [
        {},
        {"building": "yes"},
        {"natural": "tree"},
        {"highway": "bus_stop"},
        {"covered": "yes"},
        {"highway": "footway", "covered": "no"},
        {"man_made": "kerb"},
    ])
    def test_non_cover_is_rejected(self, tags):
        assert _classify(tags) is None

    def test_a_building_passage_outranks_a_bare_covered_tag(self):
        """Ladder precedence, not last-match-wins: a passage tagged both ways is
        a passage, because that is the more specific claim about cover."""
        assert _classify({"tunnel": "building_passage", "covered": "yes"})[0] == \
            "building_passage"

    def test_shelter_without_highway_is_not_cover(self):
        """`shelter=yes` alone is a property of a bus stop or a platform, not a
        structure. On its own it describes nothing that exists."""
        assert _classify({"shelter": "yes"}) is None


# --------------------------------------------------------------------------- #
# shade blocks
# --------------------------------------------------------------------------- #

class TestShadeBlocks:
    RING = [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0), (0.0, 0.0)]

    def test_a_building_becomes_a_shadow_casting_block(self):
        m = parse_shade(
            (LAT0, LNG0, LAT0 + 0.004, LNG0 + 0.004), 150.0,
            {"elements": [_way({"building": "yes", "building:levels": "3"}, self.RING)]},
            "cid", FRAME,
        )
        assert len(m.blocks) == 1
        b = m.blocks[0]
        assert b.kind == "building"
        assert b.shape.area == pytest.approx(200.0, rel=0.02)

    def test_an_unclosed_building_is_dropped_rather_than_cast_zero_width_shadow(self):
        """A building mapped as an open way has no footprint. A LineString casts
        no area, so a shadow polygon built from one is either empty or a
        sliver, and either way it is a confident shadow in the wrong place."""
        m = parse_shade(
            (LAT0, LNG0, LAT0 + 0.004, LNG0 + 0.004), 150.0,
            {"elements": [_way({"building": "yes"}, [(0.0, 0.0), (20.0, 0.0)])]},
            "cid", FRAME,
        )
        assert m.blocks == []

    def test_a_tree_becomes_a_point_with_a_crown(self):
        m = parse_shade(
            (LAT0, LNG0, LAT0 + 0.004, LNG0 + 0.004), 150.0,
            {"elements": [_node({"natural": "tree"})]},
            "cid", FRAME,
        )
        assert len(m.blocks) == 1
        assert m.blocks[0].kind == "tree"
        assert m.blocks[0].crown_r == SETTINGS.default_tree_crown_m

    def test_a_tree_row_is_skipped_rather_than_misread_as_a_trunk(self):
        """`tree_row` is tagged on a *way*, and `sun_shade.tree_shadow` reads
        `shape.x` as a single trunk. Handing it a line would raise inside the
        ranking, where the blanket handler reports "sun mode failed" and points at
        the wrong layer. Zero in the demo box, and the query no longer asks for
        them, so this pins the guard rather than a capability.
        """
        m = parse_shade(
            (LAT0, LNG0, LAT0 + 0.004, LNG0 + 0.004), 150.0,
            {"elements": [_way({"natural": "tree_row"}, self.RING)]},
            "cid", FRAME,
        )
        assert m.blocks == []

    def test_the_query_does_not_ask_for_tree_rows(self):
        """The selector is gone, so a row cannot arrive and be silently dropped.
        Asserted on the query text because that is the only place the decision
        lives, and it is easy to re-add a selector that yields nothing."""
        assert "tree_row" not in SETTINGS.shade_query

    def test_height_prefers_the_tag_then_levels_then_the_default(self):
        for tags, expected, estimated in [
            ({"building": "yes", "height": "24"}, 24.0, False),
            ({"building": "yes", "building:levels": "3"}, 10.0, False),
            ({"building": "yes"}, SETTINGS.default_building_height_m, True),
        ]:
            m = parse_shade(
                (LAT0, LNG0, LAT0 + 0.004, LNG0 + 0.004), 150.0,
                {"elements": [_way(tags, self.RING)]}, "cid", FRAME,
            )
            assert m.blocks[0].height_m == pytest.approx(expected), tags
            assert m.blocks[0].height_estimated is estimated, tags

    def test_three_of_the_demo_areas_fifty_two_buildings_carry_levels(self):
        """Why ``height_estimated`` is carried rather than smoothed over. Recorded
        as a count so the flag is not quietly demoted when the demo area changes.
        """
        assert SETTINGS.default_building_height_m == 6.0


# --------------------------------------------------------------------------- #
# the frame
# --------------------------------------------------------------------------- #

class TestOneFramePerRequest:
    """Every tile is parsed into a single rider-anchored frame.

    What ``LocalFrame`` actually does is easy to get wrong, and getting it wrong
    is how this ends up justified with a fabricated reason. ``to_m`` returns
    *absolute* UTM easting/northing; the anchor is used only to select the EPSG.
    So two anchors inside one zone give bit-identical coordinates, and a
    single-frame rule is a no-op in Miami. It is not pointless though: across a
    zone boundary the frames are ~600 km apart, not offset by a rounding error,
    and per-tile frames would place cover hundreds of kilometres from the spots
    being ranked with nothing reporting an error.
    """

    def test_frames_in_one_zone_agree_exactly(self):
        """The measured reality, and the reason a 'small offset' story would be
        wrong. Anchors 500 m apart inside zone 17N."""
        a = frame_for(LAT0, LNG0)
        b = frame_for(LAT0 + 0.004, LNG0 + 0.004)
        assert a.to_m(LAT0, LNG0) == b.to_m(LAT0, LNG0)

    def test_frames_across_a_zone_boundary_are_hundreds_of_km_apart(self):
        """-78.0 longitude is the 17N/18N boundary. The same point in each frame
        is 601 869 m apart as the crow flies, which is why 'a translation of a
        few metres' would have been a comforting lie."""
        west = frame_for(25.756918, -78.0002)   # 32617
        east = frame_for(25.756918, -78.0000)   # 32618
        wx, wy = west.to_m(25.756918, -78.0000)
        ex, ey = east.to_m(25.756918, -78.0000)
        gap = hypot(ex - wx, ey - wy)
        assert gap > 100_000, f"expected a zone-scale discontinuity, got {gap:.0f} m"

    def test_the_demo_area_is_a_single_zone(self):
        """So the reason this rule is not doing any work *here* is explicit
        rather than assumed, and a future demo area that straddles 17N/18N is
        caught by this test rather than by a rider."""
        s, w, n, e = SETTINGS.demo_bbox
        zones = {
            _utm_epsg(lat, lng)
            for lat in (s, n) for lng in (w, e)
        }
        assert len(zones) == 1, f"demo area straddles UTM zones {sorted(zones)}"
        assert zones == {32617}  # 17N, which is where Miami is

    def test_a_feature_from_two_tiles_lands_on_the_same_coordinates(self):
        """The property the merge actually depends on: a cover feature returned by
        two overlapping containment tiles must occupy the same coordinates both
        times, so deduping by id is a real dedupe rather than a near-miss that
        leaves the map drawing the same awning twice at slightly different
        places. Both calls use the rider-anchored frame -- which is the rule
        under test -- and the tile anchors are inside the same zone, so they
        agree exactly, as the first test in this class establishes.
        """
        el = _way({"tunnel": "building_passage"}, [(0.0, 0.0), (40.0, 0.0)])
        from_rider = _geometry_in(FRAME, el)
        from_other_tile = _geometry_in(frame_for(LAT0 + 0.002, LNG0 + 0.002), el)
        assert from_rider.equals(from_other_tile)
