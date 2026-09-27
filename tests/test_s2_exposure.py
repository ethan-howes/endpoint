"""The rain exposure ranking: metres in the rain along the walk, not kerb-to-cover gap.

Hermetic: every scenario is a hand-built network in metres around ``TEST_ORIGIN``,
so the geometry in each assertion can be checked on paper. No fixtures, no network.

The scenario the model exists for: a rider inside a building whose covered
passage ends 11 m short of a kerb. The gap model measures kerb-to-cover (14 m,
past the point where cover counts) and sends the rider to a nearer kerb across
open ground; the exposure model follows the walk and sees that the passage keeps
the rider dry for most of it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import polyline
import pytest
from shapely.geometry import LineString, Polygon

from services.s2_weather_cover import rain_cover, rain_exposure, service
from services.s2_weather_cover.cover import Cover, CoverMap, ShadeBlock, ShadeMap
from services.s2_weather_cover.paths import PathMap, PathWay, is_walkable, parse_paths
from shared.config import SETTINGS
from shared.geo import LocalFrame
from shared.models import Condition, LatLng, RankRequest, Spot

TEST_ORIGIN = (25.756918, -80.372182)
FRAME = LocalFrame(*TEST_ORIGIN)
OX, OY = FRAME.to_m(*TEST_ORIGIN)


def at(dx: float, dy: float) -> tuple[float, float]:
    """A point ``dx`` m east and ``dy`` m north of the origin, in frame metres."""
    return (OX + dx, OY + dy)


def ll(dx: float, dy: float) -> LatLng:
    lat, lng = FRAME.to_ll(*at(dx, dy))
    return LatLng(lat=lat, lng=lng)


def way(way_id: int, nodes: list[tuple[int, float, float]], *, covered: bool = False, highway: str = "footway") -> PathWay:
    return PathWay(
        way_id=way_id,
        node_ids=tuple(n for n, _, _ in nodes),
        points=tuple(at(x, y) for _, x, y in nodes),
        highway=highway,
        covered=covered,
    )


def spot(spot_id: str, dx: float, dy: float) -> Spot:
    # S1's walk estimate: straight line x 1.3. Deliberately what the gap model sees.
    walk = (dx * dx + dy * dy) ** 0.5 * 1.3
    return Spot(spot_id=spot_id, stop_point=ll(dx, dy), walk_distance_m=walk)


@pytest.fixture
def campus():
    """Rider inside a 20 x 20 m building at the origin.

    - A covered passage leaves the south wall at (0, -10) and runs to (0, -40),
      then an open footway continues to the kerb of an east-west road at y = -51.
    - An open footway leaves the east wall at (10, 0) and runs to a north-south
      road at x = 30.
    - Spot A is on the y = -51 road below the passage: 54 m away as the crow flies.
    - Spot B is on the x = 30 road beside the building: 33 m away.
    """
    building = Polygon([at(-10, -10), at(10, -10), at(10, 10), at(-10, 10)])
    passage = LineString([at(0, -10), at(0, -40)])
    ways = [
        way(1, [(1, 0, -10), (2, 0, -40)], covered=True),
        way(2, [(2, 0, -40), (3, 0, -51)]),
        way(3, [(4, -60, -51), (3, 0, -51), (5, 60, -51)], highway="service"),
        way(4, [(6, 10, 0), (7, 30, 0)]),
        way(5, [(8, 30, -60), (7, 30, 0), (9, 30, 40)], highway="service"),
    ]
    path_map = PathMap(frame=FRAME, bbox=(0, 0, 0, 0), ways=ways, entrances={1: at(0, -10), 6: at(10, 0)})
    cover_map = CoverMap(frame=FRAME, bbox=(0, 0, 0, 0), covers=[
        Cover(feature_id="osm_way_1", kind="building_passage", shape=passage, provides_sun=True),
    ])
    shade_map = ShadeMap(frame=FRAME, bbox=(0, 0, 0, 0), blocks=[
        ShadeBlock(block_id="b1", shape=building, height_m=10.0, kind="building"),
    ])
    spots = [spot("A", 0, -54), spot("B", 33, 0)]
    return path_map, cover_map, shade_map, spots


def _dry_cost(monkeypatch, value: float) -> None:
    """Pin ``exposure_dry_cost`` in every module that reads it."""
    from services.s2_weather_cover import walk_network

    cfg = replace(SETTINGS, exposure_dry_cost=value)
    for mod in (walk_network, rain_exposure, service):
        monkeypatch.setattr(mod, "SETTINGS", cfg)


class TestExposureRanking:
    def test_the_covered_exit_beats_the_nearer_open_kerb(self, campus, monkeypatch):
        """With dry metres nearly free (0.1), the covered exit wins even though it
        is 21 m longer. This was the default; see the next test for why it is not."""
        _dry_cost(monkeypatch, 0.1)
        path_map, cover_map, shade_map, spots = campus
        net = rain_exposure.build_network(path_map, cover_map, shade_map)
        ranked = rain_exposure.rank_by_exposure(spots, ll(0, 0), net, FRAME)

        best = ranked[0]
        assert best.spot.spot_id == "A"
        # Indoors (10 m) and through the passage (30 m, plus its buffered end cap)
        # is dry; the rest of the footway and the ~3 m to the stop point is not.
        half = SETTINGS.cover_path_half_width_m
        assert best.dry_m == pytest.approx(40.0 + half, abs=1.0)
        assert best.wet_m == pytest.approx(14.0 - half, abs=1.5)
        b = next(r for r in ranked if r.spot.spot_id == "B")
        assert b.wet_m > best.wet_m

    def test_at_the_default_a_long_covered_detour_loses_to_a_short_walk(self, campus):
        """At exposure_dry_cost 0.5 a dry metre costs half a wet one, so 21 extra
        metres to save 9 of rain is not worth it for a rider with a walker --
        the setting that stopped routes walking past the car and back."""
        path_map, cover_map, shade_map, spots = campus
        net = rain_exposure.build_network(path_map, cover_map, shade_map)
        ranked = rain_exposure.rank_by_exposure(spots, ll(0, 0), net, FRAME)
        assert ranked[0].spot.spot_id == "B"
        a = next(r for r in ranked if r.spot.spot_id == "A")
        assert a.wet_m < ranked[0].wet_m  # A is still the drier walk

    def test_the_gap_model_gets_this_case_wrong(self, campus):
        """The regression the exposure model fixes, pinned so it stays fixed."""
        _, cover_map, _, spots = campus
        gap_pick = rain_cover.rank_spots(spots, cover_map)[0]
        assert gap_pick.spot.spot_id == "B"

    def test_walk_distance_is_the_real_route_not_the_estimate(self, campus):
        path_map, cover_map, shade_map, spots = campus
        net = rain_exposure.build_network(path_map, cover_map, shade_map)
        a = next(r for r in rain_exposure.rank_by_exposure(spots, ll(0, 0), net, FRAME) if r.spot.spot_id == "A")
        # 10 indoors + 30 passage + 11 footway + 3 to the stop point.
        assert a.spot.walk_distance_m == pytest.approx(54.0, abs=1.0)
        assert a.wet_m + a.dry_m == pytest.approx(a.spot.walk_distance_m, abs=0.5)

    def test_the_route_is_returned_from_rider_to_stop(self, campus):
        path_map, cover_map, shade_map, spots = campus
        net = rain_exposure.build_network(path_map, cover_map, shade_map)
        a = next(r for r in rain_exposure.rank_by_exposure(spots, ll(0, 0), net, FRAME) if r.spot.spot_id == "A")
        pts = polyline.decode(a.walk_polyline, precision=5)
        rider, stop = ll(0, 0), ll(0, -54)
        assert pts[0] == pytest.approx((rider.lat, rider.lng), abs=1e-5)
        assert pts[-1] == pytest.approx((stop.lat, stop.lng), abs=1e-5)

    def test_the_wait_point_is_the_end_of_the_passage(self, campus):
        path_map, cover_map, shade_map, spots = campus
        net = rain_exposure.build_network(path_map, cover_map, shade_map)
        a = next(r for r in rain_exposure.rank_by_exposure(spots, ll(0, 0), net, FRAME) if r.spot.spot_id == "A")
        wx, wy = FRAME.to_m(a.wait_point.lat, a.wait_point.lng)
        # Dry until the passage's buffered end (y = -40 - half width).
        assert wy - OY == pytest.approx(-40 - SETTINGS.cover_path_half_width_m, abs=0.5)
        assert a.cover_feature is not None and a.cover_feature.kind == "building_passage"
        assert "wait under the building passage until the car arrives" in a.reason

    def test_a_fully_covered_walk_says_so(self):
        building = Polygon([at(-10, -10), at(10, -10), at(10, 10), at(-10, 10)])
        path_map = PathMap(frame=FRAME, bbox=(0, 0, 0, 0), ways=[
            way(1, [(1, 0, -10), (2, 0, -30)], covered=True),
        ])
        cover_map = CoverMap(frame=FRAME, bbox=(0, 0, 0, 0), covers=[
            Cover(feature_id="c", kind="covered_walkway",
                  shape=LineString([at(0, -10), at(0, -31)]), provides_sun=True),
        ])
        shade_map = ShadeMap(frame=FRAME, bbox=(0, 0, 0, 0), blocks=[
            ShadeBlock(block_id="b", shape=building, height_m=10.0, kind="building"),
        ])
        net = rain_exposure.build_network(path_map, cover_map, shade_map)
        r = rain_exposure.rank_by_exposure([spot("S", 0, -31)], ll(0, 0), net, FRAME)[0]
        assert r.wet_m < 1.0
        assert r.reason.startswith("Covered all the way")

    def test_a_spot_off_the_network_is_assumed_wet(self, campus):
        path_map, cover_map, shade_map, _ = campus
        net = rain_exposure.build_network(path_map, cover_map, shade_map)
        far = spot("far", 500, 500)
        r = rain_exposure.rank_by_exposure([far], ll(0, 0), net, FRAME)[0]
        assert r.wet_m == pytest.approx(far.walk_distance_m, abs=0.5)
        assert "No walking route found" in r.reason

    def test_score_prefers_less_rain_and_stays_in_range(self):
        assert rain_exposure.score(0.0, 0.0) == pytest.approx(1.0)
        assert rain_exposure.score(10.0, 0.0) > rain_exposure.score(20.0, 0.0)
        # Dry metres cost something, but less than wet ones: 20 dry metres are
        # worth taking to avoid 20 wet ones; 100 are not (exposure_dry_cost 0.5).
        assert rain_exposure.score(10.0, 20.0) > rain_exposure.score(30.0, 0.0)
        assert rain_exposure.score(10.0, 100.0) < rain_exposure.score(30.0, 0.0)
        assert 0.0 < rain_exposure.score(500.0, 500.0) < 1.0


class TestPathParsing:
    def test_unwalkable_ways_are_dropped(self):
        assert is_walkable({"highway": "footway"})
        assert is_walkable({"highway": "service", "access": "private"})
        assert not is_walkable({"highway": "motorway"})
        assert not is_walkable({"highway": "footway", "foot": "no"})
        assert not is_walkable({"building": "yes"})

    def test_parse_keeps_node_ids_aligned_with_geometry_and_marks_cover(self):
        payload = {"elements": [
            {"type": "way", "id": 1, "tags": {"highway": "footway", "tunnel": "building_passage"},
             "nodes": [10, 11, 12],
             "geometry": [{"lat": 25.7560, "lon": -80.3720}, None, {"lat": 25.7562, "lon": -80.3720}]},
            {"type": "way", "id": 2, "tags": {"highway": "motorway"}, "nodes": [1, 2],
             "geometry": [{"lat": 25.75, "lon": -80.37}, {"lat": 25.76, "lon": -80.37}]},
            {"type": "node", "id": 99, "lat": 25.7561, "lon": -80.3721, "tags": {"entrance": "main"}},
        ]}
        pm = parse_paths((25.752, -80.376, 25.756, -80.372), 150.0, payload, "paths:test", FRAME)
        assert [w.way_id for w in pm.ways] == [1]
        # The null geometry entry is dropped with its node id, not shifted onto the next one.
        assert pm.ways[0].node_ids == (10, 12)
        assert pm.ways[0].covered is True
        assert 99 in pm.entrances


class TestServiceSelection:
    """``service.rank`` uses the exposure model when it has a walking network, and
    says so when it falls back to the gap model."""

    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch, campus):
        path_map, cover_map, shade_map, spots = campus
        self.spots = spots
        service.clear_caches()

        async def covers(tile, radius, fallbacks, frame):
            return cover_map

        async def shade(tile, radius, fallbacks, frame):
            return shade_map

        async def paths(tile, radius, fallbacks, frame):
            return path_map

        monkeypatch.setattr(service, "_fetch_covers", covers)
        monkeypatch.setattr(service, "_fetch_shade", shade)
        monkeypatch.setattr(service, "_fetch_paths", paths)
        self.monkeypatch = monkeypatch
        yield
        service.clear_caches()

    def _request(self) -> RankRequest:
        return RankRequest(
            rider_location=ll(0, 0), spots=self.spots,
            pickup_time=datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc),
            force_condition=Condition.RAIN,
        )

    @pytest.mark.anyio
    async def test_exposure_is_the_default(self):
        _dry_cost(self.monkeypatch, 0.1)  # so the exposure pick differs from the gap pick
        result = await service.rank(self._request())
        assert result.ranked[0].spot.spot_id == "A"
        assert result.ranked[0].wet_m is not None
        assert result.ranked[0].walk_polyline

    @pytest.mark.anyio
    async def test_the_gap_model_is_still_selectable(self):
        self.monkeypatch.setattr(service, "SETTINGS", replace(SETTINGS, rain_ranking="gap"))
        result = await service.rank(self._request())
        assert result.ranked[0].spot.spot_id == "B"
        assert result.ranked[0].wet_m is None

    @pytest.mark.anyio
    async def test_no_walking_network_falls_back_to_gap_with_a_note(self):
        async def none(tile, radius, fallbacks, frame):
            return None

        self.monkeypatch.setattr(service, "_fetch_paths", none)
        result = await service.rank(self._request())
        assert result.ranked[0].wet_m is None
        assert any("no walking network" in f for f in result.fallbacks_used)


class TestWalkRoutes:
    """``POST /walk/routes``: routes only, for rides with no comfort features."""

    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch, campus):
        path_map, cover_map, shade_map, spots = campus
        self.spots = spots
        service.clear_caches()

        async def covers(tile, radius, fallbacks, frame):
            return cover_map

        async def shade(tile, radius, fallbacks, frame):
            return shade_map

        async def paths(tile, radius, fallbacks, frame):
            return path_map

        monkeypatch.setattr(service, "_fetch_covers", covers)
        monkeypatch.setattr(service, "_fetch_shade", shade)
        monkeypatch.setattr(service, "_fetch_paths", paths)
        self.monkeypatch = monkeypatch
        yield
        service.clear_caches()

    @pytest.mark.anyio
    async def test_returns_a_real_route_per_reachable_spot(self):
        from shared.models import WalkRoutesRequest

        res = await service.walk_routes(WalkRoutesRequest(rider_location=ll(0, 0), spots=self.spots))
        by_id = {r.spot_id: r for r in res.routes}
        assert set(by_id) == {"A", "B"}
        assert by_id["A"].walk_m == pytest.approx(54.0, abs=1.5)
        assert by_id["A"].walk_polyline

    @pytest.mark.anyio
    async def test_no_network_is_an_empty_answer_with_a_note(self):
        from shared.models import WalkRoutesRequest

        async def none(tile, radius, fallbacks, frame):
            return None

        self.monkeypatch.setattr(service, "_fetch_paths", none)
        res = await service.walk_routes(WalkRoutesRequest(rider_location=ll(0, 0), spots=self.spots))
        assert res.routes == []
        assert any("no walking network" in f for f in res.fallbacks_used)


class TestAccessiblePriority:
    """``priority=accessible`` ranks by the easiest walk, not by rain cover."""

    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch, campus):
        path_map, cover_map, shade_map, spots = campus
        self.spots = spots
        service.clear_caches()

        async def covers(tile, radius, fallbacks, frame):
            return cover_map

        async def shade(tile, radius, fallbacks, frame):
            return shade_map

        async def paths(tile, radius, fallbacks, frame):
            return path_map

        monkeypatch.setattr(service, "_fetch_covers", covers)
        monkeypatch.setattr(service, "_fetch_shade", shade)
        monkeypatch.setattr(service, "_fetch_paths", paths)
        yield
        service.clear_caches()

    def _req(self, priority):
        return RankRequest(
            rider_location=ll(0, 0), spots=self.spots,
            pickup_time=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc),
            force_condition=Condition.RAIN, priority=priority,
        )

    @pytest.mark.anyio
    async def test_accessible_and_weather_choose_differently(self, monkeypatch):
        """In the campus fixture the covered exit (A) is the dry choice and the
        nearer open kerb (B) the shorter walk. Dry metres are made cheap so the
        weather ranking takes the cover; accessible must not."""
        _dry_cost(monkeypatch, 0.1)
        weather = await service.rank(self._req("weather"))
        access = await service.rank(self._req("accessible"))
        assert weather.ranked[0].spot.spot_id == "A"
        assert access.ranked[0].spot.spot_id == "B"
        assert access.mode is Condition.RAIN  # the weather is still reported
        assert access.ranked[0].accessibility is not None
        assert access.ranked[0].reason.startswith("Step-free route")
        assert access.needs_rider_confirmation is False
