"""Tests for the predictive/vision fusion step (ENDPOINT.md §7).

Two properties here are load-bearing, and both are properties the doc's
pseudocode gets wrong in a way that only shows up at demo time:

  1. **Alternatives come only from the S1 candidate list.** Vision may confirm
     or discredit a spot the legality service already cleared, but it can never
     introduce one. That constraint is what stops a hallucinated awning across
     the street from becoming a pickup in a travel lane.

  2. **Switching has to clear a bar.** Without hysteresis the car oscillates:
     a marginally better spot arrives, the car re-routes, a frame later the other
     one looks better. A pickup point that visibly flip-flops is worse for a
     rider with mobility needs than one that is merely not quite optimal.
"""

from __future__ import annotations

import pytest

from orchestrator import fusion
from orchestrator.fusion import choose, fuse, pick_alternatives
from shared.models import Confidence, LatLng, RankedSpot, Side, Spot, VisionAssessment

pytestmark = pytest.mark.anyio


def spot(spot_id: str, walk_m: float, *, lat: float, lng: float, conf=Confidence.LIKELY) -> RankedSpot:
    return RankedSpot(
        spot=Spot(
            spot_id=spot_id,
            stop_point=LatLng(lat=lat, lng=lng),
            side=Side.RIGHT,
            curb_bearing_deg=90.0,
            walk_distance_m=walk_m,
            street_name="SW 109th Ave",
            confidence=conf,
        ),
        score=0.5,
        confidence=conf,
    )


def vision(spot_id: str, score: float | None, conf: float = 0.5, reason: str = "") -> VisionAssessment:
    return VisionAssessment(
        spot_id=spot_id,
        mode="rain",
        vision_score=score,
        model_confidence=conf,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# fuse
# --------------------------------------------------------------------------- #

class TestFuse:
    def test_no_vision_result_keeps_the_predictive_score(self):
        """S3 is out of scope in this build, so this is the *common* path, not an
        edge case. Fusion must be a no-op when there is nothing to fuse."""
        p = spot("s1_0001", 20.0, lat=25.757, lng=-80.372)
        out = fuse(p, None)
        assert out.score == p.score
        assert out.confidence == p.confidence
        assert out.denied_by_vision is False
        assert out.promoted_by_vision is False

    def test_a_confident_negative_overrides_a_good_map_score(self):
        """The map thinks there is an awning; the camera, looking at the actual
        kerb, says there is not. The camera wins -- it is the only source that
        has actually looked."""
        p = spot("s1_0001", 20.0, lat=25.757, lng=-80.372)
        p.score = 0.95
        out = fuse(p, vision("s1_0001", 0.05, conf=0.9, reason="bare kerb, no awning"))
        assert out.score < 0.5
        assert out.confidence == Confidence.UNVERIFIED
        assert out.denied_by_vision is True
        assert "bare kerb" in out.reason

    def test_a_low_confidence_negative_does_not_override(self):
        """An unsure model must not be able to veto a good map score. Otherwise a
        single blurry frame costs the rider their covered pickup."""
        p = spot("s1_0001", 20.0, lat=25.757, lng=-80.372)
        p.score = 0.95
        out = fuse(p, vision("s1_0001", 0.02, conf=0.3))
        assert out.denied_by_vision is False
        assert out.score > 0.5

    def test_a_confident_positive_promotes_an_inference_to_observed(self):
        """This is the only path by which a spot can leave `unverified` without
        an official regulation feed -- and it is what will carry trust at the
        Graham Center, where S1 returns 100% unverified."""
        p = spot("s1_0001", 20.0, lat=25.757, lng=-80.372, conf=Confidence.UNVERIFIED)
        out = fuse(p, vision("s1_0001", 0.9, conf=0.85))
        assert out.confidence == Confidence.DETECTED
        assert out.promoted_by_vision is True

    def test_it_does_not_promote_something_already_better_than_detected(self):
        """`verified` outranks `detected`; promoting it would downgrade a spot."""
        p = spot("s1_0001", 20.0, lat=25.757, lng=-80.372, conf=Confidence.VERIFIED)
        out = fuse(p, vision("s1_0001", 0.9, conf=0.9))
        assert out.confidence == Confidence.VERIFIED
        assert out.promoted_by_vision is False

    def test_the_blend_is_the_documented_60_40(self):
        p = spot("s1_0001", 20.0, lat=25.757, lng=-80.372)
        p.score = 0.8
        out = fuse(p, vision("s1_0001", 0.4, conf=0.5))
        assert out.score == pytest.approx(0.6 * 0.8 + 0.4 * 0.4)

    def test_fuse_does_not_mutate_its_input(self):
        """§7's pseudocode sets `pred.confidence = "detected"` inside the scoring
        function. That makes the outcome depend on call order and is very hard to
        diagnose from a log line; the confidence change is returned as data
        instead."""
        p = spot("s1_0001", 20.0, lat=25.757, lng=-80.372, conf=Confidence.UNVERIFIED)
        before = p.model_dump()
        fuse(p, vision("s1_0001", 0.95, conf=0.9))
        assert p.model_dump() == before

    def test_a_none_vision_score_is_treated_as_no_evidence(self):
        """A model that declines to score is not evidence of absence."""
        p = spot("s1_0001", 20.0, lat=25.757, lng=-80.372)
        p.score = 0.9
        out = fuse(p, vision("s1_0001", None, conf=0.95))
        assert out.denied_by_vision is False
        assert out.score == 0.9


# --------------------------------------------------------------------------- #
# pick_alternatives
# --------------------------------------------------------------------------- #

class TestPickAlternatives:
    def test_keeps_only_neighbours_within_the_radius(self):
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        near = spot("s1_0002", 25.0, lat=25.7571, lng=-80.3720)   # ~11 m
        far = spot("s1_0003", 30.0, lat=25.7585, lng=-80.3720)    # ~167 m
        out = pick_alternatives(p, [p, near, far], radius_m=40.0, limit=2)
        assert [c.spot.spot_id for c in out] == ["s1_0002"]

    def test_never_returns_the_prediction_itself(self):
        p = spot("s1_0001", 20.0, lat=25.757, lng=-80.372)
        out = pick_alternatives(p, [p], radius_m=40.0, limit=2)
        assert out == []

    def test_returns_at_most_the_limit(self):
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        many = [spot(f"s1_00{i:02d}", 20.0 + i, lat=25.7570 + i * 0.00005, lng=-80.3720)
                for i in range(2, 12)]
        out = pick_alternatives(p, [p, *many], radius_m=40.0, limit=2)
        assert len(out) == 2

    def test_ordered_by_walk_distance(self):
        p = spot("s1_0001", 20.0, lat=25.75700, lng=-80.3720)
        a = spot("s1_0002", 60.0, lat=25.75705, lng=-80.3720)
        b = spot("s1_0003", 30.0, lat=25.75702, lng=-80.3720)
        out = pick_alternatives(p, [p, a, b], radius_m=40.0, limit=2)
        assert [c.spot.walk_distance_m for c in out] == [30.0, 60.0]

    def test_a_spot_farther_to_walk_can_still_be_an_alternative(self):
        """Radius is about geographic proximity, not walk distance. Both are
        meaningful, and conflating them would hide a spot that is close on the map
        but awkward to reach."""
        p = spot("s1_0001", 10.0, lat=25.75700, lng=-80.3720)
        a = spot("s1_0002", 95.0, lat=25.75705, lng=-80.3720)
        out = pick_alternatives(p, [p, a], radius_m=40.0, limit=2)
        assert [c.spot.spot_id for c in out] == ["s1_0002"]


# --------------------------------------------------------------------------- #
# choose
# --------------------------------------------------------------------------- #

class TestChoose:
    def test_confirms_the_prediction_when_the_camera_agrees(self):
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        p.score = 0.9
        d = choose(p, [], {p.spot.spot_id: vision(p.spot.spot_id, 0.85, conf=0.9)})
        assert d.switched is False
        assert d.chosen.spot.spot_id == "s1_0001"
        assert "confirmed" in d.reason

    def test_switches_to_a_clearly_better_spotted_alternative(self):
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        p.score = 0.5
        a = spot("s1_0002", 30.0, lat=25.7571, lng=-80.3720)
        a.score = 0.5
        d = choose(
            p, [a],
            {
                p.spot.spot_id: vision(p.spot.spot_id, 0.1, conf=0.9),
                a.spot.spot_id: vision(a.spot.spot_id, 0.95, conf=0.9, reason="deep awning"),
            },
        )
        assert d.switched is True
        assert d.chosen.spot.spot_id == "s1_0002"

    def test_does_not_switch_for_a_marginal_improvement(self):
        """The hysteresis rule. Without it the car oscillates, and a pickup point
        that visibly flip-flops is worse than a slightly suboptimal one."""
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        p.score = 0.50
        a = spot("s1_0002", 20.0, lat=25.7571, lng=-80.3720)
        a.score = 0.50
        # 0.5/0.4 vs 0.9/0.4: alternative wins by less than SWITCH_MARGIN
        d = choose(
            p, [a],
            {
                p.spot.spot_id: vision(p.spot.spot_id, 0.50, conf=0.5),
                a.spot.spot_id: vision(a.spot.spot_id, 0.52, conf=0.5),
            },
        )
        assert d.switched is False
        assert "not enough" in d.reason

    def test_does_not_switch_to_a_spot_that_costs_too_much_extra_walking(self):
        """A switch that costs the rider more effort than it saves them defeats
        the purpose of prioritising cover in the first place."""
        p = spot("s1_0001", 10.0, lat=25.7570, lng=-80.3720)
        p.score = 0.5
        a = spot("s1_0002", 10.0 + fusion.MAX_EXTRA_WALK_M + 10, lat=25.7571, lng=-80.3720)
        a.score = 0.5
        d = choose(
            p, [a],
            {
                p.spot.spot_id: vision(p.spot.spot_id, 0.05, conf=0.9),
                a.spot.spot_id: vision(a.spot.spot_id, 0.98, conf=0.9),
            },
        )
        assert d.switched is False
        assert "farther to walk" in d.reason

    def test_survives_a_missing_assessment_for_an_alternative(self):
        """S3 may have scored only the predicted spot. An unassessed alternative
        must not be treated as a confirmed failure."""
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        p.score = 0.9
        a = spot("s1_0002", 20.0, lat=25.7571, lng=-80.3720)
        d = choose(p, [a], {p.spot.spot_id: vision(p.spot.spot_id, 0.9, conf=0.9)})
        assert d.chosen.spot.spot_id == "s1_0001"

    def test_no_vision_at_all_keeps_the_prediction(self):
        """The S3-absent path, which is the default in this build."""
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        p.score = 0.9
        a = spot("s1_0002", 20.0, lat=25.7571, lng=-80.3720)
        a.score = 0.9
        d = choose(p, [a], {})
        assert d.switched is False
        assert d.chosen.spot.spot_id == "s1_0001"

    def test_no_vision_at_all_claims_no_camera_finding(self):
        """REGRESSION. With S3 absent, `choose` used to report "camera confirmed
        the predicted spot", and the rider was told the camera had checked a spot
        no camera ever saw."""
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        d = choose(p, [], {})
        assert d.vision_ran is False
        assert d.reason == ""

    def test_an_assessment_without_a_score_does_not_count_as_vision(self):
        """S3's documented fallback is `vision_score: null`. That is a failed
        look, not a confirmation."""
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        d = choose(p, [], {p.spot.spot_id: vision(p.spot.spot_id, None, conf=0.9)})
        assert d.vision_ran is False
        assert "confirmed" not in d.reason

    def test_a_denied_prediction_that_is_kept_is_not_called_confirmed(self):
        """The camera said the cover is not there, and no alternative was clearly
        better, so the prediction stands. Calling that "confirmed" inverts what the
        camera actually reported."""
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        p.score = 0.9
        d = choose(p, [], {p.spot.spot_id: vision(p.spot.spot_id, 0.05, conf=0.9)})
        assert d.switched is False
        assert d.vision_ran is True
        assert "confirmed" not in d.reason
        assert "couldn't see the cover" in d.reason

    def test_every_considered_spot_is_reported(self):
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        a = spot("s1_0002", 20.0, lat=25.7571, lng=-80.3720)
        d = choose(p, [a], {})
        assert set(d.considered) == {"s1_0001", "s1_0002"}
        assert set(d.outcomes) == {"s1_0001", "s1_0002"}

    def test_does_not_crash_on_unhashable_candidates(self):
        """REGRESSION. §7's pseudocode does `dict(scored)[predicted]`, which
        requires `RankedSpot` to be hashable. Pydantic models are not, so the
        documented version raises TypeError on the one code path that decides
        where the car actually stops. Keying by spot_id avoids it."""
        p = spot("s1_0001", 20.0, lat=25.7570, lng=-80.3720)
        a = spot("s1_0002", 20.0, lat=25.7571, lng=-80.3720)
        with pytest.raises(TypeError):
            {p: 1}  # the documented shape really is invalid
        d = choose(p, [a], {})  # ours is not
        assert d.chosen.spot.spot_id == "s1_0001"
