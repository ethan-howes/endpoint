"""The one call the orchestrator makes: conditions, then a ranking.

Sequencing and I/O. Every decision about what the conditions *mean* lives in
``weather.py`` (classification) and ``scoring.py`` (arithmetic); this module only
decides which path to take and how to fail.

One rule shapes the whole file: **S2 must never return an error to the
orchestrator for a data problem** (ENDPOINT.md section 6). A missing forecast, an
Overpass timeout, a shapely exception on one malformed footprint -- all of them
resolve here into a ranked-by-walk-distance answer plus an entry in
``fallbacks_used``. The alternative, propagating the exception, turns a grey
afternoon into no pickup at all for the one group of riders the product exists
for.

ENDPOINT.md's own flow does not satisfy this: its ``try``/``except`` wraps the
ranking but not ``weather.get``, so the single most likely failure -- a weather
API outage -- 500s the call. Everything below is inside the guard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import polyline

from shared.config import CURB_RAMP_MAX_DISTANCE_M, SETTINGS
from shared.fixtures import cache_key, fixtures_dir, read_fixture
from shared.geo import LocalFrame, _utm_epsg, expanded_query_bbox, frame_for, tile_bbox, tile_cache_id
from shared.models import (
    Condition,
    ConditionsResult,
    CurbAccess,
    LatLng,
    Overlays,
    RankedSpot,
    RankRequest,
    ShadeSource,
    Spot,
    WalkRoute,
    WalkRoutesRequest,
    WalkRoutesResponse,
)
from shared.osm_cache import OverpassError, read_cache

from . import rain_cover, rain_exposure, scoring, sun_shade, walk_network, weather
from .cover import (
    Cover,
    CoverMap,
    OsmCoverSource,
    ShadeBlock,
    ShadeMap,
    build_cover_query,
    build_shade_query,
    parse_covers,
    parse_shade,
)
from .paths import PathMap, PathWay, build_paths_query, fetch_paths, parse_paths

log = logging.getLogger("endpoint.s2")

#: In-process memo, keyed by tile cache id. Same idea as S1's: the disk cache
#: avoids the network, this avoids the disk.
_COVER_MEMO: dict[str, CoverMap] = {}
_SHADE_MEMO: dict[str, ShadeMap] = {}
_PATHS_MEMO: dict[str, PathMap] = {}
#: Built walking networks, keyed by the tiles they cover and the UTM zone. Building
#: one means splitting every edge against the dry geometry, which is the expensive
#: part of an exposure ranking; the tiles and their documents do not change between
#: requests, so neither does the network.
_NETWORK_MEMO: dict[tuple, rain_exposure.Network] = {}


# --------------------------------------------------------------------------- #
# Radius
# --------------------------------------------------------------------------- #

def search_radius_m(req: RankRequest) -> float:
    """The radius S2 fetches cover and shade for.

    A configured constant, and deliberately not derived from the spots S1
    returned. Deriving it looked like a strict improvement -- a rider whose
    nearest spots are all within 40 m "needs" cover data for 40 m -- and it was
    actively harmful, for two reasons that only show up in the cache.

    First, it bought nothing. ``tile_bbox`` picks its grid from ``level_for``,
    where 71 m and 150 m are both level 0 (0.004 deg), so the tile count was
    identical either way. The only thing the smaller radius changed was the
    margin in ``expanded_query_bbox`` -- a marginal byte saving.

    Second, it made the cache unpredictable. The disk cache is keyed by a hash of
    the query text, and the query text embeds that margin. So a response
    captured at 150 m was invisible to a request that derived 71.42 m: correct
    cache id, different query hash, a miss every time. The demo area was fully
    seeded and S2 still reported "cover fetch timed out" on all of it, because
    the capture radius could never be guessed from the code that reads it.

    The value is the same one ``DEFAULT_RADIUS_M`` and ``prefetch_demo_area``
    use, so one prefetch serves S1 and S2 on the same tile grid.
    """
    return SETTINGS.cover_query_radius_m


def _tiles_covering(req: RankRequest, radius: float) -> list[tuple[float, float, float, float]]:
    """Every tile that contains a spot, plus one around the rider.

    Per-tile rather than bbox-spanning: the spots arrive in walk-distance order
    around the rider, so their extent is small even when they reach 150 m, and a
    bbox from tile edge to tile edge would pull in up to four tiles' worth of
    document for a handful of points along one street.
    """
    lats = [s.stop_point.lat for s in req.spots] + [req.rider_location.lat]
    lngs = [s.stop_point.lng for s in req.spots] + [req.rider_location.lng]
    seen: list[tuple[float, float, float, float]] = []
    for lat, lng in zip(lats, lngs):
        t = tile_bbox(lat, lng, radius)
        if t not in seen:
            seen.append(t)
    return seen


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #

def _tile_label(tile: tuple[float, float, float, float]) -> str:
    """A tile's own SW corner, which is how ``tile_bbox`` names it.

    In the fallback strings, because the same fetch runs once per tile and
    without this every miss reports the identical line -- so a two-tile request
    that missed both is indistinguishable from one that missed the same tile
    twice, and the useful question ("is it one slow tile or the whole area?")
    has no answer. The SW corner is enough to identify a tile, and it is the
    string already in the cache id, so it is what an operator will grep for.
    """
    return f"{tile[0]:g},{tile[1]:g}"


async def _fetch_covers(
    tile: tuple[float, float, float, float],
    radius: float,
    fallbacks: list[str],
    frame: LocalFrame,
) -> CoverMap | None:
    cache_id = tile_cache_id(
        (tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0, radius, "cover"
    )
    memo = _COVER_MEMO.get(cache_id)
    if memo is not None and memo.frame.lat == frame.lat and memo.frame.lng == frame.lng:
        return memo

    query = build_cover_query(_expanded(tile, radius))
    fdir = fixtures_dir("s2_weather_cover")

    # Fixture before disk cache, for the same reason as S1: in MOCK mode the
    # network is not an option, and a stale working cache must not shadow
    # committed bytes.
    if SETTINGS.mock:
        # The raw cache id, exactly as S1 passes it. `shared.fixtures.fixture_name`
        # owns the filename convention; a service that pre-mangles the id applies
        # a *second*, different mangling on top and the two can never agree. That
        # bug was live here: this line used to pass ``f"cover__{_slug(cache_id)}"``,
        # which produced `cover__cover_25_752000_m80_380000_...json` while
        # `export_fixtures.py` wrote `cover__25.752000_m80.380000_...json`. Every
        # exported S2 fixture was unreadable, and the symptom was "no committed
        # fixture" for an area that was in fact fully seeded.
        payload = read_fixture(fdir, cache_id)
        if payload is None:
            fallbacks.append(
                f"no committed cover fixture for tile {_tile_label(tile)}"
            )
            return None
        cmap = parse_covers(tile, radius, payload, cache_id, frame)
        _COVER_MEMO[cache_id] = cmap
        return cmap

    cached = read_cache(query, cache_key("s2", "cover", cache_id))
    if cached is not None:
        cmap = parse_covers(tile, radius, cached, cache_id, frame)
        _COVER_MEMO[cache_id] = cmap
        return cmap

    try:
        cmap = await asyncio.wait_for(
            OsmCoverSource().fetch_covers(tile, radius, frame=frame),
            timeout=SETTINGS.cold_fetch_budget_s,
        )
    except (asyncio.TimeoutError, TimeoutError):
        fallbacks.append(
            f"cover fetch timed out after {SETTINGS.cold_fetch_budget_s:g}s "
            f"for tile {_tile_label(tile)}"
        )
        return None
    except OverpassError:
        fallbacks.append(f"Overpass unavailable for cover ({_tile_label(tile)})")
        return None
    except Exception as exc:  # noqa: BLE001
        fallbacks.append(
            f"cover fetch error for tile {_tile_label(tile)}: {type(exc).__name__}"
        )
        return None

    _COVER_MEMO[cache_id] = cmap
    return cmap


async def _fetch_shade(
    tile: tuple[float, float, float, float],
    radius: float,
    fallbacks: list[str],
    frame: LocalFrame,
) -> ShadeMap | None:
    cache_id = tile_cache_id(
        (tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0, radius, "shade"
    )
    memo = _SHADE_MEMO.get(cache_id)
    if memo is not None and memo.frame.lat == frame.lat and memo.frame.lng == frame.lng:
        return memo

    query = build_shade_query(_expanded(tile, radius))
    fdir = fixtures_dir("s2_weather_cover")

    if SETTINGS.mock:
        # Raw cache id -- see the matching note in `_fetch_covers`.
        payload = read_fixture(fdir, cache_id)
        if payload is None:
            fallbacks.append(
                f"no committed shade fixture for tile {_tile_label(tile)}"
            )
            return None
        smap = parse_shade(tile, radius, payload, cache_id, frame)
        _SHADE_MEMO[cache_id] = smap
        return smap

    cached = read_cache(query, cache_key("s2", "shade", cache_id))
    if cached is not None:
        smap = parse_shade(tile, radius, cached, cache_id, frame)
        _SHADE_MEMO[cache_id] = smap
        return smap

    try:
        smap = await asyncio.wait_for(
            OsmCoverSource().fetch_shade(tile, radius, frame=frame),
            timeout=SETTINGS.cold_fetch_budget_s,
        )
    except (asyncio.TimeoutError, TimeoutError):
        fallbacks.append(
            f"shade fetch timed out after {SETTINGS.cold_fetch_budget_s:g}s "
            f"for tile {_tile_label(tile)}"
        )
        return None
    except OverpassError:
        fallbacks.append(f"Overpass unavailable for shade ({_tile_label(tile)})")
        return None
    except Exception as exc:  # noqa: BLE001
        fallbacks.append(
            f"shade fetch error for tile {_tile_label(tile)}: {type(exc).__name__}"
        )
        return None

    _SHADE_MEMO[cache_id] = smap
    return smap


async def _fetch_paths(
    tile: tuple[float, float, float, float],
    radius: float,
    fallbacks: list[str],
    frame: LocalFrame,
) -> PathMap | None:
    """The walking network for one tile. Same fixture/cache/network order as covers."""
    cache_id = tile_cache_id(
        (tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0, radius, "paths"
    )
    memo = _PATHS_MEMO.get(cache_id)
    if memo is not None and memo.frame.lat == frame.lat and memo.frame.lng == frame.lng:
        return memo

    if SETTINGS.mock:
        payload = read_fixture(fixtures_dir("s2_weather_cover"), cache_id)
        if payload is None:
            fallbacks.append(f"no committed paths fixture for tile {_tile_label(tile)}")
            return None
        pmap = parse_paths(tile, radius, payload, cache_id, frame)
        _PATHS_MEMO[cache_id] = pmap
        return pmap

    query = build_paths_query(_expanded(tile, radius))
    cached = read_cache(query, cache_key("s2", "paths", cache_id))
    if cached is not None:
        pmap = parse_paths(tile, radius, cached, cache_id, frame)
        _PATHS_MEMO[cache_id] = pmap
        return pmap

    try:
        pmap = await asyncio.wait_for(
            fetch_paths(tile, radius, frame=frame), timeout=SETTINGS.cold_fetch_budget_s
        )
    except (asyncio.TimeoutError, TimeoutError):
        fallbacks.append(
            f"paths fetch timed out after {SETTINGS.cold_fetch_budget_s:g}s "
            f"for tile {_tile_label(tile)}"
        )
        return None
    except OverpassError:
        fallbacks.append(f"Overpass unavailable for paths ({_tile_label(tile)})")
        return None
    except Exception as exc:  # noqa: BLE001
        fallbacks.append(f"paths fetch error for tile {_tile_label(tile)}: {type(exc).__name__}")
        return None

    _PATHS_MEMO[cache_id] = pmap
    return pmap


async def _merge_paths(
    tiles: list[tuple[float, float, float, float]],
    radius: float,
    fallbacks: list[str],
    frame: LocalFrame,
) -> PathMap | None:
    """Every tile's walking network in one frame, ways and entrances deduped by id."""
    ways: list[PathWay] = []
    entrances: dict[int, tuple[float, float]] = {}
    kerbs: dict[int, str] = {}
    levels: dict[int, str] = {}
    seen: set[int] = set()
    got = False
    for tile in tiles:
        pmap = await _fetch_paths(tile, radius, fallbacks, frame)
        if pmap is None:
            continue
        got = True
        for w in pmap.ways:
            if w.way_id not in seen:
                seen.add(w.way_id)
                ways.append(w)
        entrances.update(pmap.entrances)
        kerbs.update(pmap.kerbs)
        levels.update(pmap.entrance_levels)
    if not got:
        return None
    bbox = (min(t[0] for t in tiles), min(t[1] for t in tiles), max(t[2] for t in tiles), max(t[3] for t in tiles))
    return PathMap(frame=frame, bbox=bbox, ways=ways, entrances=entrances, cache_id="merged",
                   kerbs=kerbs, entrance_levels=levels)


async def _walk_network(
    tiles: list[tuple[float, float, float, float]],
    radius: float,
    fallbacks: list[str],
    frame: LocalFrame,
) -> tuple[walk_network.Network, CoverMap | None, ShadeMap | None] | None:
    """The accessible walking network for these tiles, or None without path data.

    Every mode routes over it. Buildings come from the shade document: they are
    what is dry indoors, what a connector may not cut through, and where doors
    are. A missing shade tile only means buildings are ignored, so routing still
    runs on the paths alone.
    """
    path_map = await _merge_paths(tiles, radius, fallbacks, frame)
    if path_map is None or not path_map.ways:
        return None
    cover_map, shade_map = await _merge_maps(tiles, radius, fallbacks, frame, want_shade=True)

    # Keyed on which documents were present too: a network built while a cover
    # or shade tile was missing is a degraded one, and must not be reused once
    # the data arrives.
    key = (tuple(sorted(tiles)), _utm_epsg(frame.lat, frame.lng),
           cover_map is not None, shade_map is not None)
    net = _NETWORK_MEMO.get(key)
    if net is None:
        net = walk_network.build_network(path_map, cover_map, shade_map)
        _NETWORK_MEMO[key] = net
    return net, cover_map, shade_map


def _route_factor(route: walk_network.Route | None) -> float:
    """How much a route's accessibility penalties discount a sun score: the
    walk factor at the effective length over the walk factor at the real one."""
    if route is None or route.penalty <= 0:
        return 1.0
    return scoring.walk_factor(route.length + route.penalty) / scoring.walk_factor(route.length)


def _local(when: datetime) -> datetime:
    """Pickup time on the demo area's wall clock, which is what building hours use."""
    return when.astimezone(ZoneInfo(SETTINGS.demo_tz))


def _routes(
    net: walk_network.Network, req: RankRequest, frame: LocalFrame, closed: frozenset[str],
) -> dict[str, walk_network.Route | None]:
    """Shortest accessible route, by length, from the rider to every spot."""
    s = walk_network.search(
        net, frame.to_m(req.rider_location.lat, req.rider_location.lng),
        mode="length", closed=closed,
    )
    return {
        spot.spot_id: walk_network.route_to(
            net, s, frame.to_m(spot.stop_point.lat, spot.stop_point.lng)
        )
        for spot in req.spots
    }


def _with_route(r: RankedSpot, route: walk_network.Route | None, frame: LocalFrame) -> RankedSpot:
    """Attach a route's walk, polyline and notes to a ranked spot."""
    if route is None:
        return r
    return r.model_copy(update={
        "spot": r.spot.model_copy(update={"walk_distance_m": round(route.length, 1)}),
        "walk_polyline": polyline.encode(
            [frame.to_ll(x, y) for x, y in route.points], precision=5
        ),
        "indoor_m": round(route.indoor_m, 1),
        "route_notes": list(route.notes),
    })


def _by_route(
    spots: list[Spot], routes: dict[str, walk_network.Route | None], reason: str,
    frame: LocalFrame,
) -> list[RankedSpot]:
    """Neutral ranking over real routes: shortest effective walk first, where a
    route's accessibility penalties count as extra metres."""
    out = []
    for s in spots:
        route = routes.get(s.spot_id)
        effective = route.length + route.penalty if route else s.walk_distance_m
        r = RankedSpot(
            spot=s, wait_point=s.stop_point, score=round(scoring.no_cover_score(effective), 4),
            confidence=s.confidence, reason=reason,
        )
        out.append(_with_route(r, route, frame))
    out.sort(key=lambda r: (-r.score, r.spot.walk_distance_m, r.spot.spot_id))
    return out


def _curb_factor(spot: Spot) -> float:
    table = SETTINGS.curb_access_factor
    factor = float(table.get(spot.curb_access.value, 1.0))
    if spot.curb_access is CurbAccess.LOWERED and spot.ramp_distance_m is not None:
        factor -= SETTINGS.curb_lowered_decay * min(spot.ramp_distance_m / CURB_RAMP_MAX_DISTANCE_M, 1.0)
    return factor


def _apply_curb_access(ranked: list[RankedSpot]) -> list[RankedSpot]:
    """Scale every score by the kerb at the car door, then re-rank.

    Applied last and in every mode, including the fallbacks: a spot with an
    unknown kerb is still offered, just behind an equally good one with a ramp.
    The re-sort is stable, so each ranking's own tie-breaks survive.
    """
    out = [r.model_copy(update={"score": round(r.score * _curb_factor(r.spot), 4)}) for r in ranked]
    out.sort(key=lambda r: -r.score)
    return out


def _expanded(
    tile: tuple[float, float, float, float], radius: float
) -> tuple[float, float, float, float]:
    return expanded_query_bbox(tile, radius)





async def _merge_maps(
    tiles: list[tuple[float, float, float, float]],
    radius: float,
    fallbacks: list[str],
    frame: LocalFrame,
    *,
    want_shade: bool,
) -> tuple[CoverMap | None, ShadeMap | None]:
    """Fetch every tile, then combine them into one document per kind.

    Every tile is parsed into the *same* ``frame``, supplied by the caller. That
    is the whole reason this function takes a frame rather than each tile picking
    its own. ``LocalFrame.to_m`` returns absolute UTM and uses the anchor only to
    pick the EPSG, so within one zone the choice is numerically irrelevant --
    but across a zone boundary it is not a rounding difference, it is 600 km
    (measured on a point at -78.0 longitude: 800 934 E in 17N against 199 066 E in
    18N, an apparent 601 869 m). Per-tile frames would put cover hundreds of
    kilometres from the spots being ranked, silently, in exactly the places
    where a rider is standing. One frame makes that unrepresentable.

    Concatenating rather than geometrically unioning is deliberate. Each ranking
    module builds one spatial index over the whole feature set and asks it
    questions about the whole area, so what matters is that the features share a
    frame, not that they are pre-merged. Unioning 150 building footprints per
    request to produce one geometry nobody measures against would be real work
    for no consumer.

    Features are deduped by id on the way in. Tiles are containment tiles whose
    *query* areas are expanded by the search radius, so adjacent tiles genuinely
    overlap and the same way returns from both. Without this a shelter in the
    overlap appears twice in ``overlays.cover_features``, which is the layer a
    map draws -- the rider sees one awning rendered on top of itself.
    """
    covers: list[Cover] = []
    blocks: list[ShadeBlock] = []
    seen_covers: set[str] = set()
    seen_blocks: set[str] = set()
    got_cover = False
    got_shade = False

    for tile in tiles:
        cmap = await _fetch_covers(tile, radius, fallbacks, frame)
        if cmap is not None:
            got_cover = True
            for c in cmap.covers:
                if c.feature_id not in seen_covers:
                    seen_covers.add(c.feature_id)
                    covers.append(c)
        if want_shade:
            smap = await _fetch_shade(tile, radius, fallbacks, frame)
            if smap is not None:
                got_shade = True
                for b in smap.blocks:
                    if b.block_id not in seen_blocks:
                        seen_blocks.add(b.block_id)
                        blocks.append(b)

    if not got_cover and not got_shade:
        return None, None

    # The union of the tiles' query areas, not one tile's. Used for the response
    # bbox and by `/conditions/features` to tell a client what area it got.
    bbox = (
        min(t[0] for t in tiles),
        min(t[1] for t in tiles),
        max(t[2] for t in tiles),
        max(t[3] for t in tiles),
    )

    cover_map = (
        CoverMap(frame=frame, bbox=bbox, covers=covers, cache_id="merged")
        if got_cover else None
    )
    shade_map = (
        ShadeMap(frame=frame, bbox=bbox, blocks=blocks, cache_id="merged")
        if got_shade else None
    )
    return cover_map, shade_map


# --------------------------------------------------------------------------- #
# The main entry point
# --------------------------------------------------------------------------- #

async def rank(req: RankRequest) -> ConditionsResult:
    """Weather, then a ranking appropriate to it. Never raises.

    The demo override is applied here rather than left to ``weather.assess`` alone
    so that a forced *time* also drives the sun position -- forcing ``sun`` at a
    timestamp whose sun is 4 degrees up would otherwise produce a sun-mode
    response that immediately falls back to walk distance, which is a confusing
    way to demonstrate the feature.
    """
    fallbacks: list[str] = []
    when = req.force_time or req.pickup_time
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)

    try:
        assessment = await weather.assess(
            req.rider_location.lat,
            req.rider_location.lng,
            when,
            req.wait_minutes,
            force_condition=req.force_condition,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("weather assessment raised")
        fallbacks.append(f"weather error: {type(exc).__name__}")
        assessment = weather.Assessment(
            report=weather.fallback_weather(when),
            mode=Condition.NEUTRAL,
            reason="Weather unavailable",
        )

    mode = assessment.mode
    radius = search_radius_m(req)
    tiles = _tiles_covering(req, radius)
    # One frame for the whole request, anchored on the rider. Every tile is
    # projected into it, so a spot and a shelter 4 m apart measure 4 m apart no
    # matter which tiles they came from.
    frame = frame_for(req.rider_location.lat, req.rider_location.lng)

    ranked: list[RankedSpot] = []
    overlays = Overlays()
    shade_source: ShadeSource | None = None
    sun = None

    # The accessible walking network, for every mode. A failure here costs the
    # routes, never the ranking: each mode below has its pre-network answer.
    net = None
    closed: frozenset[str] = frozenset()
    routes: dict[str, walk_network.Route | None] = {}
    if req.spots and not (mode is Condition.RAIN and SETTINGS.rain_ranking != "exposure"):
        try:
            bundle = await _walk_network(tiles, radius, fallbacks, frame)
            if bundle is None:
                fallbacks.append("no walking network; walk distances are S1 estimates")
            else:
                net = bundle[0]
                closed = walk_network.closed_buildings(net, _local(when))
        except Exception as exc:  # noqa: BLE001
            log.exception("walking network failed")
            fallbacks.append(f"walking network error: {type(exc).__name__}")
            net = None

    try:
        if mode is Condition.RAIN:
            if net is not None:
                ranked = rain_exposure.rank_by_exposure(
                    req.spots, req.rider_location, net, frame, closed=closed
                )
                cover_map, _ = await _merge_maps(tiles, radius, fallbacks, frame, want_shade=False)
                overlays = Overlays(cover_features=[c.as_model() for c in (cover_map.covers if cover_map else [])])
            else:
                if SETTINGS.rain_ranking == "exposure" and req.spots:
                    fallbacks.append("no walking network; ranked rain by distance to cover")
                cover_map, _ = await _merge_maps(
                    tiles, radius, fallbacks, frame, want_shade=False
                )
                if cover_map is None:
                    fallbacks.append("no rain cover data")
                    ranked = _by_walk(req.spots, "No cover data available nearby")
                else:
                    ranked = rain_cover.rank_spots(req.spots, cover_map)
                    overlays = Overlays(cover_features=[c.as_model() for c in cover_map.covers])

        elif mode is Condition.SUN:
            sun = weather.sun_position(req.rider_location.lat, req.rider_location.lng, when)
            cover_map, shade_map = await _merge_maps(
                tiles, radius, fallbacks, frame, want_shade=True
            )
            if net is not None:
                routes = _routes(net, req, frame, closed)
            # Sun scoring reads walk_distance_m, so hand it the real route length.
            sun_spots = [
                s.model_copy(update={"walk_distance_m": round(routes[s.spot_id].length, 1)})
                if routes.get(s.spot_id) else s
                for s in req.spots
            ]
            if sun is None:
                fallbacks.append("sun position unavailable")
                ranked = _by_walk(sun_spots, "Could not compute the sun's position")
            elif shade_map is None:
                fallbacks.append("no shade geometry")
                ranked = _by_walk(sun_spots, "No shade data for this area")
            else:
                later = when + timedelta(minutes=max(req.wait_minutes, 1))
                sun_later = weather.sun_position(
                    req.rider_location.lat, req.rider_location.lng, later
                ) or sun
                ranked, geom = sun_shade.rank_spots(
                    sun_spots, shade_map, cover_map or _empty_covers(shade_map), sun, sun_later
                )
                # Only claim a shade source when there *was* shade geometry to
                # measure with. `rank_spots` returns geom=None for the low-sun
                # and empty-area fallbacks, and reporting `osm_geometry` there
                # would tell a reader the answer came from a shadow model when
                # the answer was in fact "walk distance, sun is low" -- the
                # honesty field has to be able to say "nothing".
                if geom is not None:
                    shade_source = ShadeSource.OSM_GEOMETRY
                overlays = Overlays(
                    cover_features=[c.as_model() for c in (cover_map.covers if cover_map else [])],
                    shade_geojson=_shade_geojson(geom),
                )
            if routes:
                ranked = [_with_route(r, routes.get(r.spot.spot_id), frame) for r in ranked]
                # Route penalties (steps, unramped crossings) as a walk-factor
                # discount: the same effective extra metres neutral mode charges.
                ranked = [
                    r.model_copy(update={"score": round(r.score * _route_factor(routes.get(r.spot.spot_id)), 4)})
                    for r in ranked
                ]

        else:
            if net is not None:
                routes = _routes(net, req, frame, closed)
                ranked = _by_route(req.spots, routes, assessment.reason, frame)
            else:
                ranked = _by_walk(req.spots, assessment.reason)

    except Exception as exc:  # noqa: BLE001 - §6: a data problem is not an error
        log.exception("ranking raised in %s mode", mode.value)
        fallbacks.append(f"{mode.value}: {type(exc).__name__}")
        ranked = _by_walk(req.spots, "Conditions data unavailable")
        shade_source = None
        overlays = Overlays()

    if not ranked and req.spots:
        ranked = _by_walk(req.spots, assessment.reason)
    ranked = _apply_curb_access(ranked)

    nearest_id = min(
        req.spots, key=lambda s: (s.walk_distance_m, s.spot_id)
    ).spot_id if req.spots else None

    needs_confirm = False
    protected = ranked and (ranked[0].wet_m is not None or ranked[0].cover_feature is not None)
    if protected and mode is not Condition.NEUTRAL:
        if any(r.walk_polyline for r in ranked):
            # Real route lengths: compare the winner with the shortest real walk
            # among the candidates, not with S1's straight-line estimate.
            shortest = min(r.spot.walk_distance_m for r in ranked)
        else:
            shortest = next(
                (s.walk_distance_m for s in req.spots if s.spot_id == nearest_id), 0.0
            )
        needs_confirm = scoring.needs_detour_confirmation(ranked[0].spot.walk_distance_m, shortest)

    return ConditionsResult(
        weather=assessment.report,
        mode=mode,
        ranked=ranked,
        needs_rider_confirmation=needs_confirm,
        nearest_spot_id=nearest_id,
        sun=sun,
        shade_source=shade_source,
        overlays=overlays,
        fallbacks_used=fallbacks,
    )


async def walk_routes(req: WalkRoutesRequest) -> WalkRoutesResponse:
    """Accessible walking routes from the rider to each spot, with no ranking.

    Never raises: a missing network is an empty answer plus a fallback note, and
    the caller keeps S1's estimates.
    """
    fallbacks: list[str] = []
    if not req.spots:
        return WalkRoutesResponse()
    when = req.pickup_time or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    rank_like = RankRequest(rider_location=req.rider_location, spots=req.spots, pickup_time=when)
    radius = search_radius_m(rank_like)
    frame = frame_for(req.rider_location.lat, req.rider_location.lng)
    try:
        bundle = await _walk_network(_tiles_covering(rank_like, radius), radius, fallbacks, frame)
        if bundle is None:
            fallbacks.append("no walking network; walk distances are S1 estimates")
            return WalkRoutesResponse(fallbacks_used=fallbacks)
        net = bundle[0]
        routes = _routes(net, rank_like, frame, walk_network.closed_buildings(net, _local(when)))
    except Exception as exc:  # noqa: BLE001
        log.exception("walk routes failed")
        fallbacks.append(f"walking network error: {type(exc).__name__}")
        return WalkRoutesResponse(fallbacks_used=fallbacks)

    out = [
        WalkRoute(
            spot_id=sid,
            walk_m=round(r.length, 1),
            walk_polyline=polyline.encode([frame.to_ll(x, y) for x, y in r.points], precision=5),
            indoor_m=round(r.indoor_m, 1),
            route_notes=list(r.notes),
        )
        for sid, r in routes.items() if r is not None
    ]
    return WalkRoutesResponse(routes=out, fallbacks_used=fallbacks)


def cache_state(lat: float | None = None, lng: float | None = None) -> dict[str, Any]:
    """What is cached, so the demo can be checked before it starts.

    A demo that fails because cover data was never fetched fails in the first
    thirty seconds, in front of people. With no coordinates this reports the
    in-process memo; with them, the area that would be queried.
    """
    return {
        "tiles_memoised": {
            "cover": len(_COVER_MEMO),
            "shade": len(_SHADE_MEMO),
        },
        "checked_area": (
            list(tile_bbox(lat, lng, 150.0))
            if lat is not None and lng is not None
            else None
        ),
    }


async def features(
    lat: float, lng: float, radius_m: float
) -> dict[str, Any]:
    """Every cover and shade feature near a point, with no ranking applied.

    Public so ``main.py`` does not have to reach into ``_merge_maps``. The point
    of the route is diagnosis: an empty ``cover_features`` list here is the
    reason a rain ranking looks flat, and that is far easier to see here than by
    inspecting a ranking that is merely uninteresting.
    """
    radius = min(max(radius_m, 10.0), 500.0)
    fallbacks: list[str] = []
    cover_map, shade_map = await _merge_maps(
        [tile_bbox(lat, lng, radius)],
        radius,
        fallbacks,
        frame_for(lat, lng),
        want_shade=True,
    )
    return {
        "cover_features": [c.as_model() for c in (cover_map.covers if cover_map else [])],
        "shade_blocks": shade_map.summary() if shade_map else {"blocks": 0},
        "bbox": list(cover_map.bbox) if cover_map else None,
        "fallbacks_used": fallbacks,
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _by_walk(spots: list[Spot], reason: str) -> list[RankedSpot]:
    """The §6 neutral fallback: nearest first, all with a clear reason."""
    out = [
        RankedSpot(
            spot=s,
            wait_point=s.stop_point,
            cover_feature=None,
            gap_m=None,
            score=round(scoring.no_cover_score(s.walk_distance_m), 4),
            confidence=s.confidence,
            reason=reason,
        )
        for s in spots
    ]
    out.sort(key=lambda r: (r.spot.walk_distance_m, r.spot.spot_id))
    return out


def _empty_covers(shade_map: ShadeMap) -> CoverMap:
    return CoverMap(frame=shade_map.frame, bbox=shade_map.bbox, covers=[])


def _shade_geojson(geom) -> dict | None:
    """The shade geometry as a GeoJSON FeatureCollection for the map layer.

    Returned even when it is a union of 60 building shadows, because that is
    exactly what makes the sun demo legible: a rider shown the shaded half of the
    street can see *why* the recommendation moved when the time changed.
    """
    from shapely.geometry import mapping

    if geom is None or geom.is_empty:
        return None
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": mapping(geom), "properties": {"kind": "shade"}}
    ]}


def clear_caches() -> None:
    _COVER_MEMO.clear()
    _SHADE_MEMO.clear()
    _PATHS_MEMO.clear()
    _NETWORK_MEMO.clear()
    weather.clear_cache()


__all__ = ["rank", "search_radius_m", "clear_caches"]
