"""In-memory ride state.

ENDPOINT.md section 7 needs state to survive between ``/rides/request`` and
``GET /rides/{id}``, which the UI polls every second, and it needs a
car-simulation task alive per ride. A dict is the whole implementation: for a
single-process hackathon demo there is nothing to gain from a database, and the
one thing a database would buy -- surviving a restart -- is not a property
anything is asked to demonstrate.

The consequence is stated plainly rather than hidden: restarting the
orchestrator loses every ride, and a rider polling a ride from before the restart
gets a 404 with a message saying so. That is honest, and it is a smaller problem
than a migration path nobody needs.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field

from shared.models import (
    ConditionsResult,
    LatLng,
    RankedSpot,
    RidePhase,
    WeatherReport,
)

_RIDE_SEQ = itertools.count(1)


def new_ride_id() -> str:
    """Monotonic and human-readable: ``r_000007``.

    A timestamp would sort rides by age, but a sequence counter means a recorded
    demo transcript reads in the order things actually happened, and two requests
    in the same second cannot collide.
    """
    return f"r_{next(_RIDE_SEQ):06d}"


@dataclass
class Route:
    """A planned car path. Distances in metres, times in real seconds."""

    polyline: str = ""
    distance_m: float = 0.0
    eta_s: int = 0
    #: Decoded points, in local metres, for the simulator to walk along.
    points: list[tuple[float, float]] = field(default_factory=list)
    #: Arc length at each point, for O(1) interpolation by distance.
    cum: list[float] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.points


@dataclass
class Ride:
    """Everything one ride needs, from request to confirmed spot.

    Deliberately a plain dataclass and not the ``RidePlan`` response model: the
    plan is what the rider sees, this is what the process holds. Conflating them
    would mean every piece of internal state (the pending S1 task, the simulator
    handle, the candidates considered and rejected) leaked into the API response.
    """

    ride_id: str
    rider_location: LatLng
    car_start: LatLng
    created_at: float = field(default_factory=time.time)

    # --- filled in by /rides/{id}/answer ---
    mobility_needs: bool = False
    phase: RidePhase = RidePhase.PREDICTED
    pending_confirmation: bool = False

    spots: list = field(default_factory=list)          # S1 Spot objects
    candidates: list[RankedSpot] = field(default_factory=list)
    predicted_spot: RankedSpot | None = None
    final_spot: RankedSpot | None = None
    weather: WeatherReport | None = None
    conditions: ConditionsResult | None = None

    route: Route = field(default_factory=Route)
    #: Seconds until arrival at the current destination. Kept on the ride (not
    #: derived from the route) so a re-route can change it without every reader
    #: having to recompute it, and so the number the rider was shown does not
    #: change under them.
    eta_s: int = 0
    car_position: LatLng = field(default_factory=lambda: LatLng(lat=0.0, lng=0.0))
    car_travelled_m: float = 0.0

    rider_message: str = ""
    fallbacks_used: list[str] = field(default_factory=list)

    #: Vision assessments keyed by spot_id, kept for the fusion step and for
    #: explaining a switch after the fact.
    vision: dict[str, object] = field(default_factory=dict)
    #: The camera's finding, empty when vision did not run (S3 absent or no
    #: usable assessment). The message builder reads this and ``vision_switched``
    #: rather than parsing the reason text.
    vision_reason: str = ""
    vision_switched: bool = False

    # --- machinery ---
    #: S1 request started at /rides/request so the rider sees the mobility
    #: question without waiting for it. Awaited in /answer.
    spots_task: asyncio.Task | None = None
    spots_error: str = ""
    sim_task: asyncio.Task | None = None
    #: Set once the real-time phase has run, so it cannot run twice.
    approach_done: bool = False

    @property
    def resolved_spot(self) -> RankedSpot | None:
        return self.final_spot or self.predicted_spot

    def remaining_m(self) -> float:
        return max(0.0, self.route.distance_m - self.car_travelled_m)

    def remaining_eta_s(self) -> int:
        if self.route.eta_s <= 0:
            return 0
        left = self.remaining_m() / self.route.distance_m
        return max(0, int(self.route.eta_s * left))


class RideStore:
    """A dict with a cap, so a long demo cannot grow without bound."""

    def __init__(self, capacity: int = 200) -> None:
        self._rides: dict[str, Ride] = {}
        self._capacity = capacity

    def put(self, ride: Ride) -> Ride:
        self._rides[ride.ride_id] = ride
        if len(self._rides) > self._capacity:
            oldest = min(self._rides.values(), key=lambda r: r.created_at)
            self._rides.pop(oldest.ride_id, None)
        return ride

    def get(self, ride_id: str) -> Ride | None:
        return self._rides.get(ride_id)

    def all(self) -> list[Ride]:
        return sorted(self._rides.values(), key=lambda r: r.created_at)

    def clear(self) -> None:
        self._rides.clear()


#: One process, one store. Set in ``main``'s lifespan so tests can swap it.
RIDES = RideStore()
