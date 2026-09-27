"""Service layer: the cache-first hot path.

ENDPOINT.md section 4.5 gives the orchestrator a 3-second timeout for S1, while a
cold Overpass query on a 1 km2 bbox routinely takes several seconds and the
public instance returns 504 under load often enough that it happened on the first
request of a real session and repeatedly while capturing the demo data. Those two
numbers are simply incompatible, so the request path never performs an unbounded
cold fetch:

1. Serve from the cached network document. Milliseconds.
2. On a cache miss, spend at most ``cold_fetch_budget_s`` trying to populate it.
3. On failure, degrade to ENDPOINT.md section 6 S1's stated fallback -- a single
   unverified spot at the rider's location -- rather than raising an error,
   because "no service failure should break a ride" (section 4.5).

A background task refreshes stale data without blocking the response, so a warm
service never waits on the network.

The cache unit is a containment *tile* (see ``shared/geo.tile_bbox``), not the
rider's exact position. That is what lets ``scripts/prefetch_demo_area.py``
predict the exact key this module will look for; see ``osm_cache`` for why the
cache key and the query bbox are deliberately different boxes.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field

from shared.config import DEFAULT_RADIUS_M, MAX_RADIUS_M, SETTINGS
from shared.fixtures import fixtures_dir, read_fixture
from shared.geo import LocalFrame, expanded_query_bbox, tile_bbox, tile_cache_id
from shared.models import (
    BBox,
    Confidence,
    LatLng,
    LegalSpotsRequest,
    LegalSpotsResponse,
    RejectedCandidate,
    Spot,
    SpotsExplainResponse,
)
from shared.osm_cache import OverpassError, cache_info, read_cache

from .curb import generate_candidates
from .legality import Verdict, explain_restriction_counts, judge_candidate
from .lots import lot_stops
from .network import StreetNetwork
from .overpass import (
    OsmRegulationSource,
    build_point_query,
    build_road_query,
    parse_accessible_spaces,
    parse_kerbs,
    parse_lots,
    parse_restrictions,
    parse_roads,
)
from .ranking import ScoredCandidate, is_across_street, rank, walk_distance

#: Process-wide memo so a burst of requests for nearby riders parses the OSM
#: payload once. Keyed by the road document's cache id, so a tile change cannot
#: return the wrong document.
_NETWORK_CACHE: dict[str, StreetNetwork] = {}
_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


@dataclass
class _Fetch:
    network: StreetNetwork | None
    cached: bool
    cache_age_s: float | None
    fallbacks: list[str] = field(default_factory=list)


def clamp_radius(radius_m: float | None) -> float:
    """Clamp a caller-supplied radius into the supported range.

    One bad request must not be able to ask the service to scan the county, and a
    missing radius must not mean "unbounded".

    Note the explicit ``is None`` check: ``radius_m or DEFAULT_RADIUS_M`` would
    silently turn a requested ``0`` into 150 m, because 0 is falsy.
    """
    if radius_m is None:
        return DEFAULT_RADIUS_M
    return min(max(radius_m, 1.0), MAX_RADIUS_M)


def _tile_and_ids(
    rider: LatLng, radius: float
) -> tuple[tuple[float, float, float, float], str, str]:
    tile = tile_bbox(rider.lat, rider.lng, radius)
    center = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
    return (
        tile,
        tile_cache_id(center[0], center[1], radius, "roads"),
        tile_cache_id(center[0], center[1], radius, "points"),
    )


def parse_network(
    tile: tuple[float, float, float, float],
    radius_m: float,
    road_payload: dict,
    point_payload: dict | None,
    cache_id: str,
) -> StreetNetwork:
    """Build a ``StreetNetwork`` from raw Overpass payloads. No I/O."""
    query_bbox = expanded_query_bbox(tile, radius_m)
    frame = LocalFrame(
        (query_bbox[0] + query_bbox[2]) / 2.0, (query_bbox[1] + query_bbox[3]) / 2.0
    )
    roads = parse_roads(road_payload, frame)
    point_payload = point_payload or {"elements": []}
    return StreetNetwork(
        frame=frame,
        bbox=tile,
        roads=roads,
        restrictions=parse_restrictions(point_payload, frame, roads),
        lots=parse_lots(point_payload, frame),
        kerbs=parse_kerbs(point_payload, frame),
        accessible_spaces=parse_accessible_spaces(point_payload, frame),
        source="osm",
        generated_at=time.time(),
        endpoint="cache",
        cache_id=cache_id,
    )


async def _build_network(
    tile: tuple[float, float, float, float], radius: float
) -> _Fetch:
    """Return a ``StreetNetwork`` for ``tile``, preferring any cached document."""
    fallbacks: list[str] = []
    _, roads_id, points_id = _tile_and_ids(
        LatLng(lat=(tile[0] + tile[2]) / 2.0, lng=(tile[1] + tile[3]) / 2.0), radius
    )
    query_bbox = expanded_query_bbox(tile, radius)
    road_query = build_road_query(query_bbox)
    point_query = build_point_query(query_bbox)
    fixture_dir = fixtures_dir("s1_legal_spots")

    # --- 1. in-process memo (microseconds) ---
    memo = _NETWORK_CACHE.get(roads_id)
    if memo is not None:
        info = cache_info(road_query, roads_id)
        age = info.age_s if info and info.age_s != float("inf") else None
        return _Fetch(memo, True, age, fallbacks)

    # --- 2. committed fixture, when mocking ---
    # Placed before the disk cache because in MOCK mode the network is not an
    # option at all, and a stale working cache must not shadow the committed
    # bytes that were reviewed and versioned.
    if SETTINGS.mock:
        road_payload = read_fixture(fixture_dir, roads_id)
        if road_payload is not None:
            net = parse_network(
                tile, radius, road_payload, read_fixture(fixture_dir, points_id), roads_id
            )
            _NETWORK_CACHE[roads_id] = net
            return _Fetch(net, True, None, fallbacks)
        fallbacks.append("no committed fixture for this tile")
        return _Fetch(None, True, None, fallbacks)

    # --- 3. disk cache (milliseconds) ---
    cached_roads = read_cache(road_query, roads_id)
    if cached_roads is not None:
        net = parse_network(
            tile,
            radius,
            cached_roads,
            read_cache(point_query, points_id),
            roads_id,
        )
        _NETWORK_CACHE[roads_id] = net
        info = cache_info(road_query, roads_id)
        return _Fetch(
            net,
            True,
            info.age_s if info and info.age_s != float("inf") else None,
            fallbacks,
        )

    # --- 4. bounded cold fetch ---
    source = OsmRegulationSource()
    try:
        net = await asyncio.wait_for(source.fetch(tile, radius), timeout=SETTINGS.cold_fetch_budget_s)
        _NETWORK_CACHE[roads_id] = net
        return _Fetch(net, False, 0.0, fallbacks)
    except (TimeoutError, asyncio.TimeoutError):
        fallbacks.append(f"network fetch timed out after {SETTINGS.cold_fetch_budget_s:g}s")
    except OverpassError:
        fallbacks.append("Overpass unavailable")
    except Exception as exc:  # a data problem must never break a ride
        fallbacks.append(f"network fetch error: {type(exc).__name__}")

    return _Fetch(None, False, None, fallbacks)


def schedule_refresh(tile: tuple[float, float, float, float], radius: float) -> None:
    """Kick off a background network refresh. Never blocks a response."""
    _, roads_id, _ = _tile_and_ids(
        LatLng(lat=(tile[0] + tile[2]) / 2.0, lng=(tile[1] + tile[3]) / 2.0), radius
    )
    lock = _REFRESH_LOCKS.setdefault(roads_id, asyncio.Lock())
    if lock.locked():
        return

    async def _run() -> None:
        async with lock:
            try:
                _NETWORK_CACHE[roads_id] = await OsmRegulationSource().fetch(tile, radius)
            except Exception:
                pass  # a failed refresh leaves the previous document in place

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass  # no running loop (sync script); nothing to schedule


def _degraded_response(req: LegalSpotsRequest, fallbacks: list[str]) -> LegalSpotsResponse:
    """ENDPOINT.md section 6 S1 fallback: one unverified spot at the rider's own
    location, clearly labelled. Better than an error, and honest about it."""
    spot = Spot(
        spot_id="s1_0001",
        stop_point=LatLng(lat=req.rider_location.lat, lng=req.rider_location.lng),
        street_name=None,
        curb_bearing_deg=0.0,
        walk_distance_m=0.0,
        source="fallback",
        confidence=Confidence.UNVERIFIED,
        notes=["No road data available; showing the rider's own location."],
    )
    return LegalSpotsResponse(
        spots=[spot],
        count=1,
        source="fallback",
        cached=False,
        total_candidates=1,
        truncated=False,
        fallbacks_used=fallbacks,
    )


def _score_all(network: StreetNetwork, rider: LatLng) -> list[ScoredCandidate]:
    """Generate, judge and score every candidate in the network."""
    point_r = network.point_restrictions()
    arc_r = network.arc_restrictions()
    rx, ry = network.frame.to_m(rider.lat, rider.lng)

    out: list[ScoredCandidate] = []
    for road in network.roads:
        for cand in generate_candidates(road, SETTINGS.traffic_side):
            verdict = judge_candidate(cand, point_r, arc_r, SETTINGS.traffic_side)
            if not verdict.accepted:
                continue
            straight = math.hypot(cand.x - rx, cand.y - ry)
            probe = ScoredCandidate(verdict, 0.0, straight, False)
            across = is_across_street(network.frame, (rx, ry), probe)
            out.append(
                ScoredCandidate(
                    verdict=verdict,
                    walk_distance_m=walk_distance(network.frame, rider, straight, across),
                    straight_distance_m=straight,
                    across_street=across,
                )
            )
    return out


async def legal_spots(req: LegalSpotsRequest) -> LegalSpotsResponse:
    """Main entry point for ``POST /spots/legal``."""
    radius = clamp_radius(req.radius_m)
    tile = tile_bbox(req.rider_location.lat, req.rider_location.lng, radius)

    fetch = await _build_network(tile, radius)
    if fetch.network is None or not fetch.network.roads:
        schedule_refresh(tile, radius)
        return _degraded_response(
            req, fetch.fallbacks + ["no road data for the requested area"]
        )

    # Keep the document warm for the next request without blocking this one.
    query_bbox = expanded_query_bbox(tile, radius)
    info = cache_info(build_road_query(query_bbox), fetch.network.cache_id)
    if info is not None and not info.fresh:
        schedule_refresh(tile, radius)

    scored = _score_all(fetch.network, req.rider_location)
    net = fetch.network
    spots, total, truncated = rank(
        net.frame, req.rider_location, scored, radius_m=radius, kerbs=net.kerbs,
        lot_stops=lot_stops(net, net.frame.to_m(req.rider_location.lat, req.rider_location.lng), radius),
        network=net,
    )

    return LegalSpotsResponse(
        spots=spots,
        count=len(spots),
        source=fetch.network.source,
        cached=fetch.cached,
        total_candidates=total,
        truncated=truncated,
        bbox=BBox(south=tile[0], west=tile[1], north=tile[2], east=tile[3]),
        cache_age_s=(
            round(fetch.cache_age_s, 1)
            if fetch.cache_age_s is not None and fetch.cache_age_s != float("inf")
            else None
        ),
        fallbacks_used=fetch.fallbacks,
    )


def _network_for_tile_sync(
    tile: tuple[float, float, float, float], radius: float
) -> StreetNetwork | None:
    """Read-only network lookup for the dev explain route. No network access."""
    _, roads_id, points_id = _tile_and_ids(
        LatLng(lat=(tile[0] + tile[2]) / 2.0, lng=(tile[1] + tile[3]) / 2.0), radius
    )
    memo = _NETWORK_CACHE.get(roads_id)
    if memo is not None:
        return memo
    query_bbox = expanded_query_bbox(tile, radius)
    road_query = build_road_query(query_bbox)
    point_query = build_point_query(query_bbox)

    if SETTINGS.mock:
        road_payload = read_fixture(fixtures_dir("s1_legal_spots"), roads_id)
        point_payload = read_fixture(fixtures_dir("s1_legal_spots"), points_id)
    else:
        road_payload = read_cache(road_query, roads_id)
        point_payload = read_cache(point_query, points_id)

    if road_payload is None:
        return None
    return parse_network(tile, radius, road_payload, point_payload, roads_id)


def explain(req: LegalSpotsRequest) -> SpotsExplainResponse:
    """Dev-only diagnostic: accepted AND rejected candidates with reasons.

    This is the route that makes ENDPOINT.md section 6 S1's definition of done
    checkable ("none within the hydrant, crosswalk, bus stop, or bike-lane
    exclusions") and is how the buffers in ``shared/config.py`` get tuned against
    real data. Deliberately read-only: it never fetches, so a diagnostic call can
    never be the thing that hangs during a rehearsal.
    """
    radius = clamp_radius(req.radius_m)
    tile = tile_bbox(req.rider_location.lat, req.rider_location.lng, radius)
    net = _network_for_tile_sync(tile, radius)
    if net is None:
        return SpotsExplainResponse(
            rider_location=req.rider_location,
            radius_m=radius,
            error="no cached network for this tile; run scripts/prefetch_demo_area.py",
        )

    point_r = net.point_restrictions()
    arc_r = net.arc_restrictions()
    rx, ry = net.frame.to_m(req.rider_location.lat, req.rider_location.lng)

    accepted_scored: list[ScoredCandidate] = []
    rejected: list[RejectedCandidate] = []

    for road in net.roads:
        for cand in generate_candidates(road, SETTINGS.traffic_side):
            verdict: Verdict = judge_candidate(cand, point_r, arc_r, SETTINGS.traffic_side)
            lat, lng = net.frame.to_ll(cand.x, cand.y)
            if not verdict.accepted:
                rejected.append(
                    RejectedCandidate(
                        stop_point=LatLng(lat=round(lat, 6), lng=round(lng, 6)),
                        street_name=road.name or road.ref,
                        side=cand.side,  # type: ignore[arg-type]
                        walk_distance_m=round(math.hypot(cand.x - rx, cand.y - ry), 1),
                        reason=verdict.reason,
                        restriction_kind=verdict.restriction.kind if verdict.restriction else None,
                        restriction_source_id=(
                            verdict.restriction.source_id if verdict.restriction else None
                        ),
                        buffer_m=verdict.restriction.buffer_m if verdict.restriction else None,
                    )
                )
                continue
            straight = math.hypot(cand.x - rx, cand.y - ry)
            probe = ScoredCandidate(verdict, 0.0, straight, False)
            across = is_across_street(net.frame, (rx, ry), probe)
            accepted_scored.append(
                ScoredCandidate(
                    verdict=verdict,
                    walk_distance_m=walk_distance(
                        net.frame, req.rider_location, straight, across
                    ),
                    straight_distance_m=straight,
                    across_street=across,
                )
            )

    spots, _, _ = rank(
        net.frame, req.rider_location, accepted_scored, radius_m=radius, kerbs=net.kerbs,
        lot_stops=lot_stops(net, (rx, ry), radius), network=net,
    )
    rejected.sort(key=lambda r: (r.reason, r.walk_distance_m))

    return SpotsExplainResponse(
        rider_location=req.rider_location,
        radius_m=radius,
        accepted=spots,
        rejected=rejected[:400],
        restriction_counts=explain_restriction_counts(net),
        network_summary=net.summary(),
    )


__all__ = ["legal_spots", "explain", "schedule_refresh", "clamp_radius", "parse_network"]
