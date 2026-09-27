"""The accessible walking network: doors, hours, residence halls, walls, steps, kerbs.

Every scenario is a small hand-built campus in metres around a fixed origin, so
each assertion is about one rule. The layout most tests share:

    rider (-40, 0) --footway-- (-10, 0)|HALL 20x20|(10, 0) --footway-- (40, 0) -- stop (45, 0)
          \\                                                        /
           (-40, -30) ----------- long way round ----------- (40, -30)

The footways end at the hall's west and east walls, so those ends are its doors.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from shapely.geometry import LineString, Polygon

from services.s2_weather_cover import service, walk_network
from services.s2_weather_cover.cover import CoverMap, ShadeBlock, ShadeMap
from services.s2_weather_cover.opening_hours import in_default_window, is_open
from services.s2_weather_cover.paths import PathMap, PathWay
from shared.config import SETTINGS
from shared.geo import LocalFrame
from shared.models import CurbAccess, LatLng, RankedSpot, Spot

TEST_ORIGIN = (25.756918, -80.372182)
FRAME = LocalFrame(*TEST_ORIGIN)
OX, OY = FRAME.to_m(*TEST_ORIGIN)
TZ = ZoneInfo(SETTINGS.demo_tz)
NOON = datetime(2026, 9, 28, 12, 0, tzinfo=TZ)       # a Monday
LATE = datetime(2026, 9, 28, 23, 0, tzinfo=TZ)


def at(dx: float, dy: float) -> tuple[float, float]:
    return (OX + dx, OY + dy)


def way(way_id, nodes, *, highway="footway", footway="", surface="") -> PathWay:
    return PathWay(
        way_id=way_id, node_ids=tuple(n for n, _, _ in nodes),
        points=tuple(at(x, y) for _, x, y in nodes), highway=highway,
        footway=footway, surface=surface,
    )


def hall(building: str = "university", **kw) -> ShadeBlock:
    return ShadeBlock(
        block_id="hall", shape=Polygon([at(-10, -10), at(10, -10), at(10, 10), at(-10, 10)]),
        height_m=10.0, kind="building", building=building, name="Test Hall", **kw,
    )


def campus_ways() -> list[PathWay]:
    return [
        way(1, [(1, -40, 0), (10, -10, 0)]),                       # to the west door
        way(2, [(20, 10, 0), (2, 40, 0)]),                         # from the east door
        way(3, [(1, -40, 0), (3, -40, -30), (4, 40, -30), (2, 40, 0)]),  # the long way round
    ]


def build(blocks=None, ways=None, **pm_kw) -> walk_network.Network:
    pm = PathMap(frame=FRAME, bbox=(0, 0, 0, 0), ways=ways or campus_ways(), **pm_kw)
    sm = ShadeMap(frame=FRAME, bbox=(0, 0, 0, 0), blocks=blocks if blocks is not None else [hall()])
    return walk_network.build_network(pm, CoverMap(frame=FRAME, bbox=(0, 0, 0, 0)), sm)


def route(net, start=(-40, 0), stop=(45, 0), when=NOON, mode="length"):
    closed = walk_network.closed_buildings(net, when)
    s = walk_network.search(net, at(*start), mode=mode, closed=closed)
    return walk_network.route_to(net, s, at(*stop))


def _inside_hall_m(r) -> float:
    """Metres of the route's non-indoor legs that cut through the hall."""
    shape = hall().shape
    return sum(
        LineString([a, b]).intersection(shape).length
        for a, b in zip(r.points, r.points[1:])
    ) - r.indoor_m * 0 if r else 0.0


class TestDoors:
    def test_footway_ends_at_the_wall_are_doors(self):
        net = build()
        assert sorted(net.buildings["hall"].doors) == [10, 20]

    def test_a_mapped_entrance_on_the_wall_is_a_door(self):
        ways = [way(1, [(1, -40, 0), (5, -25, 0)])]  # stops short of the wall
        net = build(ways=ways, entrances={10: at(-10, 0), 20: at(10, 0)})
        assert sorted(net.buildings["hall"].doors) == [10, 20]

    def test_a_footway_that_stops_short_of_the_wall_is_not_a_door(self):
        ways = [way(1, [(1, -40, 0), (5, -15, 0)])]  # 5 m out
        net = build(ways=ways)
        assert net.buildings["hall"].doors == []


class TestThroughBuildings:
    def test_an_open_building_is_walked_through(self):
        r = route(build(), when=NOON)
        # 30 + 20 x 1.3 indoors + 30 + 5 to the stop.
        assert r.indoor_m == pytest.approx(20 * SETTINGS.indoor_detour_factor, abs=0.1)
        assert r.length == pytest.approx(30 + 26 + 30 + 5, abs=1.0)
        assert "through Test Hall" in r.notes

    def test_a_closed_building_is_a_wall(self):
        r = route(build(), when=LATE)
        assert r.indoor_m == 0.0
        assert r.length > 140  # the long way round
        assert not any("through" in n for n in r.notes)

    def test_default_hours_are_seven_to_ten(self):
        net = build()
        assert walk_network.closed_buildings(net, NOON.replace(hour=7)) == frozenset()
        assert walk_network.closed_buildings(net, NOON.replace(hour=6, minute=59)) == {"hall"}
        assert walk_network.closed_buildings(net, NOON.replace(hour=22)) == {"hall"}

    def test_a_tagged_opening_hours_beats_the_default(self):
        net = build(blocks=[hall(opening_hours="24/7")])
        assert walk_network.closed_buildings(net, LATE) == frozenset()

    @pytest.mark.parametrize("kind", ["dormitory", "apartments", "house", "residential"])
    def test_homes_and_residence_halls_are_never_walked_through(self, kind):
        r = route(build(blocks=[hall(building=kind)]), when=NOON)
        assert r.indoor_m == 0.0
        assert r.length > 140

    def test_a_private_building_is_never_walked_through(self):
        r = route(build(blocks=[hall(access="private")]), when=NOON)
        assert r.indoor_m == 0.0

    def test_a_rider_inside_a_residence_hall_may_leave_by_its_doors(self):
        """Leaving is not passing through: the doors open from inside."""
        r = route(build(blocks=[hall(building="dormitory")]), start=(0, 0), when=LATE)
        assert r is not None
        assert r.points[1] == pytest.approx(at(10, 0))  # straight to the east door
        assert r.length == pytest.approx(10 + 30 + 5, abs=1.0)

    def _one_sided(self, east_door: bool):
        """The hall's only door is on the west wall unless ``east_door``. The
        east footway starts 5 m out from the wall, so it is not an inferred door."""
        east_start = (10, 0) if east_door else (15, 0)
        ways = [
            way(1, [(1, -40, 0), (10, -10, 0)]),
            way(2, [(20, *east_start), (2, 40, 0)]),
            way(3, [(1, -40, 0), (3, -40, -30), (4, 40, -30), (2, 40, 0)]),
        ]
        return build(ways=ways)

    def test_a_rider_can_leave_by_the_nearest_side_when_no_door_is_mapped_there(self):
        """All the doors on the far side: without this the rider walks round the
        outside of the building. The exit costs extra and says what it assumed."""
        net = self._one_sided(east_door=False)
        assert net.buildings["hall"].doors == [10]
        r = route(net, start=(8, 0), when=NOON)
        assert "leaves by the nearest side; no door is mapped there" in r.notes
        assert r.length < 50  # ~7 m out + 25 m + 5 m, not ~160 m round
        assert r.penalty == pytest.approx(SETTINGS.nearest_side_exit_penalty_m)

    def test_a_mapped_door_nearby_still_wins(self):
        r = route(self._one_sided(east_door=True), start=(8, 0), when=NOON)
        assert not any("nearest side" in n for n in r.notes)
        assert r.points[1] == pytest.approx(at(10, 0))

    def test_the_nearest_side_exit_does_not_pass_through_another_building(self):
        net = self._one_sided(east_door=False)
        other = ShadeBlock(block_id="other", shape=Polygon([at(10.5, -5), at(14, -5), at(14, 5), at(10.5, 5)]),
                           height_m=5.0, kind="building", building="yes")
        net = build(blocks=[hall(), other], ways=[
            way(1, [(1, -40, 0), (10, -10, 0)]),
            way(2, [(20, 15, 0), (2, 40, 0)]),
            way(3, [(1, -40, 0), (3, -40, -30), (4, 40, -30), (2, 40, 0)]),
        ])
        r = route(net, start=(8, 0), when=NOON)
        shape = other.shape.buffer(-0.3)
        for a, b in zip(r.points, r.points[1:]):
            assert LineString([a, b]).intersection(shape).length == pytest.approx(0.0, abs=0.01)

    def test_doors_on_different_floors_are_not_joined(self):
        """No elevator data, so a route never changes floors indoors."""
        ways = [way(1, [(1, -40, 0), (10, -10, 0)]), way(2, [(20, 10, 0), (2, 40, 0)]),
                way(3, [(1, -40, 0), (3, -40, -30), (4, 40, -30), (2, 40, 0)])]
        net = build(ways=ways, entrances={10: at(-10, 0), 20: at(10, 0)},
                    entrance_levels={10: "0", 20: "1"})
        assert route(net).indoor_m == 0.0


class TestWalls:
    def test_connectors_never_cut_through_a_building(self):
        """REGRESSION. The straight links to the graph used to cross walls and
        count the crossing as dry. With the hall closed, the only honest route
        from just west of it to just east of it goes round."""
        net = build()
        r = route(net, start=(-15, 0), stop=(15, 0), when=LATE)
        shape = hall().shape.buffer(-0.5)
        for a, b in zip(r.points, r.points[1:]):
            assert LineString([a, b]).intersection(shape).length == pytest.approx(0.0, abs=0.01)


class TestAccessibleCosts:
    def test_steps_are_avoided_when_a_step_free_way_exists(self):
        ways = [
            way(1, [(1, 0, 0), (2, 20, 0)], highway="steps"),
            way(2, [(1, 0, 0), (3, 10, 20), (2, 20, 0)]),  # ~45 m ramp round
        ]
        r = route(build(blocks=[], ways=ways), start=(0, 0), stop=(22, 0))
        assert "route includes steps" not in r.notes
        steps = LineString([at(0, 0), at(20, 0)])
        for a, b in zip(r.points, r.points[1:]):
            assert LineString([a, b]).intersection(steps).length < 0.5

    def test_steps_are_used_and_flagged_when_they_are_the_only_way(self):
        ways = [way(1, [(1, 0, 0), (2, 20, 0)], highway="steps")]
        r = route(build(blocks=[], ways=ways), start=(0, 0), stop=(22, 0))
        assert "route includes steps" in r.notes

    def _crossing(self, kerbs):
        ways = [way(1, [(1, 0, 0), (2, 0, 10), (3, 0, 20)], footway="crossing")]
        return route(build(blocks=[], ways=ways, kerbs=kerbs), start=(0, -1), stop=(0, 22))

    def test_a_crossing_with_ramps_at_both_ends_is_free(self):
        r = self._crossing({1: "lowered", 3: "flush"})
        assert r.penalty == 0.0
        assert not any("crossing" in n for n in r.notes)

    def test_a_crossing_with_no_mapped_kerbs_is_noted(self):
        r = self._crossing({})
        assert r.penalty == pytest.approx(SETTINGS.unknown_crossing_penalty_m)
        assert "1 crossing with no mapped curb ramp" in r.notes

    def test_a_raised_kerb_costs_most(self):
        r = self._crossing({1: "lowered", 3: "raised"})
        assert r.penalty == pytest.approx(SETTINGS.raised_crossing_penalty_m)
        assert "crosses a road at a raised curb" in r.notes


class TestOpeningHours:
    @pytest.mark.parametrize("spec,hour,expected", [
        ("24/7", 3, True),
        ("06:30-22:00", 6, False),
        ("06:30-22:00", 12, True),
        ("06:30-22:00", 22, False),
        ("Mo-Fr 07:00-22:00; Sa 08:00-17:00", 12, True),   # NOON is a Monday
        ("Sa-Su 08:00-17:00", 12, False),
        ("Mo-Fr 07:00-12:00,13:00-22:00", 12, False),
        ("Mo off", 12, False),
        ("Mo-Fr 22:00-02:00", 23, True),                    # crosses midnight
    ])
    def test_common_forms(self, spec, hour, expected):
        assert is_open(spec, NOON.replace(hour=hour)) is expected

    @pytest.mark.parametrize("spec", ["", "sunrise-sunset", "PH off", "Mo-Fr 7am-10pm"])
    def test_anything_else_is_unparsed(self, spec):
        assert is_open(spec, NOON) is None

    def test_default_window(self):
        assert in_default_window(NOON, ("07:00", "22:00"))
        assert not in_default_window(LATE, ("07:00", "22:00"))


class TestCurbAccessRanking:
    def _ranked(self, spot_id, score, access, ramp=None):
        s = Spot(spot_id=spot_id, stop_point=LatLng(lat=25.7569, lng=-80.3722),
                 walk_distance_m=50.0, curb_access=access, ramp_distance_m=ramp)
        return RankedSpot(spot=s, score=score)

    def test_a_known_ramp_beats_an_equal_unknown_kerb(self):
        out = service._apply_curb_access([
            self._ranked("unknown", 0.5, CurbAccess.UNKNOWN),
            self._ranked("ramp", 0.5, CurbAccess.LOWERED, ramp=5.0),
        ])
        assert [r.spot.spot_id for r in out] == ["ramp", "unknown"]

    def test_unknown_is_ranked_lower_not_dropped(self):
        out = service._apply_curb_access([self._ranked("u", 0.5, CurbAccess.UNKNOWN)])
        assert len(out) == 1 and out[0].score == pytest.approx(0.4)

    def test_a_ramp_at_the_limit_loses_a_little(self):
        near = service._curb_factor(self._ranked("a", 1, CurbAccess.LOWERED, 0.0).spot)
        far = service._curb_factor(self._ranked("b", 1, CurbAccess.LOWERED, 15.0).spot)
        assert near == pytest.approx(1.0)
        assert far == pytest.approx(1.0 - SETTINGS.curb_lowered_decay)


class TestDespike:
    def test_an_out_and_back_leg_is_removed(self):
        pts = [at(0, 0), at(10, 0), at(20, 0), at(14, 0), at(14, 5)]
        out = walk_network._despike(pts)
        assert out[0] == pts[0] and out[-1] == pts[-1]
        assert walk_network._polyline_length(out) < walk_network._polyline_length(pts)
        xs = [p[0] - OX for p in out]
        assert max(xs) <= 14.01  # never walks past 14 m and back

    def test_a_real_corner_is_kept(self):
        pts = [at(0, 0), at(10, 0), at(10, 10)]
        assert walk_network._despike(pts) == pts


class TestRouteAccessibility:
    def test_ramps_and_crossings_are_reported(self):
        ways = [way(1, [(1, 0, 0), (2, 0, 10), (3, 0, 20)], footway="crossing")]
        net = build(blocks=[], ways=ways, kerbs={1: "lowered"})
        r = route(net, start=(0, -1), stop=(0, 22))
        a = walk_network.to_accessibility(r, FRAME)
        assert a.step_free is True
        assert len(a.curb_ramps) == 1
        assert a.unramped_crossings == 1  # one end mapped, the other not

    def test_steps_make_it_not_step_free(self):
        ways = [way(1, [(1, 0, 0), (2, 20, 0)], highway="steps")]
        r = route(build(blocks=[], ways=ways), start=(0, 0), stop=(22, 0))
        assert walk_network.to_accessibility(r, FRAME).step_free is False


class TestLeavingABuilding:
    def test_a_door_line_may_not_leave_a_concave_building(self):
        """REGRESSION. In an L-shaped building the straight line from the rider
        to a far door crossed the courtyard outside -- past the car -- and was
        counted as dry, indoor walking."""
        ell = ShadeBlock(
            block_id="ell", height_m=10.0, kind="building", building="university", name="Ell",
            shape=Polygon([at(0, 0), at(40, 0), at(40, 10), at(10, 10), at(10, 40), at(0, 40)]),
        )
        # One door at the far end of each arm; the rider is in the vertical arm.
        ways = [way(1, [(10, 40, 5), (2, 60, 5)]), way(2, [(20, 5, 40), (3, 5, 60)]),
                way(3, [(2, 60, 5), (4, 60, 60), (3, 5, 60)])]
        net = build(blocks=[ell], ways=ways)
        s = walk_network.search(net, at(5, 30), mode="length")
        door_links = [lk for lk in s.rider_links if lk.flag != "wall_exit"]
        assert {lk.node for lk in door_links} == {20}  # 40,5 is only reachable by leaving

    def test_a_rider_can_walk_straight_out_toward_a_nearby_car(self):
        """"Nearest side" means the side facing the car, not whichever wall is
        nearest the rider."""
        ways = [way(1, [(1, -40, 0), (10, -10, 0)]),        # the only door, west
                way(2, [(5, 30, -20), (6, 30, 20)])]          # a path east, no door
        net = build(ways=ways)
        r = route(net, start=(8, 0), stop=(20, 0), when=NOON)
        assert r.length == pytest.approx(12.0, abs=0.5)
        assert "leaves by the nearest side; no door is mapped there" in r.notes
