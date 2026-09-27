"""Fusing the predictive ranking with what the camera actually saw.

ENDPOINT.md section 7 gives the pseudocode and the thresholds. The logic is
sound; two things in it are corrected here, and both are noted in place.

The design decision worth keeping is the constraint it inherits from §4.2:
alternatives come only from the S1 candidate list, because vision never invents a
stopping point. A camera that spots a lovely awning across the street cannot
propose parking there -- it can only confirm or discredit the spots the legality
service already cleared. That is the property that keeps a hallucinated awning
from becoming a pickup in a travel lane, so it is enforced structurally by
``choose`` only ever being handed ranked candidates.

The hysteresis rule is the other half. Without it the car oscillates: a
marginally-better spot arrives, the car re-routes, and a frame later the other
one looks better. A pickup point that visibly flip-flops is worse for a rider with
mobility needs than one that is simply not quite optimal, so switching has to
clear a bar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from shared.geo import haversine_m
from shared.models import Confidence, RankedSpot, VisionAssessment

log = logging.getLogger("endpoint.orchestrator.fusion")

#: How much better a rival must score before we move the car. From §7.
SWITCH_MARGIN = 0.15

#: Vision weight in the blend. From §7: 0.6 predictive + 0.4 vision.
W_PREDICTIVE = 0.6
W_VISION = 0.4

#: A confident model saying "there is no cover here" overrides a good predictive
#: score. From §7.
CONFIDENCE_TO_DENY = 0.7
VISION_TO_DENY = 0.2

#: A confident "yes, there is cover" promotes an unverified map inference to
#: `detected`. From §7.
CONFIDENCE_TO_PROMOTE = 0.7
VISION_TO_PROMOTE = 0.7

#: Re-route only for a spot that is not dramatically farther. From §7.
MAX_EXTRA_WALK_M = 60.0

#: How far from the predicted spot an alternative may be (§7 uses 40 m).
ALTERNATIVE_RADIUS_M = 40.0


@dataclass
class FusionOutcome:
    """The result of scoring one candidate. Explicit rather than a bare float.

    A scoring function that mutates its argument (``fuse`` setting
    ``pred.confidence = "detected"`` in §7's pseudocode) makes the outcome depend
    on call order, which is very hard to debug from a log line. Returning the
    confidence change as data keeps the function pure.
    """

    score: float
    confidence: Confidence
    reason: str = ""
    denied_by_vision: bool = False
    promoted_by_vision: bool = False


def fuse(pred: RankedSpot, va: VisionAssessment | None) -> FusionOutcome:
    """Blend one predictive ranking with one vision assessment. Pure."""
    base_conf = pred.confidence

    if va is None or va.vision_score is None:
        return FusionOutcome(
            score=pred.score, confidence=base_conf,
            reason="no vision result; keeping the predictive ranking",
        )

    # A confident negative overrides a good map score. The map thinks there is an
    # awning; the camera, looking at the actual kerb, says there is not. The
    # camera wins, because it is the only source that has actually looked.
    if va.model_confidence >= CONFIDENCE_TO_DENY and va.vision_score < VISION_TO_DENY:
        return FusionOutcome(
            score=min(pred.score, 0.3),
            confidence=Confidence.UNVERIFIED,
            reason=va.reason or "camera did not find the cover the map showed",
            denied_by_vision=True,
        )

    # A confident positive promotes an inference to something observed.
    promoted = False
    conf = base_conf
    if va.vision_score >= VISION_TO_PROMOTE and base_conf == Confidence.UNVERIFIED:
        conf = Confidence.DETECTED
        promoted = True

    return FusionOutcome(
        score=W_PREDICTIVE * pred.score + W_VISION * va.vision_score,
        confidence=conf,
        reason=va.reason or "",
        promoted_by_vision=promoted,
    )


@dataclass
class Decision:
    """Which spot the car is going to, and why."""

    chosen: RankedSpot
    switched: bool = False
    reason: str = ""
    outcomes: dict[str, FusionOutcome] = field(default_factory=dict)
    considered: list[str] = field(default_factory=list)


def _dist_m(a, b) -> float:
    return haversine_m(a.lat, a.lng, b.lat, b.lng)


def pick_alternatives(
    predicted: RankedSpot,
    candidates: list[RankedSpot],
    radius_m: float = ALTERNATIVE_RADIUS_M,
    limit: int = 2,
) -> list[RankedSpot]:
    """The two nearby legal spots worth a second look.

    Filtered to the same street-side neighbourhood as the prediction, because a
    camera call costs real time (8s budget) and image budget, and because
    re-routing the car across a car park to save a walker 20 m is not a trade
    worth making for this rider.
    """
    anchor = predicted.spot.stop_point
    out = [
        c
        for c in candidates
        if c.spot.spot_id != predicted.spot.spot_id
        and _dist_m(c.spot.stop_point, anchor) <= radius_m
    ]
    out.sort(key=lambda c: (c.spot.walk_distance_m, c.spot.spot_id))
    return out[:limit]


def choose(
    predicted: RankedSpot,
    alternatives: list[RankedSpot],
    assessments: dict[str, VisionAssessment],
) -> Decision:
    """Score every candidate and decide whether to move the car.

    The hysteresis rule from §7, unchanged in substance: only switch for a spot
    that is clearly better (a real margin, not noise) and not much farther to
    walk (a switch that costs the rider more effort than it saves them defeats
    the purpose).
    """
    pool = [predicted, *alternatives]
    outcomes: dict[str, FusionOutcome] = {}
    for c in pool:
        outcomes[c.spot.spot_id] = fuse(c, assessments.get(c.spot.spot_id))

    current = outcomes[predicted.spot.spot_id]
    best_id = max(outcomes, key=lambda sid: outcomes[sid].score)
    best = next(c for c in pool if c.spot.spot_id == best_id)

    if best_id == predicted.spot.spot_id:
        return Decision(
            chosen=predicted, switched=False,
            reason="camera confirmed the predicted spot",
            outcomes=outcomes, considered=[c.spot.spot_id for c in pool],
        )

    extra_walk = best.spot.walk_distance_m - predicted.spot.walk_distance_m
    if outcomes[best_id].score < current.score + SWITCH_MARGIN:
        return Decision(
            chosen=predicted, switched=False,
            reason="an alternative scored slightly better, not enough to move the car",
            outcomes=outcomes, considered=[c.spot.spot_id for c in pool],
        )
    if extra_walk > MAX_EXTRA_WALK_M:
        return Decision(
            chosen=predicted, switched=False,
            reason=(
                f"a better spot is {extra_walk:.0f} m farther to walk, "
                "which is not worth it"
            ),
            outcomes=outcomes, considered=[c.spot.spot_id for c in pool],
        )

    return Decision(
        chosen=best, switched=True,
        reason=outcomes[best_id].reason or "the camera found better cover",
        outcomes=outcomes, considered=[c.spot.spot_id for c in pool],
    )
