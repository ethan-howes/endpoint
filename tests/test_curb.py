"""Candidate generation: arc-length sampling, one-way handling, corridor safety.

The geometry decision under test is ENDPOINT.md section 6 S1's offset-curb-line
approach replaced with direct centerline sampling. These tests pin down the
properties that made that simplification safe.
"""

from __future__ import annotations

import math

import pytest

from shared.config import SAMPLE_STEP_M
from shared.geo import LocalFrame, bearing_delta_deg, side_normal
from services.s1_legal_spots.curb import generate_candidates, legal_sides

from .conftest import TEST_ORIGIN, make_road


def _offset_lat_m(frame: LocalFrame, m: float) -> float:
    return m / 111_320.0


class TestSampling:
    def test_samples_every_step_along_the_road(self, frame):
        road = make_road(frame)
        cands = generate_candidates(road, "right")
        right = sorted(c.arc_m for c in cands if c.side == "right")
        assert len(right) >= 15
        # Uniform in arc length, which is what "every 10 m of curb" means.
        gaps = [b - a for a, b in zip(right, right[1:])]
        assert all(abs(g - SAMPLE_STEP_M) < 1e-6 for g in gaps), gaps

    def test_road_shorter_than_a_step_yields_nothing(self, frame):
        """Better to offer nothing than to invent a curb on a 3 m fragment."""
        base_lat, base_lng = TEST_ORIGIN
        road = make_road(
            frame,
            latlngs=[(base_lat, base_lng), (base_lat, base_lng + 0.00002)],
        )
        assert generate_candidates(road, "right") == []

    def test_candidates_sit_at_the_side_offset(self, frame):
        road = make_road(frame, right_offset=3.5)
        for c in generate_candidates(road, "right"):
            assert c.offset_m == 3.5
            # Corridor validation should hold trivially for a straight road.
            assert road.polyline.distance_to(c.x, c.y) == pytest.approx(3.5, abs=0.01)

    def test_each_side_uses_its_own_offset(self, frame):
        """A bike lane on one side pushes that curb outward and leaves the other
        side alone. A single half-width would put the car in the bike lane."""
        road = make_road(frame, left_offset=3.5, right_offset=5.3)
        for c in generate_candidates(road, "right"):
            assert c.offset_m == (3.5 if c.side == "left" else 5.3)

    def test_left_and_right_kerbs_are_distinct_points(self, frame):
        """REGRESSION: both sides were being placed on the right-hand normal, so
        every 'left' candidate landed on the right kerb -- making the two sides
        the same point, and letting the deduper silently delete half of every
        street. This is why the definition of done ("spots on both sides") could
        never pass no matter how much data was loaded.

        The failure was invisible in every other test: the candidates were all
        still legal, all still at the right distance from the centerline, and the
        bearing was computed from a *different* implementation of the same normal
        that happened to agree. Only the left-vs-right relationship was wrong.
        """
        road = make_road(frame, left_offset=2.5, right_offset=2.5)
        cands = generate_candidates(road, "right")
        by_arc: dict[float, dict[str, object]] = {}
        for c in cands:
            by_arc.setdefault(c.arc_m, {})[c.side] = c

        assert by_arc, "no candidates generated"
        for arc, pair in by_arc.items():
            assert set(pair) == {"left", "right"}, f"arc {arc} missing a side"
            left, right = pair["left"], pair["right"]
            sep = math.hypot(left.x - right.x, left.y - right.y)  # type: ignore[attr-defined]
            expected = 2.5 + 2.5
            assert sep == pytest.approx(expected, abs=0.01), (
                f"arc {arc}: kerbs {sep:.2f} m apart, expected {expected:.2f} m"
            )

    def test_sides_straddle_the_centerline(self, frame):
        """Stronger than 'not identical': each kerb must sit out toward its own
        side of the roadway. Projecting each point onto that side's normal must
        come out positive.

        Note the deliberate asymmetry (L=3.0, R=4.0): a bike lane on one side makes
        the kerbs unequal, so their midpoint is NOT the centerline and must not be
        asserted to be. What must hold is the sign of each offset.
        """
        road = make_road(frame, left_offset=3.0, right_offset=4.0)
        by_arc: dict[float, dict[str, object]] = {}
        for c in generate_candidates(road, "right"):
            by_arc.setdefault(c.arc_m, {})[c.side] = c

        assert by_arc
        for arc, pair in by_arc.items():
            cx, cy, tx, ty = road.polyline.point_at(arc)
            for side, expected_offset in (("left", 3.0), ("right", 4.0)):
                nx, ny = side_normal(tx, ty, side)
                c = pair[side]
                out = (c.x - cx) * nx + (c.y - cy) * ny  # type: ignore[attr-defined]
                assert out == pytest.approx(expected_offset, abs=0.01), (
                    f"arc {arc} {side}: offset along its own normal is {out:.2f} m, "
                    f"expected {expected_offset:.2f} m"
                )

    def test_bearing_matches_the_side_the_point_is_on(self, frame):
        """S3 aims its camera with curb_bearing_deg. If the bearing and the
        placement derive the normal separately they can disagree, and the symptom
        is a camera pointed at the far side of the street -- an image of whatever
        happens to be across the road, confidently labelled as the pickup spot.
        """
        road = make_road(frame)
        for c in generate_candidates(road, "right"):
            cx, cy, _, _ = road.polyline.point_at(c.arc_m)
            # Vector from the centerline out to the point that was actually placed.
            ox, oy = c.x - cx, c.y - cy
            # Recompute the bearing of THAT vector and compare.
            placed = math.degrees(math.atan2(ox, oy)) % 360.0
            assert bearing_delta_deg(placed, c.curb_bearing_deg) == pytest.approx(0.0, abs=1e-6)

    def test_zero_offset_side_is_skipped(self, frame):
        road = make_road(frame, left_offset=0.0, right_offset=3.5)
        assert {c.side for c in generate_candidates(road, "right")} == {"right"}


class TestOneWay:
    def test_twoway_offers_both_sides(self, frame):
        assert legal_sides(make_road(frame, oneway=False), "right") == ("left", "right")

    def test_oneway_offers_only_the_travel_side(self, frame):
        road = make_road(frame, oneway=True)
        assert legal_sides(road, "right") == ("right",)
        assert {c.side for c in generate_candidates(road, "right")} == {"right"}

    def test_reversed_oneway_offers_the_left_side(self, frame):
        """`oneway=-1` means legal travel runs opposite to the way's digitization
        direction. Offering the way's right there puts the car in oncoming
        traffic, and 71% of Miami ways carry a oneway tag, so this is not rare."""
        road = make_road(frame, oneway=True, oneway_reversed=True)
        assert legal_sides(road, "right") == ("left",)
        assert {c.side for c in generate_candidates(road, "right")} == {"left"}

    def test_left_hand_traffic_flips_the_kept_side(self, frame):
        road = make_road(frame, oneway=True)
        assert legal_sides(road, "left") == ("left",)

    def test_left_hand_traffic_flips_reversed_too(self, frame):
        road = make_road(frame, oneway=True, oneway_reversed=True)
        assert legal_sides(road, "left") == ("right",)


class TestCurbBearing:
    def test_bearing_points_from_kerb_toward_sidewalk(self, frame):
        """S3 aims its camera with this. On a west-to-east road the sidewalk is
        south on the right side (180 deg) and north on the left (0 deg).

        Compared circularly: a parallel is not exactly east-west once projected,
        and 359.7 deg is 0.3 deg from north, not 359.7.
        """
        road = make_road(frame)
        by_side = {c.side: c.curb_bearing_deg for c in generate_candidates(road, "right")}
        assert bearing_delta_deg(by_side["right"], 180.0) == pytest.approx(0.0, abs=0.5)
        assert bearing_delta_deg(by_side["left"], 0.0) == pytest.approx(0.0, abs=0.5)

    def test_bearing_is_always_a_compass_angle(self, frame):
        road = make_road(frame)
        for c in generate_candidates(road, "right"):
            assert 0.0 <= c.curb_bearing_deg < 360.0

    def test_bearing_is_perpendicular_to_the_road(self, frame):
        """Independent of the compass: the normal must be at 90 deg to the tangent,
        which is the property S3 actually depends on."""
        base_lat, base_lng = TEST_ORIGIN
        # a diagonal road, so a fixed compass answer would be wrong
        road = make_road(
            frame,
            latlngs=[
                (base_lat - 0.001, base_lng - 0.001),
                (base_lat + 0.001, base_lng + 0.001),
            ],
        )
        for c in generate_candidates(road, "right"):
            s = c.arc_m
            cx, cy, tx, ty = road.polyline.point_at(s)
            # vector from the centreline point to the candidate
            ox, oy = c.x - cx, c.y - cy
            dot = ox * tx + oy * ty
            assert dot == pytest.approx(0.0, abs=1e-6), "normal is not perpendicular"
            assert math.hypot(ox, oy) == pytest.approx(c.offset_m, abs=1e-6)


class TestCorridorValidation:
    def test_geometry_blowup_is_rejected(self, frame):
        """A hairpin's offset point lands far from its own centerline. That is the
        signature of a geometry artifact, and the candidate is dropped rather than
        offered as a real curb."""
        base_lat, base_lng = TEST_ORIGIN
        # a near-180 degree reversal: a classic miter-spike generator
        road = make_road(
            frame,
            left_offset=6.0,
            right_offset=6.0,
            latlngs=[
                (base_lat, base_lng - 0.0005),
                (base_lat + 0.0001, base_lng),
                (base_lat, base_lng - 0.0005),
            ],
        )
        for c in generate_candidates(road, "right"):
            # Whatever survives must still be a plausible distance from the road.
            assert road.polyline.distance_to(c.x, c.y) <= c.offset_m + 1.0

    def test_tight_hairpin_produces_fewer_candidates(self, frame):
        """The safety net should actually fire, not just be present in the code."""
        base_lat, base_lng = TEST_ORIGIN
        squiggle = [
            (base_lat, base_lng - 0.0005),
            (base_lat + 0.0001, base_lng),
            (base_lat, base_lng - 0.0005),
        ]
        spiky = make_road(frame, right_offset=8.0, latlngs=squiggle)
        gentle = make_road(
            frame,
            right_offset=8.0,
            latlngs=[
                (base_lat, base_lng - 0.0005),
                (base_lat + 0.00002, base_lng),
                (base_lat, base_lng + 0.0005),
            ],
        )
        n_spiky = len(generate_candidates(spiky, "right"))
        n_gentle = len(generate_candidates(gentle, "right"))
        assert n_spiky <= n_gentle

    def test_a_doubled_back_way_does_not_offer_a_spot_in_the_travel_lane(self, frame):
        """The other direction of the same guard.

        A way that runs back alongside itself -- a parking-aisle ring is the real
        case in the FIU demo area -- puts a second centerline within a metre or
        two of a candidate's own curb point. Without the check we offer that point
        as a kerb when it is actually inside the aisle beside it. This was the
        only genuine violation in the section 6 definition-of-done check.
        """
        base_lat, base_lng = TEST_ORIGIN
        # a narrow ring: two parallel legs ~3 m apart
        ring = [
            (base_lat, base_lng - 0.00005),
            (base_lat, base_lng + 0.00005),
            (base_lat + 0.00003, base_lng + 0.00005),
            (base_lat + 0.00003, base_lng - 0.00005),
        ]
        road = make_road(frame, left_offset=2.5, right_offset=2.5, latlngs=ring)
        for c in generate_candidates(road, "right"):
            assert road.polyline.distance_to(c.x, c.y) >= c.offset_m - 1.0, (
                f"offered a spot {road.polyline.distance_to(c.x, c.y):.2f} m from a "
                f"centerline, on a {c.offset_m:.2f} m offset -- that is the travel lane"
            )
