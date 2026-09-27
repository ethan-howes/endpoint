"""The two phases of a ride, and the §4.5 guarantee.

``plan_ride`` is the predictive phase: ask S1 for legal spots, ask S2 which one
suits the conditions at pickup time, route the car, explain. ``on_approach`` is
the real-time phase: check the prediction against the camera and commit.

The design decision that shapes this file is that **nothing in it raises**. §4.5
says no service failure should break a ride, and the cheapest way to honour that
is to make the failure path the normal shape of the code rather than an exception
handler bolted on afterwards. Every step has an answer when it produces nothing,
and the answer is always "the nearest legal spot, and a note saying why".

The pickup time is computed *before* S2 is called, and that ordering is load
bearing rather than incidental: S2 needs the weather at the moment the rider will
actually be standing on the kerb, not the moment they tapped the button. A car
four minutes away in a rainstorm is a different decision than one arriving in
dry weather, and using "now" would quietly get that wrong every time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from shared.config import DEFAULT_RADIUS_M, SETTINGS
from shared.models import (
    ConditionsResult,
    LatLng,
    LegalSpotsRequest,
    LegalSpotsResponse,
    RankRequest,
    RankedSpot,
    RidePhase,
    Spot,
    WalkRoutesRequest,
    WalkRoutesResponse,
    utcnow,
)
from shared.osm_cache import USER_AGENT  # noqa: F401  (keeps one UA for the project)

from . import messages, routing, simulator
from .clients import ServiceResult, call_service, merge_fallbacks
from .fusion import choose, pick_alternatives
from .ride import Ride

log = logging.getLogger("endpoint.orchestrator.flow")

#: How long the rider waits at the kerb. §7 uses 10 minutes.
DEFAULT_WAIT_MINUTES = 10


def as_ranked(spot: Spot, reason: str = "", score: float = 0.5) -> RankedSpot:
    """Wrap a plain S1 spot as a ranked candidate.

    The one helper §7's pseudocode calls ``as_ranked`` without defining. A spot
    straight from S1 has no condition-based score, so it gets a neutral one: high
    enough not to lose to a real ranking, low enough that a scored candidate
    always wins. Inventing a score here would let an unranked spot outrank a
    properly assessed one.
    """
    return RankedSpot(spot=spot, score=score, reason=reason, confidence=spot.confidence)


# --------------------------------------------------------------------------- #
# S1
# --------------------------------------------------------------------------- #

async def fetch_spots(ride: Ride, radius_m: float = DEFAULT_RADIUS_M) -> LegalSpotsResponse | None:
    """Call S1. Started at request time so the mobility question is not blocked."""
    result = await call_service(
        "S1", "POST", "/spots/legal",
        json_body=LegalSpotsRequest(
            rider_location=ride.rider_location, radius_m=radius_m
        ).model_dump(mode="json"),
        model=LegalSpotsResponse,
    )
    merge_fallbacks(ride.fallbacks_used, result)
    if not result.ok or not isinstance(result.data, LegalSpotsResponse):
        return None
    return result.data


def nearest_spot(spots: list[Spot]) -> Spot | None:
    """Closest legal spot. The §4.5 fallback target, and the answer whenever no
    better option exists.

    Ties broken by ``spot_id`` so two equidistant spots cannot make the answer
    depend on the order the API happened to return them in.
    """
    if not spots:
        return None
    return min(spots, key=lambda s: (s.walk_distance_m, s.spot_id))


# --------------------------------------------------------------------------- #
# S2
# --------------------------------------------------------------------------- #

async def fetch_conditions(
    ride: Ride, spots: list[Spot], pickup_time, force_condition=None, force_time=None
) -> ConditionsResult | None:
    result = await call_service(
        "S2", "POST", "/conditions/rank",
        json_body=RankRequest(
            rider_location=ride.rider_location,
            spots=spots,
            pickup_time=pickup_time,
            wait_minutes=DEFAULT_WAIT_MINUTES,
            force_condition=force_condition,
            force_time=force_time,
        ).model_dump(mode="json"),
        model=ConditionsResult,
    )
    merge_fallbacks(ride.fallbacks_used, result)
    if not result.ok or not isinstance(result.data, ConditionsResult):
        return None
    return result.data


async def _nearest_by_walk(ride: Ride, spots: list[Spot], fallback: Spot, pickup_time) -> RankedSpot:
    """The spot with the shortest real walk, with its route, or ``fallback``."""
    result = await call_service(
        "S2", "POST", "/walk/routes",
        json_body=WalkRoutesRequest(
            rider_location=ride.rider_location, spots=spots, pickup_time=pickup_time,
        ).model_dump(mode="json"),
        model=WalkRoutesResponse,
    )
    merge_fallbacks(ride.fallbacks_used, result)
    routes = result.data.routes if result.ok and isinstance(result.data, WalkRoutesResponse) else []
    by_id = {s.spot_id: s for s in spots}
    routes = [r for r in routes if r.spot_id in by_id]
    if not routes:
        return as_ranked(fallback, "Fastest pickup", score=1.0)
    best = min(routes, key=lambda r: (r.walk_m, r.spot_id))
    spot = by_id[best.spot_id].model_copy(update={"walk_distance_m": best.walk_m})
    return RankedSpot(
        spot=spot, score=1.0, reason="Fastest pickup", confidence=spot.confidence,
        wait_point=spot.stop_point, walk_polyline=best.walk_polyline,
        indoor_m=best.indoor_m, route_notes=list(best.route_notes),
    )


# --------------------------------------------------------------------------- #
# Predictive phase
# --------------------------------------------------------------------------- #

async def plan_ride(
    ride: Ride,
    mobility_needs: bool,
    force_condition=None,
    force_time=None,
) -> None:
    """Run the predictive phase and populate ``ride`` in place."""
    ride.mobility_needs = mobility_needs

    # S1 was kicked off at /rides/request so the rider saw the mobility question
    # without waiting for it. Await that task; if there is none (or it failed),
    # fetch now.
    resp: LegalSpotsResponse | None = None
    if ride.spots_task is not None:
        try:
            resp = await ride.spots_task
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            resp = None
    if resp is None:
        resp = await fetch_spots(ride)

    spots: list[Spot] = list(resp.spots) if resp else []
    ride.spots = spots
    nearest = nearest_spot(spots)

    if nearest is None:
        # Nothing at all. §6 S1's own degraded answer is a single unverified spot
        # at the rider's own location, and it is the right one: better a car
        # arriving where they are standing than no ride.
        ride.spots = [Spot(
            spot_id="s1_0001",
            stop_point=ride.rider_location,
            curb_bearing_deg=0.0,
            walk_distance_m=0.0,
            source="fallback",
        )]
        ride.predicted_spot = as_ranked(ride.spots[0], "no road data available")
        merge_fallbacks(ride.fallbacks_used, ServiceResult(
            ok=False, reason="no legal spots found near the rider"
        ))
        await _dispatch(ride)
        ride.rider_message = messages.build(ride)
        return

    # Route to the nearest first, so we have an ETA to give S2. S2 ranks for the
    # moment of arrival, and arrival is measured from here.
    route = await routing.route(ride.car_position, nearest.stop_point)
    ride.route = route
    ride.eta_s = route.eta_s
    pickup_time = utcnow() + timedelta(seconds=route.eta_s)

    if not mobility_needs:
        # §3 step 4: no mobility needs means the nearest spot, and no weather or
        # cover ranking -- the rider did not ask about either. "Nearest" is by
        # the walk they will actually make: S2's walking routes, not S1's
        # straight-line x 1.3, which ignores buildings and fences and let the UI
        # draw a public router's 280 m detour for a 134 m walk. If S2 cannot
        # route, S1's nearest stands.
        ride.predicted_spot = await _nearest_by_walk(ride, spots, nearest, pickup_time)
        if ride.predicted_spot.spot.spot_id != nearest.spot_id:
            await _dispatch(ride, ride.predicted_spot)
        ride.candidates = [ride.predicted_spot]
        ride.final_spot = ride.predicted_spot
        ride.phase = RidePhase.CONFIRMED
        ride.rider_message = messages.build(ride)
        return

    cond = await fetch_conditions(ride, spots, pickup_time, force_condition, force_time)
    if cond is not None:
        ride.weather = cond.weather
        ride.conditions = cond
        ride.pending_confirmation = cond.needs_rider_confirmation
        if cond.ranked:
            ride.candidates = cond.ranked
            best = cond.ranked[0]
        else:
            ride.candidates = [as_ranked(s) for s in spots]
            best = as_ranked(nearest, "No better option for current conditions")
            merge_fallbacks(ride.fallbacks_used, ServiceResult(
                ok=False, reason="S2 returned no ranked spots"
            ))
    else:
        ride.candidates = [as_ranked(s) for s in spots]
        # Any S2 failure lands here (timeout, outage, bad response), not only a
        # weather outage; the specific cause is already in `fallbacks_used`.
        best = as_ranked(nearest, "Conditions service unavailable; using the nearest spot")

    ride.predicted_spot = best
    await _dispatch(ride)
    ride.phase = RidePhase.PREDICTED
    ride.rider_message = messages.build(ride)


async def _dispatch(ride: Ride, target: RankedSpot | None = None) -> None:
    """Route the car from where it is now to ``target``.

    ``target`` defaults to the spot the ride is committed to (the final spot if
    there is one, else the prediction). Callers that change the destination pass
    the new spot explicitly: routing to ``predicted_spot`` unconditionally sent
    the car to the old spot after a vision switch or a declined detour.

    The new route starts at the car's current position, so progress along it
    starts at zero. Keeping the old ``car_travelled_m`` measured the new route
    with the old route's odometer, and the car "arrived" the moment it was
    rerouted.
    """
    spot = target or ride.resolved_spot
    if spot is None:
        return
    route = await routing.route(ride.car_position, spot.spot.stop_point)
    ride.route = route
    ride.eta_s = route.eta_s
    ride.car_travelled_m = 0.0


# --------------------------------------------------------------------------- #
# Real-time phase
# --------------------------------------------------------------------------- #

async def on_approach(ride: Ride) -> None:
    """Confirm or revise the pickup once the car is close.

    Runs exactly once per ride. Idempotence is not defensive padding: the
    simulator checks the approach threshold on every tick, so without the guard
    this would fire repeatedly for as long as the car sat within 150 m.
    """
    if ride.approach_done:
        return
    ride.approach_done = True

    predicted = ride.predicted_spot
    if predicted is None:
        ride.phase = RidePhase.CONFIRMED
        return

    neutral = ride.weather is None or ride.weather.condition.value == "neutral"

    # §3 step 7: the real-time phase is skipped entirely in neutral conditions --
    # there is nothing to protect against, so looking is wasted time and money.
    if not ride.mobility_needs or neutral:
        ride.final_spot = predicted
        ride.phase = RidePhase.CONFIRMED
        ride.rider_message = messages.build(ride)
        return

    alternatives = pick_alternatives(
        predicted, ride.candidates,
        radius_m=SETTINGS.alternative_radius_m,
        limit=SETTINGS.alternative_limit,
    )
    pool = [predicted, *alternatives]

    # S3 is out of scope for this build, so this call is expected to fail and
    # degrade. The seam is real: when S3 exists this is where its answer lands.
    assessments = await _assess(ride, pool)
    decision = choose(predicted, alternatives, assessments)

    ride.vision_reason = decision.reason
    ride.vision_switched = decision.switched
    ride.final_spot = decision.chosen
    if decision.switched:
        await _dispatch(ride, decision.chosen)
    ride.phase = RidePhase.CONFIRMED
    ride.rider_message = messages.build(ride)


async def _assess(ride: Ride, pool: list[RankedSpot]) -> dict:
    """Ask S3 to look at the predicted spot and its alternatives.

    Returns {} when S3 is absent, which makes fusion a no-op and the prediction
    stand -- the documented behaviour when the vision step cannot run.
    """
    from shared.models import VisionAssessment  # local: keeps S3 optional

    result = await call_service(
        "S3", "POST", "/vision/assess",
        json_body={
            "mode": ride.weather.condition.value if ride.weather else "neutral",
            "candidates": [
                {
                    "spot_id": c.spot.spot_id,
                    "stop_point": c.spot.stop_point.model_dump(mode="json"),
                    "curb_bearing_deg": c.spot.curb_bearing_deg,
                }
                for c in pool
            ],
        },
    )
    merge_fallbacks(ride.fallbacks_used, result)
    if not result.ok or not isinstance(result.data, dict):
        return {}

    out: dict = {}
    for item in result.data.get("assessments", []) or []:
        try:
            va = VisionAssessment.model_validate(item)
        except Exception:  # noqa: BLE001
            continue
        out[va.spot_id] = va
    return out


# --------------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------------- #

async def confirm(ride: Ride, accept_detour: bool) -> None:
    """Rider's answer to the detour question (ENDPOINT.md line 519).

    The semantics, which are easy to get backwards: S2's best pick is *always*
    the predicted spot -- if the covered spot were 200 m away and the rider
    declined the detour, we would never have dispatched the car there in the
    first place. The question is not "shall we move you to the cover" but
    "shall we keep you here, where it is closer to walk".

    So:

        accept_detour=True   -> keep the covered spot (the S2 prediction)
        accept_detour=False  -> switch to the nearest legal spot

    The first version of this had it exactly inverted, which produced the worst
    possible combination: a rider who said "yes, I'll walk the extra two minutes
    for the awning" was moved to the uncovered spot beside them.

    Answering is part of the *predictive* phase, not the end of the ride: the
    car keeps driving and the real-time phase (§3 steps 7-9) still runs on
    approach. The previous version marked the ride confirmed here, and the route
    handler stopped the simulator, so the car froze where it was and vision never
    looked at the spot the rider chose. The simulator is (re)started by the route
    handler after this returns.
    """
    ride.pending_confirmation = False
    predicted = ride.predicted_spot
    if predicted is None:
        ride.phase = RidePhase.CONFIRMED
        return

    if not accept_detour:
        near = nearest_spot(ride.spots)
        if near is not None and near.spot_id != predicted.spot.spot_id:
            # Prefer S2's own ranking of that spot, which carries its real cover
            # and wait point, over a bare wrapper.
            target = next(
                (c for c in ride.candidates if c.spot.spot_id == near.spot_id),
                None,
            ) or as_ranked(near, "Closest legal spot; you declined the detour")
            if ride.approach_done:
                # The car already reached the approach threshold while the
                # question was open, so the camera looked at the old spot. The
                # rider's choice still wins, and its finding no longer applies.
                ride.final_spot = target
                ride.vision_reason = ""
                ride.vision_switched = False
            else:
                ride.predicted_spot = target
            await _dispatch(ride, target)

    ride.phase = RidePhase.CONFIRMED if ride.approach_done else RidePhase.PREDICTED
    ride.rider_message = messages.build(ride)


def start_simulation(ride: Ride) -> None:
    """Kick off the car. Separated from ``plan_ride`` so a caller can plan and
    inspect before the car starts moving."""
    if ride.sim_task is None or ride.sim_task.done():
        simulator.start(ride, lambda: on_approach(ride))
