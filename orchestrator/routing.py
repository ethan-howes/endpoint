"""Routing adapter: one function, two interchangeable backends.

ENDPOINT.md section 7 specifies exactly this shape -- ``route(origin, destination)
-> {polyline, distance_m, eta_s}`` over either the OSRM public demo server (no
key) or Google Routes -- and the doc is right about the two details that actually
bite:

  * the OSRM URL takes ``lng,lat``, not ``lat,lng``
  * the demo server is shared and must be cached

The second one matters more than it looks. The UI polls every second, the car
simulator re-routes when fusion switches spots, and a retry-on-failure loop on a
shared public server is how you get your IP throttled mid-demo. So: an in-process
memo keyed by the rounded origin/destination pair, and routes that survive a
service restart because a fresh orchestrator asking OSRM again is the one moment
we most want to avoid.

This module does no service-call policy; ``clients.py`` owns timeouts and
fallbacks. Routing is not a service, so it has its own small, honest error
handling: a failed route yields an empty ``Route``, and the flow proceeds with
straight-line geometry. A ride with a slightly wrong ETA beats a ride with no
car.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import polyline

from shared.config import SETTINGS
from shared.geo import LocalFrame, haversine_m
from shared.models import LatLng

from .ride import Route

log = logging.getLogger("endpoint.orchestrator.routing")

#: Coordinate rounding for the cache key. ~1 m at the equator: close enough that
#: a jittering rider reuses the same route, far enough apart that two genuinely
#: different pickups do not collide.
_KEY_PRECISION = 5

_MEMO: dict[tuple[Any, ...], tuple[float, Route]] = {}


class RoutingUnavailable(RuntimeError):
    """Routing could not be completed. Always resolved into a straight line."""


def _key(origin: LatLng, dest: LatLng) -> tuple[Any, ...]:
    return (
        round(origin.lat, _KEY_PRECISION), round(origin.lng, _KEY_PRECISION),
        round(dest.lat, _KEY_PRECISION), round(dest.lng, _KEY_PRECISION),
    )


def straight_line_route(origin: LatLng, dest: LatLng) -> Route:
    """The fallback route: a two-point straight line.

    Used when routing is unavailable. It is a real path the simulator can walk
    and a real ETA to show, which is what keeps §4.5's "no service failure
    should break a ride" true for the one dependency that is an external service
    rather than a sibling process.
    """
    frame = LocalFrame((origin.lat + dest.lat) / 2.0, (origin.lng + dest.lng) / 2.0)
    o = frame.to_m(origin.lat, origin.lng)
    d = frame.to_m(dest.lat, dest.lng)
    dist = max(0.0, (d[0] - o[0]) ** 2 + (d[1] - o[1]) ** 2) ** 0.5
    # ~30 km/h urban average. Rough on purpose: it is a fallback.
    eta = int(dist / 8.33) if dist else 0
    return Route(
        polyline=polyline.encode([(o[0], o[1]), (d[0], d[1])], precision=5),
        distance_m=dist,
        eta_s=eta,
        points=[o, d],
        cum=[0.0, dist],
    )


def _decode(points: list[tuple[float, float]], frame: LocalFrame) -> tuple[list, list]:
    """Local-metre coordinates plus cumulative arc length, for the simulator."""
    cum = [0.0]
    for i in range(1, len(points)):
        dx = points[i][0] - points[i - 1][0]
        dy = points[i][1] - points[i - 1][1]
        cum.append(cum[-1] + (dx * dx + dy * dy) ** 0.5)
    return points, cum


async def route_osrm(
    origin: LatLng, dest: LatLng, client: httpx.AsyncClient | None = None
) -> Route:
    """One attempt at the OSRM public demo server. May raise.

    Deliberately *not* defensive. The fallback lives one level up in ``route``,
    which is the seam ``flow.py`` actually calls: putting the try/except here
    meant the guarantee was a property of this function rather than of the module,
    so anything raised outside the narrow ``except`` tuple below -- a malformed
    body, a ``TypeError`` from a new response field, anything at all -- escaped
    into the ride endpoint and turned a slow route into a failed ride. §4.5 says
    no external dependency may do that, and the cheapest way to make that true is
    for the public function to own it.
    """
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=5.0)
    try:
        # lng,lat order -- noted in ENDPOINT.md section 7, and the single easiest
        # thing to get wrong here.
        url = (
            f"{SETTINGS.osrm_url}/{origin.lng},{origin.lat};{dest.lng},{dest.lat}"
            "?overview=full&geometries=polyline"
        )
        resp = await client.get(url)
        if resp.status_code != 200:
            raise RoutingUnavailable(f"OSRM HTTP {resp.status_code}")

        body = resp.json()
        routes = body.get("routes") or []
        if not routes:
            raise RoutingUnavailable("OSRM returned no routes")

        r = routes[0]
        encoded = r.get("geometry") or ""
        raw = polyline.decode(encoded, precision=5) if encoded else []
        if len(raw) < 2:
            raise RoutingUnavailable("OSRM geometry was not decodable")

        frame = LocalFrame(dest.lat, dest.lng)
        points = [frame.to_m(lat, lng) for lat, lng in raw]
        points, cum = _decode(points, frame)
        return Route(
            polyline=encoded,
            distance_m=float(r.get("distance") or cum[-1]),
            eta_s=int(float(r.get("duration") or 0)),
            points=points,
            cum=cum,
        )
    finally:
        if owns:
            await client.aclose()


async def route(origin: LatLng, dest: LatLng) -> Route:
    """The adapter: cached, and never raises. §4.5, for the one dependency that
    is an external service rather than a sibling process.

    Returns a straight line rather than failing. It is a real path the simulator
    can walk and a real ETA to show, and it is visibly different from a routed
    path (two vertices), so a degraded ETA is not something that can pass for a
    good one without someone looking.
    """
    k = _key(origin, dest)
    hit = _MEMO.get(k)
    if hit and (time.time() - hit[0]) < SETTINGS.route_cache_ttl_s:
        return hit[1]

    try:
        r = await route_osrm(origin, dest)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("routing failed (%s: %s); using a straight line",
                    type(exc).__name__, exc)
        r = straight_line_route(origin, dest)

    _MEMO[k] = (time.time(), r)
    return r


def clear_route_cache() -> None:
    _MEMO.clear()


def route_info() -> dict[str, Any]:
    """For /ready and the README: is routing actually working, or are we on the
    straight-line fallback? Worth surfacing, because a fallback route looks
    plausible on screen and nobody would otherwise notice."""
    return {
        "backend": "osrm",
        "endpoint": SETTINGS.osrm_url,
        "cached_routes": len(_MEMO),
    }
