"""Legality rules: the actual definition of done for S1.

ENDPOINT.md section 6 S1 finishes with "for 3 test locations in the demo area, the
UI shows spots on both sides of the street, none within the hydrant, crosswalk,
bus stop, or bike-lane exclusions". That is not checkable by eye on a map, so
each rule gets an isolated scenario here with an assertion a human wrote down.

Orientation convention used throughout, matching ``conftest.make_road``: the test
road runs west to east, so its RIGHT side is south (-y) and its LEFT side is north
(+y).
"""

from __future__ import annotations

import pytest

from shared.config import (
    BUFFER_BUS_STOP_M,
    BUFFER_CROSSWALK_M,
    BUFFER_FIRE_HYDRANT_M,
    BUFFER_INTERSECTION_M,
)
from shared.models import Confidence, LegalityBasis, RestrictionKind
from services.s1_legal_spots.curb import Candidate, generate_candidates
from services.s1_legal_spots.legality import (
    cycleway_blocks,
    judge_candidate,
    resolve_side_legality,
)

from .conftest import arc_restriction, make_road, point_restriction


# --------------------------------------------------------------------------- #
# Parking side tags
# --------------------------------------------------------------------------- #

class TestSideLegality:
    def test_untagged_side_is_permissive_but_inferred(self):
        """The permissive default, and the reason it is safe to have: it is
        labelled as an inference and capped at `likely`, not asserted as fact."""
        road = make_road(None, tags={})  # type: ignore[arg-type]
        v = resolve_side_legality(road, "right")
        assert v.verdict == "permissive"
        assert v.basis == LegalityBasis.INFERRED_STANDARD

    @pytest.mark.parametrize("side", ["left", "right"])
    @pytest.mark.parametrize("value", ["no", "no_parking", "no_stopping", "customers"])
    def test_restrictive_values_block_that_side_only(self, side, value):
        other = "right" if side == "left" else "left"
        road = make_road(None, tags={f"parking:{side}": value})  # type: ignore[arg-type]
        assert resolve_side_legality(road, side).verdict == "restrictive"
        assert resolve_side_legality(road, other).verdict == "permissive"

    @pytest.mark.parametrize("value", ["yes", "parallel", "designated"])
    def test_permissive_values_are_tagged_as_such(self, value):
        """An explicit permissive tag is a fact, not an inference -- this is the
        only path to `likely` on a road with no width evidence."""
        road = make_road(None, tags={"parking:right": value})  # type: ignore[arg-type]
        v = resolve_side_legality(road, "right")
        assert v.verdict == "permissive"
        assert v.basis == LegalityBasis.TAGGED_PERMISSIVE

    def test_no_stopping_restriction_is_a_hard_override(self):
        """`parking:left:restriction=no_stopping` must win over a permissive
        `parking:left=parallel` on the same way."""
        road = make_road(
            None,  # type: ignore[arg-type]
            tags={"parking:left": "parallel", "parking:left:restriction": "no_stopping"},
        )
        v = resolve_side_legality(road, "left")
        assert v.verdict == "restrictive"
        assert "restriction" in v.source_tag

    def test_whole_street_tags_apply_to_both_sides(self):
        road = make_road(None, tags={"parking:both": "no"})  # type: ignore[arg-type]
        assert resolve_side_legality(road, "left").verdict == "restrictive"
        assert resolve_side_legality(road, "right").verdict == "restrictive"

    def test_side_tag_takes_precedence_over_whole_street_tag(self):
        """`parking:left=parallel` must beat `parking:lane:both=no`; otherwise a
        single per-side exception is silently overridden."""
        road = make_road(
            None,  # type: ignore[arg-type]
            tags={"parking:left": "parallel", "parking:lane:both": "no"},
        )
        assert resolve_side_legality(road, "left").verdict == "permissive"


# --------------------------------------------------------------------------- #
# Cycleways
# --------------------------------------------------------------------------- #

class TestCyclewayExclusion:
    def test_in_lane_cycleway_blocks_its_side(self):
        road = make_road(None, tags={"cycleway:right": "lane"})  # type: ignore[arg-type]
        assert cycleway_blocks(road, "right") != ""
        assert cycleway_blocks(road, "left") == ""

    @pytest.mark.parametrize("value", ["separate", "opposite", "opposite_lane", "no"])
    def test_non_obstructing_values_keep_the_curb(self, value):
        """`separate` is an off-roadway path and `opposite*` belongs to the other
        carriageway. Treating them as obstructions deletes legal curb on exactly
        the wide busy roads where a rider has the fewest options."""
        road = make_road(None, tags={"cycleway:right": value})  # type: ignore[arg-type]
        assert cycleway_blocks(road, "right") == ""

    def test_bare_cycleway_on_twoway_blocks_both_sides(self):
        road = make_road(None, tags={"cycleway": "lane"})  # type: ignore[arg-type]
        assert cycleway_blocks(road, "left") != ""
        assert cycleway_blocks(road, "right") != ""

    def test_bare_cycleway_on_oneway_blocks_only_the_travel_side(self):
        road = make_road(
            None,  # type: ignore[arg-type]
            tags={"cycleway": "lane", "oneway": "yes"},
            oneway=True,
        )
        assert cycleway_blocks(road, "right") != ""
        assert cycleway_blocks(road, "left") == ""


# --------------------------------------------------------------------------- #
# Typed restrictions
# --------------------------------------------------------------------------- #

def _first_candidate(road, side: str) -> Candidate:
    cands = [c for c in generate_candidates(road, "right") if c.side == side]
    assert cands, f"no candidates generated for {side}"
    return cands[0]


class TestFireHydrant:
    def test_blocks_near_the_hydrant(self, frame):
        road = make_road(frame)
        cand = _first_candidate(road, "right")
        hydrant = point_restriction(
            frame,
            RestrictionKind.FIRE_HYDRANT,
            "node/1",
            BUFFER_FIRE_HYDRANT_M,
            at=frame.to_ll(cand.x, cand.y),  # type: ignore[arg-type]
        )
        v = judge_candidate(cand, [hydrant], [])
        assert not v.accepted
        assert v.restriction is hydrant
        assert "hydrant" in v.reason

    def test_far_hydrant_does_not_block(self, frame):
        """A 4.6 m disc must not delete the whole street. This is the regression
        test for using discs for everything."""
        road = make_road(frame)
        cand = _first_candidate(road, "right")
        far = point_restriction(
            frame,
            RestrictionKind.FIRE_HYDRANT,
            "node/2",
            BUFFER_FIRE_HYDRANT_M,
            at=frame.to_ll(cand.x + 40.0, cand.y),  # type: ignore[arg-type]
        )
        assert judge_candidate(cand, [far], []).accepted


class TestCrossingIsLinearNotDisc:
    def test_blocks_within_extent_along_the_road(self, frame):
        road = make_road(frame)
        crossing = arc_restriction(
            RestrictionKind.CROSSING,
            "node/3",
            road.way_id,
            anchor_m=100.0,
            extent_m=BUFFER_CROSSWALK_M,
            buffer_m=BUFFER_CROSSWALK_M,
        )
        cands = [c for c in generate_candidates(road, "right")]
        near = [c for c in cands if abs(c.arc_m - 100.0) <= BUFFER_CROSSWALK_M]
        assert near, "fixture road is too short to contain a crossing"
        for c in near:
            assert not judge_candidate(c, [], [crossing]).accepted

    def test_does_not_block_up_the_street(self, frame):
        """The whole reason crossings are linear extents: a disc would also delete
        curb 40 m away and on the cross street, emptying the map."""
        road = make_road(frame)
        crossing = arc_restriction(
            RestrictionKind.CROSSING,
            "node/4",
            road.way_id,
            anchor_m=100.0,
            extent_m=BUFFER_CROSSWALK_M,
            buffer_m=BUFFER_CROSSWALK_M,
        )
        far = [c for c in generate_candidates(road, "right") if abs(c.arc_m - 100.0) > 30.0]
        assert far, "fixture road is too short"
        for c in far:
            assert judge_candidate(c, [], [crossing]).accepted

    def test_blocks_both_sides(self, frame):
        """A crosswalk spans the carriageway, so it must block both curbs."""
        road = make_road(frame)
        crossing = arc_restriction(
            RestrictionKind.CROSSING,
            "node/5",
            road.way_id,
            anchor_m=100.0,
            extent_m=BUFFER_CROSSWALK_M,
            buffer_m=BUFFER_CROSSWALK_M,
        )
        for side in ("left", "right"):
            cands = [c for c in generate_candidates(road, "right") if c.side == side]
            assert any(
                not judge_candidate(c, [], [crossing]).accepted for c in cands
            ), f"crossing did not block the {side} curb"


class TestBusStopIsOneSideOnly:
    def test_blocks_only_its_own_curb(self, frame):
        """A bus stop sits at one kerb. Blocking both sides would remove a
        usable pickup place across the street for no safety gain."""
        road = make_road(frame)
        stop = arc_restriction(
            RestrictionKind.BUS_STOP,
            "node/6",
            road.way_id,
            anchor_m=100.0,
            extent_m=BUFFER_BUS_STOP_M,
            buffer_m=BUFFER_BUS_STOP_M,
            side="right",
            label="bus stop",
        )
        for c in generate_candidates(road, "right"):
            v = judge_candidate(c, [], [stop])
            if c.side == "right" and abs(c.arc_m - 100.0) <= BUFFER_BUS_STOP_M:
                assert not v.accepted
            elif c.side == "left":
                assert v.accepted, "bus stop leaked onto the opposite curb"


class TestIntersection:
    def test_blocks_junction_on_the_sharing_way(self, frame):
        road = make_road(frame)
        junction = arc_restriction(
            RestrictionKind.INTERSECTION,
            "node/7",
            road.way_id,
            anchor_m=0.0,
            extent_m=BUFFER_INTERSECTION_M,
            buffer_m=BUFFER_INTERSECTION_M,
        )
        cands = generate_candidates(road, "right")
        blocked = [c for c in cands if not judge_candidate(c, [], [junction]).accepted]
        assert blocked, "intersection at the start of the road blocked nothing"
        assert all(c.arc_m <= BUFFER_INTERSECTION_M for c in blocked)

    def test_does_not_apply_to_a_different_way(self, frame):
        """An arc restriction is anchored to one way id. Without that check a
        crossing on one street would delete curb on every parallel street."""
        road = make_road(frame, way_id="way/1")
        crossing = arc_restriction(
            RestrictionKind.CROSSING,
            "node/8",
            "way/999",
            anchor_m=100.0,
            extent_m=BUFFER_CROSSWALK_M,
            buffer_m=BUFFER_CROSSWALK_M,
        )
        assert all(judge_candidate(c, [], [crossing]).accepted for c in generate_candidates(road, "right"))


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #

class TestConfidenceCeiling:
    def test_s1_never_emits_verified_or_detected(self, frame):
        """`verified` is reserved for official regulation sources and `detected`
        for S3 Vision. There is no official feed for the demo area, so both are
        unreachable from S1 by construction -- and the label must never lie."""
        roads = [
            make_road(frame, way_id="way/1", tags={}),
            make_road(frame, way_id="way/2", tags={"parking:both": "parallel"}),
            make_road(frame, way_id="way/3", tags={"parking:both": "yes", "lanes": "4"}),
        ]
        for road in roads:
            for cand in generate_candidates(road, "right"):
                v = judge_candidate(cand, [], [])
                if v.accepted:
                    assert v.confidence in (
                        Confidence.LIKELY,
                        Confidence.UNVERIFIED,
                    )

    def test_never_offers_a_restrictive_side(self, frame):
        road = make_road(frame, tags={"parking:right": "no"})
        for cand in generate_candidates(road, "right"):
            if cand.side == "right":
                assert not judge_candidate(cand, [], []).accepted
