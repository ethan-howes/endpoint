"""Car simulation: walk a position along the route polyline.

ENDPOINT.md section 7 asks for a background task that advances the car every
second with a speed-up factor so a 4-minute approach takes ~50s of demo. This is
that, and it is deliberately the dumbest possible implementation: interpolate
along the polyline by arc length, convert back to lat/lng, and stop.

Two things it deliberately does NOT do:

  * **It does not re-plan.** If fusion switches the pickup spot mid-approach,
    ``flow.py`` re-routes and hands the simulator a new ``Route``; the simulator
    just follows whatever it currently holds. Mixing route planning into the
    thing that ticks once a second is how the two get tangled.

  * **It does not model traffic, or stops, or anything.** A car that obeys the
    speed limit perfectly is a car you can watch arrive on schedule, which is
    the entire point for a demo.

Interpolation is by *distance*, not by fraction of points, because a route's
vertices are unevenly spaced -- a naive "move 5% of the points per tick"
accelerates and decelerates for no visible reason and makes the ETA visibly wrong.
"""

from __future__ import annotations

import asyncio
import logging

from shared.config import SETTINGS
from shared.geo import LocalFrame
from shared.models import LatLng

from .ride import Ride, Route

log = logging.getLogger("endpoint.orchestrator.simulator")


def point_at_distance(route: Route, distance_m: float, frame: LocalFrame) -> tuple[float, float]:
    """Local-metre position ``distance_m`` along ``route``.

    Clamps at both ends. Clamping is not a shortcut -- the car is allowed to be
    asked for a position past the end of its route (a re-route that shortened the
    path, a demo skip), and returning the last vertex is correct rather than
    raising.
    """
    if route.is_empty or len(route.points) < 2:
        return (0.0, 0.0)

    if distance_m <= 0:
        return route.points[0]
    if distance_m >= route.cum[-1]:
        return route.points[-1]

    # Linear scan. A city route is tens to low hundreds of vertices and this runs
    # once a second, so a binary search would be more code for no measurable gain.
    for i in range(1, len(route.cum)):
        if route.cum[i] >= distance_m:
            span = route.cum[i] - route.cum[i - 1]
            frac = 0.0 if span <= 0 else (distance_m - route.cum[i - 1]) / span
            ax, ay = route.points[i - 1]
            bx, by = route.points[i]
            return ax + (bx - ax) * frac, ay + (by - ay) * frac
    return route.points[-1]


def _latlng(route: Route, distance_m: float, frame: LocalFrame) -> LatLng:
    x, y = point_at_distance(route, distance_m, frame)
    lat, lng = frame.to_ll(x, y)
    return LatLng(lat=round(lat, 6), lng=round(lng, 6))


async def run(ride: Ride, on_approach) -> None:
    """Advance ``ride``'s car until it arrives, then call ``on_approach``.

    The speed-up is applied to *simulated* time: each tick advances the car by
    ``speedup * tick`` metres of route, so a 50s ETA covers 250 simulated
    seconds of driving in 50s of wall clock.

    Cancellation is normal, not exceptional -- every ride teardown cancels this
    task -- so ``CancelledError`` is re-raised rather than swallowed.
    """
    frame = LocalFrame(ride.rider_location.lat, ride.rider_location.lng)
    tick = max(0.05, SETTINGS.sim_tick_s)

    try:
        while True:
            while ride.car_travelled_m < ride.route.distance_m:
                # Re-read every tick: a reroute (vision switch, declined detour)
                # replaces ``ride.route`` mid-run and resets the car's progress,
                # and a speed derived once from the first route would drive the
                # new one at the wrong pace.
                per_tick = _per_tick(ride.route, tick)
                if per_tick <= 0:
                    break
                ride.car_travelled_m = min(
                    ride.route.distance_m, ride.car_travelled_m + per_tick
                )
                ride.car_position = _latlng(ride.route, ride.car_travelled_m, frame)

                if (
                    ride.remaining_m() <= SETTINGS.approach_distance_m
                    or ride.remaining_eta_s() <= SETTINGS.approach_eta_s
                ):
                    await on_approach()

                await asyncio.sleep(tick)

            # Arrived, or there is no usable route. ``on_approach`` is idempotent,
            # so this only acts when the car never crossed the approach threshold
            # (no route, or one shorter than a tick). If it reroutes, keep driving.
            await on_approach()
            if (
                ride.car_travelled_m >= ride.route.distance_m
                or _per_tick(ride.route, tick) <= 0
            ):
                break

        if ride.resolved_spot is not None:
            ride.car_position = ride.resolved_spot.spot.stop_point
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        # A simulator crash must not take the ride down with it; the rider keeps
        # whatever position we last wrote and the plan is still valid.
        log.exception("simulator for ride %s stopped early", ride.ride_id)


def _per_tick(route: Route, tick_s: float) -> float:
    """Metres of route per tick, at the route's own implied speed times the
    demo speed-up, so the car finishes on the ETA the rider was shown."""
    if route.eta_s <= 0 or route.distance_m <= 0:
        return 0.0
    return route.distance_m / route.eta_s * max(1.0, SETTINGS.sim_speedup) * tick_s


def start(ride: Ride, on_approach) -> asyncio.Task:
    """Launch the simulation as a background task."""
    ride.sim_task = asyncio.create_task(run(ride, on_approach))
    return ride.sim_task


def skip_to_approach(ride: Ride) -> float:
    """Jump the car to just inside the approach threshold. Returns metres remaining.

    The demo shortcut from §7 (``/rides/{id}/skip_to_arrival``). Deterministic on
    purpose: "put the car 150 m out" always means exactly that, so rehearsing the
    vision step twice gives the same demo twice.
    """
    if ride.route.is_empty or ride.route.distance_m <= 0:
        ride.car_travelled_m = 0.0
    else:
        target = SETTINGS.approach_distance_m * 0.9
        ride.car_travelled_m = min(
            ride.route.distance_m, max(0.0, ride.route.distance_m - target)
        )
    frame = LocalFrame(ride.rider_location.lat, ride.rider_location.lng)
    ride.car_position = _latlng(ride.route, ride.car_travelled_m, frame)
    return ride.remaining_m()
