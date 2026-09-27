"""Ranking: walk distance, deduplication, deterministic ordering, capping.

Determinism is the theme here. ENDPOINT.md section 6 S1 step 7 says "deduplicate
within 5 m ... and return the nearest 30" without saying which of a cluster
survives, so the tie-break is ours to define -- and it has to be deterministic or
the same request returns different spots, which breaks caching, makes the
recorded demo unreproducible, and makes these tests flaky.
"""

from __future__ import annotations

import pytest

from shared.config import DEDUPE_RADIUS_M, DETOUR_FACTOR, MAX_SPOTS
from shared.models import Confidence, LatLng, LegalityBasis
from services.s1_legal_spots.legality import SideLegality, Verdict
from services.s1_legal_spots.ranking import (
    ScoredCandidate,
    dedupe,
    is_across_street,
    rank,
    walk_distance,
)

from .conftest import TEST_ORIGIN, make_road

RIDER = LatLng(lat=TEST_ORIGIN[0], lng=TEST_ORIGIN[1])


@pytest.fixture
def rider_xy(frame):
    """The rider's position in the local metric frame.

    Ranking filters by true distance from the rider, so test candidates have to be
    positioned relative to this, not relative to the projected origin.
    """
    return frame.to_m(RIDER.lat, RIDER.lng)


def make_scored(
    frame,
    x: float,
    y: float,
    *,
    walk: float | None = None,
    confidence: Confidence = Confidence.UNVERIFIED,
    clearance: float = 5.0,
    way_id: str = "way/1",
    side: str = "right",
    arc_m: float = 0.0,
) -> ScoredCandidate:
    """Build a ScoredCandidate directly, bypassing geometry, for ranking tests."""
    from services.s1_legal_spots.curb import Candidate

    road = make_road(frame, way_id=way_id)
    cand = Candidate(
        road=road,
        side=side,
        arc_m=arc_m,
        x=x,
        y=y,
        curb_bearing_deg=180.0,
        offset_m=3.5,
    )
    verdict = Verdict(
        candidate=cand,
        accepted=True,
        legality=SideLegality("permissive", LegalityBasis.INFERRED_STANDARD),
        clearance_m=clearance,
        confidence=confidence,
    )
    straight = 0.0 if walk is None else walk / DETOUR_FACTOR
    return ScoredCandidate(
        verdict=verdict,
        walk_distance_m=walk if walk is not None else 0.0,
        straight_distance_m=straight,
        across_street=False,
    )


def near_rider(frame, rider_xy, metres: float, *, dy: float = 0.0, **kw) -> ScoredCandidate:
    """A candidate ``metres`` from the rider, offset ``dy`` metres across it."""
    return make_scored(frame, rider_xy[0] + metres, rider_xy[1] + dy, **kw)


class TestWalkDistance:
    def test_detour_factor_is_applied(self, frame):
        assert walk_distance(frame, RIDER, 100.0, across=False) == pytest.approx(130.0, abs=0.1)

    def test_crossing_the_street_costs_more(self, frame):
        """Reaching a spot across a live carriageway is materially harder than
        walking along your own side, especially with a cane or walker. ENDPOINT.md
        applies one flat 1.3x factor, which under-prices exactly the curb a
        mobility-needs rider would rather not cross to."""
        same = walk_distance(frame, RIDER, 100.0, across=False)
        across = walk_distance(frame, RIDER, 100.0, across=True)
        assert across > same

    def test_result_is_rounded_to_a_readable_precision(self, frame):
        # The UI shows this number; sub-decimal-metre precision is noise.
        assert walk_distance(frame, RIDER, 33.3333, across=False) == 43.3


class TestAcrossStreetDetection:
    def test_rider_left_of_a_eastbound_road_is_on_the_left(self, frame):
        """The test road runs west to east, so north is LEFT."""
        road = make_road(frame)
        cx, cy, _, _ = road.polyline.point_at(50.0)
        for cand_side, delta_y, expected in [
            ("left", +20.0, False),   # rider north, candidate north: same side
            ("right", +20.0, True),   # rider north, candidate south: across
            ("left", -20.0, True),
            ("right", -20.0, False),
        ]:
            sc = make_scored(frame, cx, cy, side=cand_side)
            assert is_across_street(frame, (cx, cy + delta_y), sc) is expected, (
                f"side={cand_side} dy={delta_y}"
            )


class TestDedupe:
    def test_closer_than_radius_collapses(self, frame, rider_xy):
        a = near_rider(frame, rider_xy, 0.0, walk=10.0, way_id="way/1")
        b = near_rider(frame, rider_xy, DEDUPE_RADIUS_M * 0.5, walk=20.0, way_id="way/2")
        assert len(dedupe([a, b])) == 1

    def test_farther_than_radius_survives(self, frame, rider_xy):
        a = near_rider(frame, rider_xy, 0.0, walk=10.0, way_id="way/1")
        b = near_rider(frame, rider_xy, DEDUPE_RADIUS_M * 1.5, walk=20.0, way_id="way/2")
        assert len(dedupe([a, b])) == 2

    def test_higher_confidence_wins_a_cluster(self, frame, rider_xy):
        weak = near_rider(
            frame, rider_xy, 0.0, walk=10.0, confidence=Confidence.UNVERIFIED, way_id="way/1"
        )
        strong = near_rider(
            frame, rider_xy, 1.0, walk=30.0, confidence=Confidence.LIKELY, way_id="way/2"
        )
        kept = dedupe([weak, strong])
        assert len(kept) == 1
        assert kept[0].verdict.confidence is Confidence.LIKELY

    def test_more_clearance_wins_at_equal_confidence(self, frame, rider_xy):
        near_hydrant = near_rider(frame, rider_xy, 0.0, walk=10.0, clearance=2.0, way_id="way/1")
        far_hydrant = near_rider(frame, rider_xy, 1.0, walk=30.0, clearance=9.0, way_id="way/2")
        kept = dedupe([near_hydrant, far_hydrant])
        assert len(kept) == 1
        assert kept[0].verdict.clearance_m == 9.0

    def test_dedupe_is_order_independent(self, frame, rider_xy):
        """Same input set, any iteration order, same output. Otherwise caching and
        the recorded demo are both unreliable."""
        items = [
            near_rider(frame, rider_xy, 0.0, walk=10.0, way_id="way/1"),
            near_rider(frame, rider_xy, 2.0, walk=20.0, way_id="way/2"),
            near_rider(frame, rider_xy, 40.0, walk=30.0, way_id="way/3"),
        ]
        a = [s.verdict.candidate.road.way_id for s in dedupe(items)]
        b = [s.verdict.candidate.road.way_id for s in dedupe(list(reversed(items)))]
        assert a == b


class TestRank:
    def test_sorts_by_walk_distance(self, frame, rider_xy):
        items = [
            near_rider(frame, rider_xy, 50.0, walk=50.0, way_id="way/1"),
            near_rider(frame, rider_xy, 100.0, walk=10.0, way_id="way/2"),
            near_rider(frame, rider_xy, 150.0, walk=30.0, way_id="way/3"),
        ]
        spots, _, _ = rank(frame, RIDER, items, radius_m=500.0)
        assert [s.walk_distance_m for s in spots] == [10.0, 30.0, 50.0]

    def test_spot_ids_are_sequential_and_stable(self, frame, rider_xy):
        items = [
            near_rider(frame, rider_xy, 40.0 * i, walk=10.0 * i, way_id=f"way/{i}")
            for i in range(1, 4)
        ]
        spots, _, _ = rank(frame, RIDER, items, radius_m=500.0)
        assert [s.spot_id for s in spots] == ["s1_0001", "s1_0002", "s1_0003"]

    def test_caps_and_flags_truncation(self, frame, rider_xy):
        items = [
            near_rider(frame, rider_xy, float(i) * 12.0, walk=float(i) * 6.0, way_id=f"way/{i}")
            for i in range(MAX_SPOTS + 7)
        ]
        spots, total, truncated = rank(frame, RIDER, items, radius_m=500.0)
        assert len(spots) == MAX_SPOTS
        assert total == MAX_SPOTS + 7
        assert truncated is True

    def test_does_not_flag_truncation_when_everything_fits(self, frame, rider_xy):
        items = [
            near_rider(frame, rider_xy, float(i) * 40.0, walk=float(i) * 6.0, way_id=f"way/{i}")
            for i in range(3)
        ]
        spots, total, truncated = rank(frame, RIDER, items, radius_m=500.0)
        assert (len(spots), total, truncated) == (3, 3, False)

    def test_drops_candidates_outside_the_radius(self, frame, rider_xy):
        """Overpass `around:` returns ways with any node in range, so a road can
        contribute candidates well beyond the requested radius. Those must not
        appear in the response."""
        near = near_rider(frame, rider_xy, 50.0, walk=65.0, way_id="way/near")
        far = near_rider(frame, rider_xy, 900.0, walk=1170.0, way_id="way/far")
        spots, total, _ = rank(frame, RIDER, [near, far], radius_m=150.0)
        assert [s.segment_id for s in spots] == ["way/near"]
        assert total == 1

    def test_identical_input_gives_identical_output(self, frame, rider_xy):
        items = [
            near_rider(frame, rider_xy, float(i) * 12.0, walk=float(i) * 4.0, way_id=f"way/{i}")
            for i in range(12)
        ]
        first, _, _ = rank(frame, RIDER, items, radius_m=500.0)
        second, _, _ = rank(frame, RIDER, list(reversed(items)), radius_m=500.0)
        assert [s.model_dump() for s in first] == [s.model_dump() for s in second]
