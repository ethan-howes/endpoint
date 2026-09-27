"""Orchestrator. Port 8000. ENDPOINT.md section 7.

    POST /rides/request                  {rider_location, car_start?} -> {ride_id, spot_count, question}
    POST /rides/{ride_id}/answer         {mobility_needs, force_condition?, force_time?} -> RidePlan
    POST /rides/{ride_id}/confirm        {accept_detour} -> RidePlan
    GET  /rides/{ride_id}                current RidePlan + car_position + phase (polled at 1 Hz)
    POST /rides/{ride_id}/skip_to_arrival demo shortcut
    GET  /health  /ready

The route that carries the design decision is ``/rides/request``. §3 has it call
S1 and *then* ask the mobility question, but the question text does not depend on
S1's answer in any way -- it is a fixed yes/no. So S1 is started as a background
task and the question returned in the same round trip, with the result awaited in
``/answer``. The rider sees the prompt immediately instead of after a call
capped at three seconds, and the S1 work is not repeated.

``/health`` vs ``/ready`` is split for the same reason as in S1: a process with
no downstream services up is alive but cannot do anything, and finding that out
during a rehearsal is the worst possible moment. ``/ready`` reports each
dependency separately so the failing one is obvious.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from shared.config import DEFAULT_RADIUS_M, SETTINGS
from shared.models import (
    Condition,
    LatLng,
    RankedSpot,
    RidePhase,
    RidePlan,
    StrictModel,
    utcnow,
)

from . import flow, messages, routing
from .clients import call_service
from .ride import RIDES, Ride, new_ride_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("endpoint.orchestrator")


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class RideRequest(StrictModel):
    """§7: ``{ rider_location, car_start? }``.

    Strict, so a typo'd field is a 422 rather than a silently defaulted ride.
    """

    rider_location: LatLng
    car_start: LatLng | None = None


class AnswerRequest(StrictModel):
    """§7: ``{ mobility_needs, force_condition?, force_time? }``.

    The two force fields are the §6 demo overrides. They are echoed back by S2 on
    the weather report (``overridden: true``) rather than applied here, so a
    recorded run can always be told apart from a real forecast.
    """

    mobility_needs: bool
    force_condition: Condition | None = None
    force_time: str | None = None


class ConfirmRequest(StrictModel):
    accept_detour: bool


class RideStatus(BaseModel):
    """``GET /rides/{id}`` -- the plan plus live car state.

    The UI polls this once a second, so it is a flat, small, cheap object: no
    nested candidate dumps beyond what the plan already carries, and car state
    included inline because the UI would otherwise need a second request per tick.
    """

    plan: RidePlan
    car_position: LatLng
    phase: RidePhase
    remaining_m: float
    remaining_eta_s: int
    #: The section 7 detour question, when S2 asked for one and the rider has
    #: not answered yet. Carried here rather than inside ``RidePlan`` because it
    #: is a question about the ride's *current state*, not part of the section 5
    #: plan document -- and because the UI only polls this endpoint, so anything
    #: it needs in order to render a prompt has to be in this response.
    confirmation_question: str | None = None
    #: Non-null when the ride is degraded; the UI shows it as a banner rather
    #: than reading it aloud at the rider.
    degraded_note: str | None = None


class RequestAccepted(BaseModel):
    """``POST /rides/request`` -- deliberately not a RidePlan.

    At this point the ride has a location, an id, and an in-flight S1 call. It has
    no spot, no ETA and no message yet. Returning a half-built plan here would
    imply a prediction that does not exist.
    """

    ride_id: str
    spot_count: int
    question: str


MOBILITY_QUESTION = (
    "Do you need extra time or assistance to get to a pickup point? "
    "We'll prioritise sheltered and shaded spots if so."
)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "orchestrator starting: demo rider %s, S1=%s S2=%s S3=%s",
        SETTINGS.demo_rider, SETTINGS.s1_url, SETTINGS.s2_url, SETTINGS.s3_url,
    )
    yield
    # Rides are in memory (see orchestrator/ride.py), so shutdown drops them and
    # cancels any simulator still running.
    for ride in RIDES.all():
        if ride.sim_task and not ride.sim_task.done():
            ride.sim_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ride.sim_task
    RIDES.clear()


app = FastAPI(
    title="Endpoint Orchestrator",
    version="1.0.0",
    description="Ride flow: legal spots, conditions, prediction, confirmation (ENDPOINT.md section 7)",
    lifespan=lifespan,
)

# The frontend is served from a different port in development, so CORS is open.
# Worth stating plainly: this is fine for a local demo and would not be for a
# public deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_ride(ride_id: str) -> Ride:
    ride = RIDES.get(ride_id)
    if ride is None:
        # The most likely cause by far is an orchestrator restart, because ride
        # state is in memory. Say that rather than a bare 404.
        raise HTTPException(
            status_code=404,
            detail=(
                f"ride {ride_id} not found. Rides are held in memory and are lost "
                "when the orchestrator restarts."
            ),
        )
    return ride


def _to_plan(ride: Ride) -> RidePlan:
    return RidePlan(
        ride_id=ride.ride_id,
        phase=ride.phase,
        mobility_needs=ride.mobility_needs,
        weather=ride.weather,
        candidates=ride.candidates,
        predicted_spot=ride.predicted_spot,
        final_spot=ride.final_spot,
        route_polyline=ride.route.polyline or None,
        eta_s=ride.eta_s,
        rider_message=ride.rider_message,
        fallbacks_used=list(ride.fallbacks_used),
    )


def _to_status(ride: Ride) -> RideStatus:
    return RideStatus(
        plan=_to_plan(ride),
        car_position=ride.car_position,
        phase=ride.phase,
        remaining_m=round(ride.remaining_m(), 1),
        remaining_eta_s=ride.remaining_eta_s(),
        confirmation_question=(
            messages.confirmation_question(ride) if ride.pending_confirmation else None
        ),
        degraded_note=messages.degraded_note(ride),
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict:
    """Per-dependency readiness.

    Each service gets a real call with a short budget rather than a TCP check: a
    port that is open but wedged is the failure that actually happens, and
    ``/health`` cannot tell the difference.
    """
    checks: dict[str, str] = {}

    s1 = await call_service("S1", "GET", "/health")
    checks["S1_legal_spots"] = "ok" if s1.ok else s1.reason

    s2 = await call_service("S2", "GET", "/health")
    # S2 missing is degraded, not fatal: the ride still works, just without
    # condition-aware ranking.
    checks["S2_weather_cover"] = "ok" if s2.ok else f"{s2.reason} (optional)"

    s3 = await call_service("S3", "GET", "/health")
    checks["S3_vision"] = "ok" if s3.ok else f"{s3.reason} (out of scope; predictions stand)"

    checks["routing"] = "ok" if routing._MEMO else "not exercised yet"
    checks["rides_in_memory"] = str(len(RIDES.all()))

    return {
        "status": "ready" if s1.ok else "no_data",
        "demo_rider": list(SETTINGS.demo_rider),
        "checks": checks,
    }


@app.post("/rides/request", response_model=RequestAccepted)
async def rides_request(req: RideRequest) -> RequestAccepted:
    """Start a ride. Returns the mobility question immediately.

    S1 is started here as a background task and awaited in ``/answer``; see the
    module docstring for why.
    """
    ride = Ride(
        ride_id=new_ride_id(),
        rider_location=req.rider_location,
        car_start=req.car_start or LatLng(lat=SETTINGS.demo_car_start[0], lng=SETTINGS.demo_car_start[1]),
        car_position=req.car_start or LatLng(lat=SETTINGS.demo_car_start[0], lng=SETTINGS.demo_car_start[1]),
    )
    RIDES.put(ride)

    # Fire and forget. The task is stored on the ride so /answer can await it
    # and so a ride that is abandoned mid-flight can cancel it.
    ride.spots_task = asyncio.create_task(flow.fetch_spots(ride, DEFAULT_RADIUS_M))

    return RequestAccepted(
        ride_id=ride.ride_id,
        spot_count=0,  # unknown yet; /answer carries the real count
        question=MOBILITY_QUESTION,
    )


@app.post("/rides/{ride_id}/answer", response_model=RidePlan)
async def rides_answer(ride_id: str, req: AnswerRequest) -> RidePlan:
    """Answer the mobility question, then run the predictive phase."""
    ride = _get_ride(ride_id)

    force_time = None
    if req.force_time:
        from datetime import datetime

        try:
            force_time = datetime.fromisoformat(req.force_time)
        except ValueError:
            raise HTTPException(status_code=422, detail="force_time must be ISO 8601")

    await flow.plan_ride(
        ride,
        mobility_needs=req.mobility_needs,
        force_condition=req.force_condition,
        force_time=force_time,
    )

    # Only a ride that is still predicted needs a car en route. One that is
    # already confirmed (no mobility needs) is done.
    if ride.phase == RidePhase.PREDICTED:
        flow.start_simulation(ride)

    return _to_plan(ride)


@app.post("/rides/{ride_id}/confirm", response_model=RidePlan)
async def rides_confirm(ride_id: str, req: ConfirmRequest) -> RidePlan:
    """Answer the detour question. Only reached when S2 asked for one."""
    ride = _get_ride(ride_id)
    if ride.sim_task and not ride.sim_task.done():
        ride.sim_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ride.sim_task
    await flow.confirm(ride, req.accept_detour)
    return _to_plan(ride)


@app.get("/rides/{ride_id}", response_model=RideStatus)
async def rides_get(ride_id: str) -> RideStatus:
    """Current plan and car state. The UI polls this at 1 Hz."""
    return _to_status(_get_ride(ride_id))


@app.post("/rides/{ride_id}/skip_to_arrival", response_model=RideStatus)
async def rides_skip(ride_id: str) -> RideStatus:
    """Demo shortcut: jump the car to the approach threshold and run S3 now.

    Runs the real-time phase inline rather than waiting for the simulator's next
    tick, so the response the caller gets back already reflects the confirmed
    spot -- which is what makes it usable as a single-call demo trigger.
    """
    from . import simulator

    ride = _get_ride(ride_id)
    if ride.sim_task and not ride.sim_task.done():
        ride.sim_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ride.sim_task
        ride.sim_task = None

    simulator.skip_to_approach(ride)
    await flow.on_approach(ride)
    return _to_status(ride)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
